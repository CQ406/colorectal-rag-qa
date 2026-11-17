"""中文指南/共识的人工录入与导入。

设计取向：为什么用 Markdown 文件而不是内联在代码里
--------------------------------------------------
早期实现（`seed_references.SeedReference`）把中文摘要写在 Python 源码的
字符串里，对短摘要可行，但录入**指南正文**会遇到三个问题：

  1. 指南正文长（数千至数万字），塞进代码字符串无法阅读与校对
  2. 指南有章节结构（推荐意见、证据等级、实施要点），
     内联字符串会丢失结构，切块时无法按章节切分
  3. 医学文本需要多人复核，代码文件不适合作为校对载体

因此改为**一份指南 = 一个 Markdown 文件**，用一个目录集中存放。
这样录入与校对就是编辑纯文本，章节用 `##` 标题天然对应切块边界。

文件格式
--------
    第一行：# 指南标题
    随后：  ## 章节标题
    正文：  自然段落
    可选元数据行（紧跟标题后）：**字段**：值

完整示例见 `data/manual/README.md`。元数据可省略，缺省值会按规则推断。

为什么元数据用「**字段**：值」而不是 YAML front matter
------------------------------------------------------
YAML 需要额外依赖，且对录入者来说语法更苛刻（缩进、引号转义易出错）。
医学指南的录入者通常不是程序员，「**期刊**：中华消化外科杂志」这种写法
所见即所得，不需要学习任何语法。

与 ETL 流程的衔接
-----------------
导入时构造 `ParsedArticle`（source="manual"），复用 `ingest_articles`，
因此去重、切块、向量化、分期与证据等级标注全部与 PubMed 文献一致，
不需要单独维护一条入库路径。

证据等级默认 `guideline`，因为人工录入的对象**就是**指南与共识 ——
这是本模块存在的主要理由：PubMed 无法提供中文正文，
而指南是最高等级证据，值得人工录入。
"""

from __future__ import annotations

import re
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger
from app.etl.pubmed_client import AbstractSection, ParsedArticle

logger = get_logger("etl.manual")

# 元数据行形如：**期刊**：中华消化外科杂志
_META_RE = re.compile(r"^\*\*(?P<key>[^*]+)\*\*\s*[:：]\s*(?P<value>.+?)\s*$")
# 一级标题（文献题名）
_H1_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$")
# 二级标题（章节）
_H2_RE = re.compile(r"^##\s+(?P<section>.+?)\s*$")

# 元数据字段名 → 内部字段。同时接受中英文写法，降低录入出错概率。
_META_KEYS: dict[str, str] = {
    "作者": "authors", "authors": "authors",
    "期刊": "journal", "来源": "journal", "journal": "journal",
    "年份": "year", "年": "year", "year": "year",
    "doi": "doi", "DOI": "doi",
    "发布机构": "publisher", "机构": "publisher",
    "证据等级": "evidence_level", "evidence": "evidence_level",
    "分期": "phase", "phase": "phase",
    "版本": "version",
    # 语种可显式声明（zh/en），留空则自动判定
    "语种": "language", "语言": "language", "language": "language",
    "lang": "language",
}


@dataclass
class ManualEntry:
    """一份人工录入的文献（通常是中文指南或共识）。"""

    title: str
    sections: list[tuple[str, str]] = field(default_factory=list)
    authors: str | None = None
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    publisher: str | None = None
    evidence_level: str = "guideline"
    phase: str | None = None
    version: str | None = None
    # 语种：留空则按正文汉字/拉丁字母比例自动判定（见 detect_language）
    language: str | None = None
    source_path: str | None = None
    source_sha256: str | None = None
    page_count: int | None = None
    section_pages: list[int | None] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def abstract_text(self) -> str:
        """拼接全部章节为一段完整文本。

        `ingest._build_document` 需要一个摘要字段用于分期与证据等级标注，
        这里把章节拼起来传入。切块时仍按章节走 `build_chunks` 的
        `_manual_sections` 分支，不会重复。
        """
        return "\n\n".join(
            f"{name}：{text}" if name else text for name, text in self.sections
        )


class ManualParseError(ValueError):
    """文件格式不符合要求。"""


def _sanitize_control_chars(text: str) -> str:
    """删除 PDF 抽取常带的不可见控制字符。

    为什么需要这一步
    ----------------
    PDF 的文字层里可能夹带 NUL（0x00）与其他 C0 控制字符（来自字体的
    私有编码或内嵌对象）。PostgreSQL 的 text 字段**直接拒绝 0x00**，
    报 `PostgreSQL text fields cannot contain NUL (0x00) bytes`，
    导致整篇文献入库失败。

    实测触发：一份 GraphRAG 论文（英文 PDF）在入库时因该错误被整篇拒绝。
    由于任何 PDF 都可能带这类字符，因此在解析入口统一清洗，
    而不是逐个文件手工处理。

    保留 \n（0x0A）与 \t（0x09），其余 C0 控制字符一并删除。
    """
    if not text:
        return text
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def parse_markdown(text: str, *, source_path: str | None = None) -> ManualEntry:
    """解析一份 Markdown 录入文件。

    Raises:
        ManualParseError: 缺少一级标题，或没有任何内容
    """
    # 兜底：即使调用方绕过 load_directory 直接传字符串，也剥离 BOM
    text = text.lstrip("\ufeff")
    # 清除 PDF 抽取带入的 NUL 等控制字符，否则 PostgreSQL 拒绝写入
    text = _sanitize_control_chars(text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    title: str | None = None
    meta: dict[str, str] = {}
    sections: list[tuple[str, str]] = []
    current_name: str | None = None
    current_buf: list[str] = []
    seen_content = False

    def flush() -> None:
        nonlocal current_buf
        body = "\n".join(current_buf).strip()
        if body:
            sections.append((current_name or "", body))
        current_buf = []

    for raw in lines:
        line = raw.rstrip()

        m1 = _H1_RE.match(line)
        if m1 and title is None:
            title = m1.group("title").strip()
            continue

        m2 = _H2_RE.match(line)
        if m2:
            flush()
            current_name = m2.group("section").strip()
            continue

        # 元数据只在正文开始前识别，避免把正文里形如 **重点**：... 的句子当元数据
        if not seen_content and title is not None:
            mm = _META_RE.match(line.strip())
            if mm:
                key = mm.group("key").strip()
                if key in _META_KEYS:
                    meta[_META_KEYS[key]] = mm.group("value").strip()
                    continue

        if line.strip():
            seen_content = True
        current_buf.append(line)

    flush()

    if not title:
        raise ManualParseError(
            "缺少一级标题。文件第一行应为「# 指南标题」。"
        )
    if not sections:
        raise ManualParseError(
            f"《{title}》没有任何正文内容。"
            "请用「## 章节名」分节并填入正文。"
        )

    year: int | None = None
    raw_year = meta.get("year")
    if raw_year:
        ym = re.search(r"(19|20)\d{2}", raw_year)
        if ym:
            year = int(ym.group(0))

    return ManualEntry(
        title=title,
        sections=sections,
        authors=meta.get("authors"),
        journal=meta.get("journal") or meta.get("publisher"),
        year=year,
        doi=meta.get("doi"),
        publisher=meta.get("publisher"),
        evidence_level=meta.get("evidence_level") or "guideline",
        phase=meta.get("phase"),
        version=meta.get("version"),
        language=meta.get("language"),
        source_path=source_path,
    )


def parse_docx(path: str | Path) -> ManualEntry:
    """解析 Word/DOCX 指南，返回与 Markdown 相同的 ``ManualEntry``。

    标题优先使用 Title/Heading 1；Heading 2/3 作为章节。普通段落会
    归入当前章节，表格内容按行拼接，便于导入带推荐意见表格的指南。
    元数据仍支持 Markdown 的 ``**字段**：值`` 行。
    """
    try:
        from docx import Document as WordDocument
    except ImportError as exc:  # pragma: no cover - requirements 已声明
        raise ManualParseError("未安装 python-docx，无法读取 DOCX") from exc

    from docx.oxml.text.paragraph import CT_P
    from docx.oxml.table import CT_Tbl
    from docx.text.paragraph import Paragraph
    from docx.table import Table

    raw_source = Path(path).read_bytes()
    doc = WordDocument(io.BytesIO(raw_source))
    lines: list[str] = []
    seen_title = False
    for block in doc.element.body.iterchildren():
        if isinstance(block, CT_P):
            paragraph = Paragraph(block, doc)
            text = _sanitize_control_chars(paragraph.text).strip()
            if not text:
                continue
            style = (paragraph.style.name or "").lower() if paragraph.style else ""
            if "title" in style:
                lines.append(f"# {text}")
                seen_title = True
            elif "heading 1" in style or "标题 1" in style:
                lines.append(("## " if seen_title else "# ") + text)
                seen_title = True
            elif "heading" in style or "标题" in style:
                lines.append(f"## {text}")
            else:
                lines.append(text)
        elif isinstance(block, CT_Tbl):
            table = Table(block, doc)
            for row in table.rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                cells = [cell for cell in cells if cell]
                if cells:
                    lines.append("；".join(cells))

    # 没有 Word 标题样式时，第一段作为题名，其余作为正文。
    if lines and not any(line.startswith("# ") for line in lines):
        lines.insert(0, "# " + lines.pop(0))
    entry = parse_markdown("\n".join(lines), source_path=str(path))
    entry.source_sha256 = hashlib.sha256(raw_source).hexdigest()
    return entry


def parse_pdf(path: str | Path) -> ManualEntry:
    """提取 PDF 文献文本，按页保留章节边界以便引用和检索。"""
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - requirements 已声明
        raise ManualParseError("未安装 PyMuPDF，无法读取 PDF") from exc

    source = Path(path)
    raw_source = source.read_bytes()
    try:
        with fitz.open(stream=raw_source, filetype='pdf') as pdf:
            metadata = pdf.metadata or {}
            pages = [_sanitize_control_chars(page.get_text("text")).strip() for page in pdf]
    except Exception as exc:  # noqa: BLE001
        raise ManualParseError(f"PDF 读取失败：{type(exc).__name__}: {exc}") from exc

    nonempty = [(index, text) for index, text in enumerate(pages, 1) if text]
    if not nonempty:
        raise ManualParseError("PDF 中未提取到文本；扫描件请先进行 OCR")

    first_lines = [line.strip() for line in nonempty[0][1].splitlines() if line.strip()]
    title = (metadata.get("title") or "").strip()
    if not title or title.lower() in {"untitled", "microsoft word"}:
        title = first_lines[0][:500] if first_lines else source.stem
    # 文件元数据可能是软件名而非题名，使用页面首行兜底。
    if len(title) < 4:
        title = first_lines[0][:500] if first_lines else source.stem

    full_text = "\n".join(text for _, text in nonempty)
    year_match = re.search(r"\b(19|20)\d{2}\b", full_text[:5000])
    author = (metadata.get("author") or "").strip() or None
    return ManualEntry(
        title=title,
        sections=[(f"第{page_no}页", text) for page_no, text in nonempty],
        authors=author,
        year=int(year_match.group(0)) if year_match else None,
        evidence_level="other",
        source_path=str(source),
        source_sha256=hashlib.sha256(raw_source).hexdigest(),
        page_count=len(pages),
        section_pages=[number for number, _ in nonempty],
        parse_warnings=[f"第{number}页没有可提取文本，请核对空白页或 OCR 需求"
                        for number, body in enumerate(pages, 1) if not body]
                       + [f"第{number}页可提取文字少于 30 字符，请核对扫描内容或文字层"
                          for number, body in nonempty if len(body) < 30],
    )


def load_directory(directory: str | Path) -> tuple[list[ManualEntry], list[str]]:
    """加载目录下全部 Markdown 与 DOCX 录入文件。

    Returns:
        (成功解析的条目列表, 错误信息列表)

    单个文件出错不中断整体导入 —— 批量录入时，
    因一个文件少写了标题而丢掉其余几十份指南是不可接受的。
    """
    root = Path(directory)
    entries: list[ManualEntry] = []
    errors: list[str] = []

    if not root.exists():
        return [], [f"目录不存在: {root}"]

    # 约定：文件名以下划线开头的是参考资料（模板、清单、说明），不参与导入；
    # README.md 同理。这样参考资料可以与正式录入内容放在同一目录，
    # 不必为「哪些该导入」单独维护一份列表。
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {".md", ".docx", ".pdf"}
        and not p.name.startswith("_")
        and not p.name.startswith("~$")
        and p.name.lower() not in {"readme.md", "readme.docx"}
    )
    if not files:
        return [], [f"目录下没有 .md、.docx 或可提取文本的 .pdf 文件（README 不计）: {root}"]

    for path in files:
        try:
            # 用 utf-8-sig 而非 utf-8：Windows 记事本、部分编辑器保存的
            # 中文文件会带 UTF-8 BOM（\\ufeff）。若用 utf-8 读取，BOM 会成为
            # 文件首字符，导致第一行的 `# 标题` 变成 `\\ufeff# 标题`，
            # 正则匹配失败并报「缺少一级标题」，**整篇文献被静默跳过**。
            # 实测踩过：一篇已整理好的共识因此未入库，而导入日志显示"更新 1 篇"
            # （那是另一篇的统计），极具误导性。
            if path.suffix.lower() == ".docx":
                entry = parse_docx(path)
            elif path.suffix.lower() == ".pdf":
                entry = parse_pdf(path)
            else:
                text = path.read_text(encoding="utf-8-sig")
                entry = parse_markdown(text, source_path=str(path))
            entries.append(entry)
            logger.info(
                "已解析《%s》%d 个章节", entry.title[:40], len(entry.sections)
            )
        except ManualParseError as exc:
            errors.append(f"{path.name}: {exc}")
            logger.warning("解析失败 %s: %s", path.name, exc)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            logger.warning("读取失败 %s: %s", path.name, exc)

    return entries, errors


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_language(entry: ManualEntry) -> str:
    """判定语种，返回 'chi' 或 'eng'。

    为什么必须自动判定
    ------------------
    早期实现把人工录入的语种**硬编码为 'chi'**，导致英文文献
    （如从外网 PDF 整理的 34 篇英文全文）在界面上被标成「中文*」，
    语种统计与「中文占比」也随之失真。

    判定顺序（先看标题，再看正文）
    ----------------------------
    1. 元数据显式声明 `**语种**：en/zh` → 以声明为准
    2. **标题含 ≥4 个汉字 → chi**；标题字母 ≥15 且汉字 <2 → eng
    3. 仅当标题无法判断时，才用正文汉字占比（阈值 0.15）

    为什么标题优先
    -------------
    正文比例不可靠：中文指南常夹带大量英文药名与缩写
    （FOLFOX、FOLFIRI、EGFR、RAS、BRAF…）以及英文参考文献。
    实测《结直肠癌肝转移诊断和综合治疗指南（2025版）》正文汉字占比仅 28%，
    若以 0.30 为阈值会被误判为英文文献。
    而标题的语种几乎不会歧义：纯英文文献标题不含汉字，
    中文文献标题必然含多个汉字。两者实测分离度极大
    （英文文献正文汉字占比约 0.1%，中文文献约 99.9%）。

    显式覆盖
    --------
    元数据写 `**语种**：en`（或 zh/chi/eng）时以声明为准，
    便于个别边界情况手工指定。
    """
    declared = (entry.language or "").strip().lower()
    if declared:
        if declared in ("en", "eng", "english"):
            return "eng"
        if declared in ("zh", "chi", "cn", "chinese"):
            return "chi"

    title = entry.title or ""
    title_han = len(_HAN_RE.findall(title))
    title_latin = len(_LATIN_RE.findall(title))
    if title_han >= 4:
        return "chi"
    if title_latin >= 15 and title_han < 2:
        return "eng"

    # 标题无法判断时看正文
    text = title + " " + " ".join(t for _, t in entry.sections)
    han = len(_HAN_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    total = han + latin
    if total == 0:
        return "chi"
    return "chi" if han / total >= 0.15 else "eng"


def to_parsed_article(entry: ManualEntry) -> ParsedArticle:
    """把录入条目转为 ETL 流程使用的 ParsedArticle。

    放入 `abstract_sections` 而非新增字段，使下游 `build_chunks`、
    `_build_document` 无需改动即可处理人工录入内容。

    分期与证据等级由 `ingest` 侧的分类器按正文重新判定，
    不直接采用元数据里的声明值 —— 保持与 PubMed 文献一致的处理路径，
    也让「录入者自报等级」与「分类器判定等级」可以互相校验。
    """
    lang = detect_language(entry)
    keywords = (["中文指南", "人工录入"] if lang == "chi" else ["英文文献", "人工录入"])
    if entry.version:
        keywords.append(entry.version)

    return ParsedArticle(
        pmid=None,
        title=entry.title,
        abstract_sections=[
            AbstractSection(label=name, text=text,
                            page_start=entry.section_pages[index] if entry.section_pages else None,
                            page_end=entry.section_pages[index] if entry.section_pages else None)
            for index, (name, text) in enumerate(entry.sections)
        ],
        authors=[entry.authors] if entry.authors else [],
        journal=entry.journal,
        year=entry.year,
        doi=entry.doi,
        language=lang,
        keywords=keywords,
        publication_types=["Guideline"] if entry.evidence_level in ('guideline', 'consensus') else ["Journal Article"],
        # 录入者声明的分期与证据等级。为空则退回分类器自动判定。
        phase_hint=entry.phase,
        evidence_hint=entry.evidence_level,
        source="manual",
        source_path=entry.source_path,
        source_sha256=entry.source_sha256,
        source_page_count=entry.page_count,
        parse_warnings=entry.parse_warnings,
    )


def stats(entries: list[ManualEntry]) -> dict:
    """录入内容的规模统计，用于导入前预览。"""
    total_chars = sum(len(text) for e in entries for _, text in e.sections)
    return {
        "documents": len(entries),
        "sections": sum(len(e.sections) for e in entries),
        "characters": total_chars,
        "documents_detail": [
            {
                "title": e.title,
                "sections": len(e.sections),
                "characters": sum(len(t) for _, t in e.sections),
                "journal": e.journal,
                "year": e.year,
            }
            for e in entries
        ],
    }
