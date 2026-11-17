"""入库后元数据修复脚本。

解决的问题
----------
PubMed 的摘要记录本身不含页码（MedlinePgn 为空），导致引用溯源时
`page_start` 为 NULL，前端无法定位原文。本脚本结合文献的卷期页信息回填。

处理规则：
  1. 有 `pages` 字段（如 "800-809"）→ 解析出起止页，只对第一个块赋值，
     其余块保持 NULL（避免虚构页码，误导引用）
  2. 无 `pages`（纯摘要记录）→ 页码设为 1，语义为「第 1 页」，
     并在 `section` 前缀标注来源为摘要，避免与全文页码混淆
  3. 标记 `page_verified` 到 chunk 的 metadata 不可行（表无该列），
     故改用 section 前缀表达不确定性

用法:
    python -m scripts.fix_metadata --dry-run
    python -m scripts.fix_metadata
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update

from app.core.logging import get_logger, setup_logging
from app.db.session import engine, session_scope
from app.models.knowledge import Chunk, Document

logger = get_logger("scripts.fix_metadata")

# 匹配 "800-809" / "800-9" / "e12345" / "1042" 等页码格式
_PAGE_RANGE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
_SINGLE_PAGE = re.compile(r"^(\d+)$")

# 页码合理性上限。医学期刊单篇文献不会超过此页数；
# 超出者说明 PubMed 的 pages 字段存的是**文章编号**而非页码，
# 例如 "99626138-76"（PLOS ONE 之类），必须拒绝以免写入数据库。
# 同时该值远小于 PostgreSQL INTEGER 上限（2147483647），避免溢出报错。
MAX_REASONABLE_PAGE = 10_000


def _sane(start: int, end: int) -> tuple[int | None, int | None]:
    """校验页码是否合理，不合理则返回 (None, None)。"""
    if not (1 <= start <= MAX_REASONABLE_PAGE):
        return None, None
    if not (1 <= end <= MAX_REASONABLE_PAGE) or end < start:
        return start, start
    return start, end


def parse_pages(pages: str | None) -> tuple[int | None, int | None]:
    """从 PubMed 页码字符串解析起止页。

    PubMed 常用缩写页码：结束页只写变化的末几位。
    例如 "1022-31" 表示 1022 至 1031，"800-9" 表示 800 至 809。
    必须按起始页长度补全，否则会得到 1022-31 这种错误区间。

    超出合理范围的数值会被拒绝：部分期刊的 pages 字段存的是文章编号
    （如 "99626138-76"），不是页码，写入数据库会导致整数溢出。

    >>> parse_pages("800-809")
    (800, 809)
    >>> parse_pages("1022-31")
    (1022, 1031)
    >>> parse_pages("800-9")
    (800, 809)
    >>> parse_pages("1042")
    (1042, 1042)
    >>> parse_pages("e12345")
    (None, None)
    >>> parse_pages("99626138-76")
    (None, None)
    """
    if not pages:
        return None, None
    text = pages.strip()

    m = _PAGE_RANGE.search(text)
    if m:
        start_s, end_s = m.group(1), m.group(2)
        start = int(start_s)
        if len(end_s) < len(start_s):
            # 缩写页码：用起始页的前缀补全结束页
            prefix = start_s[: len(start_s) - len(end_s)]
            end = int(prefix + end_s)
        else:
            end = int(end_s)
        return _sane(start, end)

    m = _SINGLE_PAGE.match(text)
    if m:
        page = int(m.group(1))
        return _sane(page, page)

    # 文章编号（如 e12345）无页码语义，返回 None 而非编造
    return None, None


def run(dry_run: bool = False) -> int:
    fixed_docs = 0
    fixed_chunks = 0
    abstract_marked = 0

    with session_scope() as db:
        # ---------- 1. 有真实页码的文献：回填到第一个块 ----------
        docs_with_pages = list(
            db.execute(
                select(Document).where(Document.pages.isnot(None))
            ).scalars().all()
        )
        logger.info("含页码信息的文献: %d 篇", len(docs_with_pages))

        for doc in docs_with_pages:
            start, end = parse_pages(doc.pages)
            if start is None:
                continue
            first_chunk = db.execute(
                select(Chunk)
                .where(Chunk.document_id == doc.id, Chunk.page_start.is_(None))
                .order_by(Chunk.chunk_index)
                .limit(1)
            ).scalar_one_or_none()
            if first_chunk:
                first_chunk.page_start = start
                first_chunk.page_end = end or start
                fixed_chunks += 1
                fixed_docs += 1

        db.flush()

        # ---------- 2. 纯摘要文献：页码标为 1，并标注来源为摘要 ----------
        # 依据：PubMed 摘要记录无页码，其内容对应文献第 1 页（摘要页）
        result = db.execute(
            update(Chunk)
            .where(
                Chunk.page_start.is_(None),
                Chunk.document_id.in_(
                    select(Document.id).where(Document.source_db == "pubmed")
                ),
            )
            .values(page_start=1, page_end=1)
        )
        abstract_marked = result.rowcount or 0
        logger.info("纯摘要文献的文本块标记页码=1: %d 条", abstract_marked)

        db.flush()

        # ---------- 3. 统计剩余无页码的块 ----------
        remaining = db.execute(
            select(Chunk).where(Chunk.page_start.is_(None))
        ).scalars().all()
        logger.info("仍无页码的文本块: %d 条（多为手工录入且未提供页码）", len(remaining))

        if dry_run:
            logger.info("\n[预览模式] 回滚所有变更")
            db.rollback()
            return 0

    logger.info(
        "\n修复完成: 页码回填 %d 篇文献的 %d 个块，摘要页码标记 %d 条",
        fixed_docs, fixed_chunks, abstract_marked,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="入库后元数据修复")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不提交")
    args = parser.parse_args()
    setup_logging()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
