"""知识库入库服务。

职责：把采集到的文献题录转换为可检索的文本块。
流程：
    ParsedArticle
      → 去重（按 PMID / DOI / 文件哈希）
      → 自动标注分期与证据等级
      → 文本切分（章节感知）
      → 生成 bigram 倒排索引
      → 批量向量化（GPU）
      → 写入 kb_document / kb_chunk

设计要点
--------
1. **幂等**：同一 PMID 重复入库会更新而非新增，可安全重跑。
2. **批量向量化**：先收集全部块再一次性编码，充分利用 GPU 吞吐
   （实测 694 条/秒，逐条编码会慢两个数量级）。
3. **进度可观测**：返回统计信息，供后台任务与前端展示。
"""

from __future__ import annotations

import re
import hashlib
from pathlib import Path

import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import EvidenceLevel, PerioperativePhase
from app.core.logging import get_logger
from app.core.text_utils import tokenize_for_search
from app.etl.chunker import TextChunk, split_document, chunk_by_tokens
from app.etl.classifier import classify_evidence, classify_phase
from app.etl.pubmed_client import ParsedArticle
from app.models.knowledge import Chunk, Document
from app.services.document_versions import archive_chunks
from app.core.config import PROJECT_ROOT

logger = get_logger("etl.ingest")

# 与向量模型绑定的标识，写入 Chunk.embedding_model 以便索引一致性校验
EMBEDDING_MODEL_TAG = "bge-m3-fp16"


@dataclass
class IngestStats:
    """入库统计。"""

    documents_created: int = 0
    documents_updated: int = 0
    documents_skipped: int = 0
    chunks_created: int = 0
    chunks_embedded: int = 0
    embed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def embed_throughput(self) -> float:
        """向量化吞吐（条/秒）。"""
        if self.embed_seconds <= 0:
            return 0.0
        return self.chunks_embedded / self.embed_seconds

    def summary(self) -> str:
        return (
            f"新增文献 {self.documents_created} / 更新 {self.documents_updated} "
            f"/ 跳过 {self.documents_skipped}，"
            f"生成文本块 {self.chunks_created}（向量化 {self.chunks_embedded}，"
            f"{self.embed_throughput:.0f} 条/秒，耗时 {self.embed_seconds:.1f}s）"
        )


# ----------------------------------------------------------------------
#  文档层
# ----------------------------------------------------------------------
def _normalize_title(text: str | None) -> str:
    """标题归一化，用于人工录入文献的去重。

    去掉全部空白与常见标点后比较，使下列写法视为同一篇：
        中国结直肠癌诊疗规范(2023版)
        中国结直肠癌诊疗规范（2023版）    ← 全角括号
        中国结直肠癌诊疗规范 2023版       ← 多余空格
    """
    if not text:
        return ""
    # 全角括号统一为半角，再去掉空白与括号类标点
    t = text.replace("（", "(").replace("）", ")")
    t = re.sub(r"[\s\(\)\[\]【】《》,，.。:：;；\-—_]", "", t)
    return t.lower()


def _find_existing(db: Session, article: ParsedArticle) -> Document | None:
    """按 PMID / DOI / 标题查找已入库文献，用于幂等更新。

    为什么必须有「按标题」这一路
    ---------------------------
    人工录入的中文指南通常既无 PMID 也无 DOI（指南一般不分配 DOI）。
    若只有前两路判定，**每次重复导入都会新建一条记录**。
    而指南需要反复校对与增补（改错字、补章节、更新版本），
    重复导入是高频操作，产生重复文献会直接污染检索结果。

    对 PubMed 文献无副作用：它们都带 PMID，第一步即命中返回，
    不会走到标题比较。
    """
    if article.pmid:
        doc = db.execute(
            select(Document).where(Document.pmid == article.pmid)
        ).scalar_one_or_none()
        if doc:
            return doc
    if article.doi:
        doc = db.execute(
            select(Document).where(Document.doi == article.doi)
        ).scalar_one_or_none()
        if doc:
            return doc

    # 标题匹配（主要服务于人工录入的指南）
    norm = _normalize_title(article.title)
    if norm:
        # 标点与空白的归一化在 Python 侧完成，SQL 无法直接比较，
        # 因此先按来源缩小范围，再逐条比较。
        # 人工录入量级很小（数十篇），这样做的代价可忽略。
        candidates = db.execute(
            select(Document).where(
                Document.source_db == "manual",
                Document.title.is_not(None),
            )
        ).scalars().all()
        for cand in candidates:
            if _normalize_title(cand.title) == norm:
                return cand
    return None


def _build_document(
    db: Session,
    article: ParsedArticle,
    doc: Document | None,
    source_db: str = "pubmed",
) -> tuple[Document, bool]:
    """构造或更新 Document 实体，返回 (文档, 是否新建)。

    Args:
        db: 数据库会话（新建文档时需 add）
        article: 解析后的文献题录
        doc: 已存在的文档（用于幂等更新），None 表示新建
        source_db: 来源标识 pubmed / manual
    """
    abstract = article.abstract_text
    title = article.title

    # 自动标注
    phase_result = classify_phase(title, abstract)
    evidence, evidence_reason = classify_evidence(
        publication_types=article.publication_types,
        title=title,
        abstract=abstract,
    )
    # 人工录入的文献可使用录入者声明的分期与证据等级覆盖分类器结果。
    #
    # 为什么需要覆盖：分类器按关键词匹配，对跨阶段内容会误判。
    # 实测《中国结直肠癌筛查与早诊早治指南》被判为 postop（置信度 0.60），
    # 而筛查指南应属 general —— 它讲筛查与早诊，不限于术后。
    # 人工录入时录入者明确掌握文献的适用阶段，此时声明值更可靠。
    #
    # 覆盖仅对人工录入生效（phase_hint 只由 manual_import 设置），
    # PubMed 文献继续走纯自动标注，保证既有评测结果可比。
    auto_phase = phase_result.phase.value
    auto_evidence = evidence.value
    phase_used = article.phase_hint or auto_phase
    evidence_used = article.evidence_hint or auto_evidence


    is_new = doc is None
    if doc is None:
        doc = Document(title=title)
        db.add(doc)

    # 中文文献标题字段：PubMed 都是英文，故 title_en 存原标题
    doc.title = title
    doc.title_en = title
    doc.authors = ", ".join(article.authors[:20]) if article.authors else None
    doc.journal = article.journal
    doc.year = article.year
    doc.volume = article.volume
    doc.issue = article.issue
    doc.pages = article.pages
    doc.doi = article.doi
    doc.pmid = article.pmid
    doc.language = "en" if article.language.startswith("eng") else article.language
    doc.abstract = abstract
    if article.source_path:
        # Keep immutable originals outside import discovery directories.
        source = Path(article.source_path)
        raw = source.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        if article.source_sha256 and article.source_sha256 != source_hash:
            raise ValueError('Source file changed after parsing; parse it again')
        destination = PROJECT_ROOT / 'data/document_sources' / (source_hash + source.suffix.lower())
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != source_hash:
            raise ValueError('Preserved source hash mismatch')
        if not destination.exists():
            destination.write_bytes(raw)
        doc.file_path = destination.relative_to(PROJECT_ROOT).as_posix()
        doc.file_hash = source_hash
        doc.file_size = len(raw)
        doc.page_count = article.source_page_count

    doc.doc_type = "literature"
    doc.evidence_level = evidence_used
    doc.phase = phase_used
    doc.source_db = source_db
    doc.keywords = article.keywords or None
    doc.mesh_terms = article.mesh_terms or None
    doc.publication_types = article.publication_types or None
    doc.status = "parsing"

    # 记录标注依据，便于人工复核（前端可展示，也是论文误差分析的素材）。
    # 人工声明被采纳时明确标出，避免与自动判定混淆。
    if article.phase_hint or article.evidence_hint:
        doc.error_message = (
            f"[人工声明] 分期={phase_used}"
            f"（分类器判为 {auto_phase}，置信度{phase_result.confidence:.2f}） "
            f"证据={evidence_used}（分类器判为 {auto_evidence}）"
        )
    else:
        doc.error_message = (
            f"[自动标注] 分期={auto_phase}"
            f"(置信度{phase_result.confidence:.2f},"
            f"{'需复核' if phase_result.needs_review else '可信'}) "
            f"证据={auto_evidence}({evidence_reason})"
        )
    if article.parse_warnings:
        doc.error_message += '\n[解析提醒] ' + '；'.join(article.parse_warnings)
    return doc, is_new


# ----------------------------------------------------------------------
#  切分层
# ----------------------------------------------------------------------
def build_chunks(article: ParsedArticle, doc: Document) -> list[TextChunk]:
    """把文献题录转为文本块。

    策略：
      - 有结构化摘要 → 按小节切分，保留小节名
      - 无结构化摘要 → 整段摘要切分
      - 标题也单独成块（标题蕴含核心主题，对检索有价值）
    """
    sections: list[tuple[str, str]] = []

    # 标题块：显式标注为 Title，便于检索时识别
    if article.title:
        sections.append(("Title", article.title))

    if article.abstract_sections:
        for s in article.abstract_sections:
            sections.append((s.label or "Abstract", s.text))
    elif article.abstract_text:
        sections.append(("Abstract", article.abstract_text))

    if any(section.page_start is not None for section in article.abstract_sections):
        chunks = split_document([('Title', article.title)]) if article.title else []
        for section in article.abstract_sections:
            for chunk in chunk_by_tokens(section.text, section=section.label,
                                         page_start=section.page_start):
                chunk.page_end = section.page_end or section.page_start
                chunk.index = len(chunks)
                chunks.append(chunk)
        return chunks
    return split_document(sections)


# ----------------------------------------------------------------------
#  向量化
# ----------------------------------------------------------------------
def embed_chunks(
    db: Session, chunks: list[Chunk], batch_size: int | None = None
) -> tuple[int, float]:
    """批量向量化并写回。

    Returns:
        (成功条数, 耗时秒)
    """
    if not chunks:
        return 0, 0.0

    from app.services.search_service import embed_texts

    texts = [c.content for c in chunks]
    # 不吞异常：向量化失败必须让调用方知道。
    #
    # 此前这里捕获所有异常并 `return 0`，后果是三层"假成功"：
    #   ① 文本块照常提交（无向量）→ 向量路永远检索不到这些块
    #   ② doc.status 此前已写成 embedded → 质量报告筛不出来
    #   ③ 批次统计写 success → 管理员以为全部入库成功
    # 现在改为抛错，由调用方标记文档状态与批次结果。
    vectors, elapsed = embed_texts(texts, batch_size=batch_size)

    count = 0
    for chunk, vec in zip(chunks, vectors, strict=False):
        chunk.embedding = vec
        chunk.embedding_model = EMBEDDING_MODEL_TAG
        count += 1
    db.flush()
    return count, elapsed


def unchanged_article(db: Session, article: ParsedArticle, doc: Document) -> bool:
    """Keep chunk IDs on identical imports; metadata changes create a revision."""
    probe, _ = _build_document(db, article, Document(title=article.title), source_db=article.source)
    fields = ('title', 'title_en', 'authors', 'journal', 'year', 'volume', 'issue', 'pages',
              'doi', 'pmid', 'language', 'abstract', 'doc_type', 'evidence_level', 'phase',
              'source_db', 'keywords', 'mesh_terms', 'publication_types')
    if article.source_path:
        fields += ('file_hash', 'file_size', 'page_count')
    if any(getattr(probe, field) != getattr(doc, field) for field in fields):
        return False
    old = db.scalars(select(Chunk).where(Chunk.document_id == doc.id)
                     .order_by(Chunk.chunk_index)).all()
    incoming = build_chunks(article, probe)
    return len(old) == len(incoming) and all(
        (a.content, a.section, a.page_start, a.page_end) ==
        (b.content, b.section, b.page_start, b.page_end) for a, b in zip(old, incoming))


# ----------------------------------------------------------------------
#  主流程
# ----------------------------------------------------------------------
def ingest_articles(
    db: Session,
    articles: list[ParsedArticle],
    *,
    embed: bool = True,
    skip_no_abstract: bool = True,
    batch_size: int | None = None,
) -> IngestStats:
    """批量入库文献。

    Args:
        db: 数据库会话
        articles: 解析后的文献题录
        embed: 是否立即生成向量（False 可先快速入库，稍后批量向量化）
        skip_no_abstract: 无摘要的文献是否跳过（对问答系统无价值）
        batch_size: 向量化批大小
    """
    stats = IngestStats()
    pending_chunks: list[Chunk] = []
    # 本批次涉及的文档：向量化成功后统一把状态置 embedded，
    # 失败则置 failed 并写入原因（便于质量报告与管理员定位）。
    pending_docs: dict[str, object] = {}

    for article in articles:
        try:
            if skip_no_abstract and not article.is_usable:
                stats.documents_skipped += 1
                logger.debug("跳过（无可用摘要）: %s", article.title[:60])
                continue

            article_chunks = []
            with db.begin_nested():
                existing = _find_existing(db, article)
                if existing is not None:
                    # Serialize replacement and snapshot original metadata first.
                    db.refresh(existing, with_for_update=True)
                    if unchanged_article(db, article, existing):
                        stats.documents_skipped += 1
                        continue
                    archive_chunks(db, existing)
                doc, is_new = _build_document(
                    db, article, existing, source_db=article.source
                )
                db.flush()   # 取得 doc.id

                if not is_new:
                    doc.is_reviewed = False
                    doc.reviewed_by = None
                    doc.reviewed_at = None
                    doc.review_note = None
                    # 更新时清空旧块，避免重复
                    db.query(Chunk).filter(Chunk.document_id == doc.id).delete()
                    db.flush()

                # 切分并建块
                text_chunks = build_chunks(article, doc)
                for tc in text_chunks:
                    chunk = Chunk(
                        document_id=doc.id,
                        content=tc.content,
                        chunk_index=tc.index,
                        page_start=tc.page_start,
                        page_end=tc.page_end,
                        section=tc.section,
                        char_start=tc.char_start,
                        char_end=tc.char_end,
                        phase=doc.phase,
                        evidence_level=doc.evidence_level,
                        doc_type=doc.doc_type,
                        # 中文关键词倒排索引
                        bigrams=tokenize_for_search(tc.content),
                        token_count=tc.token_count,
                        char_count=tc.char_count,
                    )
                    db.add(chunk)
                    article_chunks.append(chunk)
                doc.chunk_count = len(text_chunks)
                # 状态只到 parsing：embedded 要等真正向量化成功后再写。
                # 此前在这里就写 embedded，一旦后面向量化失败，库里状态是假的。
                doc.status = "parsing"
                db.flush()
                pending_docs[str(doc.id)] = doc
            pending_chunks = [chunk for chunk in pending_chunks if chunk.document_id != doc.id]
            pending_chunks.extend(article_chunks)
            stats.chunks_created += len(article_chunks)
            if is_new:
                stats.documents_created += 1
            else:
                stats.documents_updated += 1

        except Exception as exc:  # noqa: BLE001 - 单篇失败不影响整批
            msg = f"{article.pmid or article.title[:30]}: {type(exc).__name__}: {exc}"
            stats.errors.append(msg)
            logger.warning("入库失败 %s", msg)
            continue

    # 统一向量化（批量效率远高于逐篇）
    if embed and pending_chunks:
        try:
            count, elapsed = embed_chunks(db, pending_chunks, batch_size=batch_size)
        except Exception as exc:  # noqa: BLE001
            # 向量化失败：文档标记 failed，批次记为失败，并把异常上抛。
            # 文本块仍在库里（无向量），可事后用 vectorize_missing 补，
            # 但一定要让人知道"这批还没可用"。
            reason = f"向量化失败: {type(exc).__name__}: {exc}"[:300]
            stats.errors.append(reason)
            stats.chunks_embedded = 0
            for doc in pending_docs.values():
                doc.status = "failed"  # type: ignore[attr-defined]
                doc.error_message = reason  # type: ignore[attr-defined]
            db.flush()
            logger.error("批量向量化失败，已标记 %d 篇为 failed：%s",
                         len(pending_docs), reason)
            raise

        stats.chunks_embedded = count
        stats.embed_seconds = elapsed

        # 只有真正写完向量才把状态推进到 embedded
        if count >= len(pending_chunks):
            for doc in pending_docs.values():
                doc.status = "embedded"  # type: ignore[attr-defined]
                doc.error_message = None  # type: ignore[attr-defined]
        else:
            reason = f"向量化不完整：{count}/{len(pending_chunks)}"
            stats.errors.append(reason)
            for doc in pending_docs.values():
                doc.status = "failed"  # type: ignore[attr-defined]
                doc.error_message = reason  # type: ignore[attr-defined]
        db.flush()

    return stats


def vectorize_missing(
    db: Session, limit: int | None = None, batch_size: int | None = None
) -> tuple[int, float]:
    """为尚未向量化的文本块补充向量（断点续跑用）。

    适合场景：先用 embed=False 快速入库，再后台批量向量化。
    """
    stmt = select(Chunk).where(Chunk.embedding.is_(None))
    if limit:
        stmt = stmt.limit(limit)
    chunks = list(db.execute(stmt).scalars().all())
    if not chunks:
        logger.info("没有需要向量化的文本块")
        return 0, 0.0

    logger.info("待向量化 %d 条", len(chunks))
    count, elapsed = embed_chunks(db, chunks, batch_size=batch_size)
    logger.info("完成 %d 条，耗时 %.1f 秒 (%.0f 条/秒)",
                count, elapsed, count / elapsed if elapsed else 0)
    return count, elapsed
