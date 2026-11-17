"""PubMed E-utilities 客户端。

API 说明：https://www.ncbi.nlm.nih.gov/books/NBK25499/
  esearch  —— 按查询式搜索，返回 PMID 列表
  efetch   —— 按 PMID 拉取完整题录与摘要（XML）
  esummary —— 拉取简要题录（JSON，本模块未用）

限流规则（必须遵守，否则会被封 IP）：
  - 无 API key：每秒最多 3 次请求
  - 有 API key：每秒最多 10 次请求
  本模块通过请求间隔控制速率，并在 429/5xx 时指数退避重试。

设计说明：
  统一返回 ParsedArticle 数据结构，与 PubMed XML 解耦，
  便于将来接入其他数据源（如 Europe PMC）时复用下游流程。
"""

from __future__ import annotations

import re
import time
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator

import re
import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("etl.pubmed")

# ----------------------------------------------------------------------
#  数据结构
# ----------------------------------------------------------------------
@dataclass
class AbstractSection:
    """结构化摘要的一个小节。"""

    label: str          # 如 Background / Methods / Results
    text: str
    page_start: int | None = None
    page_end: int | None = None


@dataclass
class ParsedArticle:
    """归一化后的文献题录。"""

    pmid: str | None
    title: str
    abstract_sections: list[AbstractSection] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    journal: str | None = None
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    language: str = "eng"
    mesh_terms: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)
    # 原文摘要（非英文）。多数中文文献的 PubMed 记录中，
    # 中文摘要作为独立的 AbstractText 段与英文摘要并列存在。
    # 实测抽样 12 篇中文文献，其中 4 篇（33%）含中文摘要段。
    abstract_zh_sections: list[AbstractSection] = field(default_factory=list)
    # 来源标识：pubmed（在线抓取）/ manual（人工录入，如中文指南与种子文献）
    # 分期与证据等级的**人工声明值**（仅人工录入的文献会设置）。
    # 为空时由 classifier 按正文自动判定，与 PubMed 文献走同一路径。
    # 人工录入时录入者明确知道该指南属于哪个围术期阶段，此时声明值优先 ——
    # 分类器对「筛查指南」这类跨阶段内容会判错（实测判为 postop）。
    phase_hint: str | None = None
    evidence_hint: str | None = None
    # 来源标识：pubmed（在线抓取）/ manual（人工录入，如中文指南与种子文献）
    source: str = "pubmed"
    source_path: str | None = None
    source_sha256: str | None = None
    source_page_count: int | None = None
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def abstract_text(self) -> str:
        """拼接全部摘要小节为完整摘要。"""
        if not self.abstract_sections:
            return ""
        if len(self.abstract_sections) == 1:
            return self.abstract_sections[0].text
        return "\n\n".join(
            f"{s.label}: {s.text}" if s.label else s.text
            for s in self.abstract_sections
        )

    @property
    def abstract_zh(self) -> str:
        """拼接原文摘要（中文）。无中文摘要时返回空串。"""
        if not self.abstract_zh_sections:
            return ""
        return "\n\n".join(s.text for s in self.abstract_zh_sections)

    @property
    def is_usable(self) -> bool:
        """是否有可用内容（无摘要的题录对问答系统无价值）。"""
        return bool(self.title) and len(self.abstract_text) >= 100


# ----------------------------------------------------------------------
#  客户端
# ----------------------------------------------------------------------
class PubMedClient:
    """PubMed E-utilities 客户端。"""

    def __init__(
        self,
        email: str | None = None,
        api_key: str | None = None,
        tool: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.email = email or settings.pubmed.pubmed_email
        self.api_key = api_key or settings.pubmed.pubmed_api_key or None
        self.tool = tool or settings.pubmed.pubmed_tool
        self.base = settings.pubmed.eutils_base

        # 有 key 时限流放宽到 10 req/s，否则 3 req/s；留出安全余量
        self.interval = 0.12 if self.api_key else 0.36
        self._last_request = 0.0

        # 代理：本机系统代理可能失效，显式禁用以免连接中断
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": f"{self.tool} (mailto:{self.email})"},
            trust_env=False,   # 忽略系统代理设置，避免 127.0.0.1:7899 连接失败
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    #  内部工具
    # ------------------------------------------------------------------
    def _common_params(self) -> dict[str, str]:
        params = {"tool": self.tool, "email": self.email}
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _throttle(self) -> None:
        """按限流间隔休眠。"""
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_request = time.monotonic()

    def _get(self, endpoint: str, params: dict, max_retries: int = 4) -> httpx.Response:
        """带重试与指数退避的 GET 请求。"""
        url = f"{self.base}/{endpoint}"
        merged = {**self._common_params(), **params}

        for attempt in range(max_retries):
            self._throttle()
            try:
                resp = self._client.get(url, params=merged)
                if resp.status_code == 200:
                    return resp
                # 429 限流 / 5xx 服务端错误 → 退避重试
                if resp.status_code in (429, 500, 502, 503, 504):
                    wait = 2**attempt
                    logger.warning(
                        "%s 返回 %d，%d 秒后重试 (%d/%d)",
                        endpoint, resp.status_code, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                wait = 2**attempt
                logger.warning(
                    "%s 请求异常: %s，%d 秒后重试 (%d/%d)",
                    endpoint, exc, wait, attempt + 1, max_retries,
                )
                time.sleep(wait)

        raise RuntimeError(f"{endpoint} 请求失败，已重试 {max_retries} 次")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PubMedClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    #  检索
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        max_results: int = 200,
        sort: str = "relevance",
        mindate: str | None = None,
        maxdate: str | None = None,
    ) -> list[str]:
        """按查询式搜索，返回 PMID 列表。

        Args:
            query: PubMed 查询式，支持字段限定如
                   (colorectal surgery[Title/Abstract]) AND (enhanced recovery[Title/Abstract])
            max_results: 最多返回条数
            sort: relevance（相关度）或 pub_date（按日期）
            mindate/maxdate: 日期范围，格式 YYYY/MM/DD
        """
        pmids: list[str] = []
        batch = 200          # E-utilities 单次上限 10000，取 200 稳妥
        retstart = 0

        while len(pmids) < max_results:
            params: dict = {
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": min(batch, max_results - len(pmids)),
                "retstart": retstart,
                "sort": sort,
            }
            if mindate:
                params["mindate"] = mindate
                params["maxdate"] = maxdate or "3000"
                params["datetype"] = "pdat"

            resp = self._get("esearch.fcgi", params)
            data = resp.json()
            result = data.get("esearchresult", {})
            ids = result.get("idlist", [])
            total = int(result.get("count", 0))

            if not ids:
                logger.info("搜索无更多结果，累计 %d 条", len(pmids))
                break

            pmids.extend(ids)
            logger.info(
                "已获取 %d/%d 条 PMID (库中共 %d 条匹配)",
                len(pmids), min(max_results, total), total,
            )

            retstart += len(ids)
            if retstart >= total:
                break

        return pmids[:max_results]

    def fetch(self, pmids: list[str], batch_size: int = 100) -> list[ParsedArticle]:
        """按 PMID 批量拉取并解析题录。"""
        articles: list[ParsedArticle] = []
        for i in range(0, len(pmids), batch_size):
            chunk = pmids[i : i + batch_size]
            logger.info("拉取题录 %d-%d / %d", i + 1, i + len(chunk), len(pmids))

            resp = self._get(
                "efetch.fcgi",
                {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml", "rettype": "abstract"},
            )
            parsed = parse_pubmed_xml(resp.text)
            articles.extend(parsed)

        logger.info("共解析 %d 篇文献题录", len(articles))
        return articles

    def fetch_by_title(self, title: str) -> ParsedArticle | None:
        """按标题查找单篇文献（用于录入报审表的种子文献）。

        先用标题精确匹配搜索，再用相似度确认，避免匹配到错误文献。
        """
        # 去掉标点后作为检索词，提高匹配率
        cleaned = title.replace(".", " ").replace(",", " ").strip()
        query = f'"{cleaned}"[Title]'
        pmids = self.search(query, max_results=5)
        if not pmids:
            # 退化为宽松检索
            pmids = self.search(f"{cleaned}[Title/Abstract]", max_results=5)
        if not pmids:
            return None

        articles = self.fetch(pmids[:3])
        if not articles:
            return None

        # 选标题最相似的一篇
        best = max(articles, key=lambda a: _title_similarity(title, a.title))
        score = _title_similarity(title, best.title)
        if score < 0.6:
            logger.warning("标题匹配度过低 (%.2f)，放弃: %s", score, title[:50])
            return None
        return best


# ----------------------------------------------------------------------
#  XML 解析
# ----------------------------------------------------------------------
def _text(elem: ET.Element | None) -> str:
    """安全提取元素全部文本。"""
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


# 中日韩统一表意文字范围（含扩展 A 区与兼容区）。
#
# ⚠️ 此处**不能用 raw string**：r"[\u4e00-\u9fff]" 里的 \u4e00 不会被
# Python 当作转义序列，而是字面字符 u、4、e、0、0，正则于是去匹配
# "u4e00"~"u9fff" 这样的字面串，永远匹配不到中文。
# 实测症状：原始 XML 中 40 篇有 16 篇存在中文摘要段，但解析器取到 0 篇；
# 而 _has_cjk("吻合口漏") 恰好返回 True（5 个汉字的码点落在那个字面区间内），
# 使这个 bug 更加隐蔽。
# 用于区分 PubMed 记录中并列的原文摘要段与英文翻译段。
_CJK_RE = re.compile("[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def _has_cjk(text: str) -> bool:
    """判断文本是否包含中日韩表意文字。

    用途：把 PubMed 记录里并列的中文摘要段与英文摘要段分开。
    只做「是否含中文」的二值判断，不做完整语种识别 ——
    摘要段通常纯为中文或纯为英文，二值判断已足够且无额外依赖。
    """
    return bool(text) and _CJK_RE.search(text) is not None


def _title_similarity(a: str, b: str) -> float:
    """标题相似度（基于字符集合的 Jaccard，对中英文都适用）。"""
    import re

    def norm(s: str) -> set[str]:
        s = s.lower()
        # 英文取单词，中文取单字
        words = set(re.findall(r"[a-z]{3,}", s))
        cjk = set(re.findall(r"[\u4e00-\u9fff]", s))
        return words | cjk

    sa, sb = norm(a), norm(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def parse_pubmed_xml(xml_text: str) -> list[ParsedArticle]:
    """解析 PubMed efetch 返回的 XML。"""
    if not xml_text.strip():
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("XML 解析失败: %s", exc)
        return []

    articles: list[ParsedArticle] = []

    for pubmed_article in root.findall(".//PubmedArticle"):
        try:
            articles.append(_parse_one(pubmed_article))
        except Exception as exc:  # noqa: BLE001 - 单篇失败不应影响整批
            logger.warning("解析单篇文献失败: %s", exc)

    return articles


def _parse_one(article: ET.Element) -> ParsedArticle:
    """解析单篇 PubmedArticle。"""
    citation = article.find("MedlineCitation")
    if citation is None:
        raise ValueError("缺少 MedlineCitation")

    pmid = _text(citation.find("PMID"))
    art = citation.find("Article")
    if art is None:
        raise ValueError("缺少 Article")

    # --- 标题 ---
    title = _text(art.find("ArticleTitle"))

    # --- 摘要（可能是单段或多段结构化摘要） ---
    #
    # ⚠️ 关键：中文文献的记录里，中文摘要会作为**独立的 AbstractText 段**
    # 与英文摘要并列存在（PubMed 对非英文文献同时提供原文摘要与英文翻译）。
    # 早期实现只取全部段拼成一段，导致中英文混在一起且中文部分与英文
    # 无法区分；更严重的是若按语言过滤排查，会误以为「中文文献只有英文内容」。
    #
    # 实测（抽样 12 篇中文文献）：题名含中文 0 篇（PubMed 只提供英文译名），
    # 摘要含中文 4 篇（33%）。因此中文摘要必须单独保留、单独切块，
    # 否则语料里几乎没有真正的中文文本，原生中文检索无法命中。
    sections: list[AbstractSection] = []
    zh_sections: list[AbstractSection] = []
    abstract_elem = art.find("Abstract")
    if abstract_elem is not None:
        for abs_text in abstract_elem.findall("AbstractText"):
            label = abs_text.get("Label", "")
            content = _text(abs_text)
            if not content:
                continue
            # 按是否含中日韩统一表意文字判定语种
            if _has_cjk(content):
                zh_sections.append(AbstractSection(label=label, text=content))
            else:
                sections.append(AbstractSection(label=label, text=content))

    # --- 作者 ---
    authors: list[str] = []
    author_list = art.find("AuthorList")
    if author_list is not None:
        for author in author_list.findall("Author"):
            last = _text(author.find("LastName"))
            initials = _text(author.find("Initials"))
            collective = _text(author.find("CollectiveName"))
            if collective:
                authors.append(collective)
            elif last:
                authors.append(f"{last} {initials}".strip())

    # --- 期刊信息 ---
    journal_elem = art.find("Journal")
    journal = None
    volume = issue = pages = None
    year = None

    if journal_elem is not None:
        journal = _text(journal_elem.find("Title")) or _text(
            journal_elem.find("ISOAbbreviation")
        )
        journal_issue = journal_elem.find("JournalIssue")
        if journal_issue is not None:
            volume = _text(journal_issue.find("Volume")) or None
            issue = _text(journal_issue.find("Issue")) or None
            pub_date = journal_issue.find("PubDate")
            if pub_date is not None:
                year_text = _text(pub_date.find("Year"))
                if not year_text:
                    # 有些记录只有 MedlineDate，如 "2023 Jan-Feb"
                    medline_date = _text(pub_date.find("MedlineDate"))
                    if medline_date:
                        year_text = medline_date[:4]
                if year_text.isdigit():
                    year = int(year_text)

    pages = _text(art.find("Pagination/MedlinePgn")) or None

    # --- DOI ---
    doi = None
    for article_id in article.findall(".//ArticleId"):
        if article_id.get("IdType") == "doi":
            doi = _text(article_id)
            break

    # --- 语言 ---
    language = _text(art.find("Language")) or "eng"

    # --- MeSH 主题词 ---
    mesh_terms: list[str] = []
    for mesh in citation.findall(".//MeshHeading/DescriptorName"):
        term = _text(mesh)
        if term:
            mesh_terms.append(term)

    # --- 关键词 ---
    keywords: list[str] = []
    for kw in citation.findall(".//KeywordList/Keyword"):
        term = _text(kw)
        if term:
            keywords.append(term)

    # --- 出版类型（证据等级推断依据） ---
    publication_types: list[str] = []
    for pt in art.findall(".//PublicationType"):
        term = _text(pt)
        if term:
            publication_types.append(term)

    return ParsedArticle(
        pmid=pmid,
        title=title,
        abstract_sections=sections,
        abstract_zh_sections=zh_sections,
        authors=authors,
        journal=journal,
        year=year,
        volume=volume,
        issue=issue,
        pages=pages,
        doi=doi,
        language=language,
        mesh_terms=mesh_terms,
        keywords=keywords,
        publication_types=publication_types,
    )
