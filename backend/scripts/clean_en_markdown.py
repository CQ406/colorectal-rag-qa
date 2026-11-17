"""清洗英文 PDF 转出的 Markdown：去期刊噪声、识别章节结构。

问题
----
直接从版面块导出的英文文献存在三类噪声，且没有 `##` 章节
（中文的编号式标题识别规则对英文无效），无法按章节入库：

1. **作者单位块**：以数字开头、含 Department / University 的长行，
   动辄数百字符，会严重干扰文本切分与向量语义。
2. **期刊流程噪声**：`EDITED BY`、`REVIEWED BY`、`frontiersin.org`、
   `Downloaded from`、`This article is` 等。
3. **页眉页脚**：单独的页码、期刊名加卷期。

处理
----
1. 丢弃噪声行
2. 把英文的常见章节名（ABSTRACT / Introduction / Methods / Results /
   Discussion / Conclusion / References 等）识别为 `##` 章节
3. 若一个章节都没识别出来，兜底加 `## Full Text`，保证可入库
"""
from __future__ import annotations

import re
from pathlib import Path

STAGING = Path(r"D:\AI\RAG_project\data\manual_staging_en")

# 常见英文章节名（整行匹配，允许带编号与冒号）
SECTION_NAMES = [
    "abstract", "summary", "introduction", "background", "objectives",
    "objective", "aim", "aims", "methods", "materials and methods",
    "method", "patients and methods", "study design", "results", "findings",
    "discussion", "conclusions", "conclusion", "limitations",
    "references", "bibliography", "acknowledgements", "acknowledgments",
    "supplementary material", "appendix", "keywords", "key words",
]

_SEC_RE = re.compile(
    r"^(?:(?:\d+|[IVX]+)[\.\s]+)?(" + "|".join(re.escape(s) for s in SECTION_NAMES) + r")\s*[:\.]?\s*$",
    re.I,
)

# 噪声行
NOISE_PATTERNS = [
    re.compile(r"^(EDITED BY|REVIEWED BY|PUBLISHED|CITATION|CORRESPONDENCE|"
               r"Copyright ©|This is an open access|Downloaded from|"
               r"This article is|Published in partnership|"
               r"Received:|Accepted:|Published online|"
               r"Academic Editor|Academic Editors|Citation:|"
               r"Frontiers in|frontiersin\.org|"
               r"ORIGINAL RESEARCH|SYSTEMATIC REVIEW|RESEARCH ARTICLE|"
               r"Original Article|Original Research|RESEARCH PAPER|"
               r"View from IRCAD|Article|TYPE |"
               r"This copy is for personal use only)", re.I),
    # 作者单位块：以数字开头且含机构词
    re.compile(r"^\d+\s*(Department|Division|School|Institute|University|"
               r"Faculty|Hospital|Center|Centre|College)", re.I),
    # 纯数字/页码
    re.compile(r"^[\d\s\-–—\.]+$"),
    # 页眉：期刊名 + 卷期页
    re.compile(r"^[A-Z][A-Za-z\s&\.]+\(\d{4}\)\s*\d+", re.I),
]

# 明显是作者名单的行（多个「姓 名首字母」+ 上标数字，或含 † * 等标记）
_AUTHOR_LINE = re.compile(
    r"^[A-Z][a-z]+ [A-Z]{1,3}[,;]|"
    r"^\d+\*|"
    r"^[A-Z][a-z]+ [A-Z][a-z]+ [A-Z]|"
    r"†"
)


def is_noise(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    for pat in NOISE_PATTERNS:
        if pat.search(s):
            return True
    # 超长的机构描述行（中文警示语不算）
    if len(s) > 260 and re.search(r"(Department|University|Hospital)", s, re.I):
        return True
    return False


def clean_body(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            if out and out[-1] != "":
                out.append("")
            continue
        m = _SEC_RE.match(s)
        if m:
            title = m.group(1)
            # 统一成首字母大写形式
            title = " ".join(w.capitalize() if w.isupper() else w for w in title.split())
            out.append("")
            out.append(f"## {title}")
            out.append("")
            continue
        if is_noise(s):
            continue
        if _AUTHOR_LINE.search(s) and len(s) < 200 and "," in s and "。" not in s:
            # 作者行：短、含逗号、无中文句号 —— 跳过
            continue
        out.append(s)
    return out


def process(path: Path) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # 找一级标题位置：保留 # 标题与元数据行，正文从警示语之后开始
    head: list[str] = []
    body_start = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("# ") or s.startswith("**") or s.startswith(">"):
            head.append(ln)
            continue
        if not s:
            head.append(ln)
            continue
        body_start = i
        break

    body = clean_body(lines[body_start:])

    # 确保有章节
    if not any(x.startswith("## ") for x in body):
        # 在正文第一段之前插入
        for i, x in enumerate(body):
            if x.strip():
                body.insert(i, "## Full Text")
                body.insert(i + 1, "")
                break

    result = "\n".join(head + body)
    result = re.sub(r"\n{3,}", "\n\n", result)
    path.write_text(result, encoding="utf-8")

    n_sec = sum(1 for x in result.split("\n") if x.startswith("## "))
    return n_sec, len(result)


if __name__ == "__main__":
    files = sorted(STAGING.glob("*.md"))
    print("清洗 %d 份英文录入文件" % len(files))
    print()
    print("%-70s %5s %8s" % ("文件", "章节", "字符"))
    print("-" * 88)
    total_sec = 0
    for f in files:
        n, ln = process(f)
        total_sec += n
        print("%-70s %5d %8d" % (f.stem[:70], n, ln))
    print()
    print("章节合计 %d | 平均 %.1f 章节/篇" % (total_sec, total_sec / len(files)))
    no_sec = [f for f in files if not any(
        x.startswith("## ") for x in f.read_text(encoding="utf-8").split("\n"))]
    print("仍无章节的文件: %d 份" % len(no_sec))
