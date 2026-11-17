"""阶段一端到端验证脚本。

验证内容：
  1. 中文 bigram 分词正确性
  2. 数据库写入与 GIN 索引检索（关键词路）
  3. BGE-M3 向量化 + pgvector HNSW 检索（向量路）
  4. 两者结合的实际检索效果

用法:
    python -m scripts.verify_phase1
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import get_logger, setup_logging
from app.core.text_utils import extract_cjk_bigrams, tokenize_for_search
from app.db.session import session_scope
from app.models.knowledge import Chunk, Document
from app.repositories import search_repo
from app.services import search_service

logger = get_logger("scripts.verify_phase1")

# 用于验证的真实医学文本（结直肠围术期领域）
SAMPLE_CHUNKS = [
    {
        "title": "中国结直肠癌诊疗规范(2023版)",
        "journal": "中华消化外科杂志",
        "year": 2023,
        "phase": "general",
        "evidence_level": "guideline",
        "content": (
            "结直肠癌术后吻合口漏是结直肠手术最常见且最严重的并发症之一，"
            "发生率约为3%~15%。吻合口漏的处理原则包括：充分引流、禁食水、"
            "胃肠减压、广谱抗生素治疗，必要时行近端肠造口转流手术。"
        ),
        "page": 850,
        "section": "术后并发症处理",
    },
    {
        "title": "加速康复外科在结直肠手术中的应用进展",
        "journal": "中国实用外科杂志",
        "year": 2024,
        "phase": "postop",
        "evidence_level": "review",
        "content": (
            "加速康复外科（ERAS）在结直肠手术中的应用可显著缩短住院时间。"
            "术后早期进食与早期下床活动是ERAS的核心措施，"
            "推荐术后24小时内开始经口进食，可降低吻合口漏与肺部感染风险。"
        ),
        "page": 146,
        "section": "术后管理",
    },
    {
        "title": "结直肠手术术前准备的研究进展",
        "journal": "肠外与肠内营养",
        "year": 2023,
        "phase": "preop",
        "evidence_level": "review",
        "content": (
            "结直肠手术术前准备包括营养评估与营养支持。"
            "对存在营养不良的患者，推荐术前7~10天给予口服营养补充，"
            "以降低术后并发症发生率。不推荐常规行机械性肠道准备。"
        ),
        "page": 194,
        "section": "术前营养支持",
    },
    {
        "title": "机器人结直肠手术的现状与未来",
        "journal": "中华外科杂志",
        "year": 2024,
        "phase": "intraop",
        "evidence_level": "review",
        "content": (
            "机器人手术系统在直肠癌手术中具有视野清晰、操作精细的优势，"
            "尤其适用于骨盆狭窄的男性患者。术中应注意保护自主神经，"
            "以降低术后排尿功能障碍与性功能障碍的发生率。"
        ),
        "page": 26,
        "section": "术中操作要点",
    },
    {
        "title": "心血管疾病介入治疗进展（无关对照）",
        "journal": "中华心血管病杂志",
        "year": 2023,
        "phase": "general",
        "evidence_level": "review",
        "content": (
            "经皮冠状动脉介入治疗是冠心病血运重建的重要手段。"
            "药物洗脱支架可显著降低再狭窄率，术后需长期双联抗血小板治疗。"
        ),
        "page": 100,
        "section": "介入治疗",
    },
]


def seed_test_data() -> int:
    """写入测试数据（可重复运行，先清空同名文档）。"""
    titles = [c["title"] for c in SAMPLE_CHUNKS]
    with session_scope() as db:
        # 幂等：删除已有同名文档及其 chunk（级联删除）
        existing = (
            db.query(Document).filter(Document.title.in_(titles)).all()
        )
        for doc in existing:
            db.delete(doc)
        db.flush()

        count = 0
        for item in SAMPLE_CHUNKS:
            doc = Document(
                title=item["title"],
                journal=item["journal"],
                year=item["year"],
                phase=item["phase"],
                evidence_level=item["evidence_level"],
                doc_type="literature",
                source_db="manual",
                language="zh",
                status="pending",
            )
            db.add(doc)
            db.flush()  # 取得 doc.id

            chunk = Chunk(
                document_id=doc.id,
                content=item["content"],
                chunk_index=0,
                page_start=item["page"],
                page_end=item["page"],
                section=item["section"],
                phase=item["phase"],
                evidence_level=item["evidence_level"],
                doc_type="literature",
                # 关键：写入 bigram 倒排索引字段
                bigrams=tokenize_for_search(item["content"]),
                char_count=len(item["content"]),
            )
            db.add(chunk)
            doc.chunk_count = 1
            count += 1
        return count


def step1_tokenize() -> bool:
    """验证 1：bigram 分词。"""
    logger.info("=" * 70)
    logger.info("验证 1：中文 bigram 分词")
    logger.info("=" * 70)

    cases = [
        ("吻合口漏", ["吻合", "合口", "口漏"]),
        ("结直肠癌", ["结直", "直肠", "肠癌"]),
        ("术后 ERAS 管理", ["术后", "管理", "eras"]),
    ]
    ok = True
    for text, expected in cases:
        got = tokenize_for_search(text)
        # 逐项检查期望词元都在结果中
        missing = [e for e in expected if e not in got]
        status = "OK  " if not missing else "FAIL"
        if missing:
            ok = False
        logger.info("  [%s] %-18s -> %s", status, text, got)
        if missing:
            logger.info("         缺少期望词元: %s", missing)
    return ok


def step2_keyword_search() -> bool:
    """验证 2：关键词检索（bigram + GIN 索引）。"""
    logger.info("=" * 70)
    logger.info("验证 2：关键词检索（中文 bigram 重叠）")
    logger.info("=" * 70)

    queries = [
        ("吻合口漏", "中国结直肠癌诊疗规范(2023版)"),
        ("术前营养支持", "结直肠手术术前准备的研究进展"),
        ("机器人手术", "机器人结直肠手术的现状与未来"),
    ]

    with session_scope() as db:
        ok = True
        for query, expected_title in queries:
            results = search_repo.search_by_keyword(db, query, limit=5)
            hit = any(r.doc_title == expected_title for r in results)
            status = "OK  " if hit else "FAIL"
            if not hit:
                ok = False
            logger.info("  [%s] 查询 '%s' -> 命中 %d 条", status, query, len(results))
            for r in results[:3]:
                logger.info(
                    "          score=%.3f hits=%s  %s",
                    r.score, r.keyword_hits, r.doc_title[:30],
                )
        return ok


def step3_vector_search() -> bool:
    """验证 3：向量检索（BGE-M3 + pgvector HNSW）。"""
    logger.info("=" * 70)
    logger.info("验证 3：向量检索（BGE-M3 + pgvector）")
    logger.info("=" * 70)

    with session_scope() as db:
        chunks = db.query(Chunk).all()
        if not chunks:
            logger.error("  没有待向量化的 chunk")
            return False

        texts = [c.content for c in chunks]
        logger.info("  向量化 %d 条文本...", len(texts))
        vectors, elapsed = search_service.embed_texts(texts, batch_size=8)
        logger.info("  完成，耗时 %.2f 秒 (%.1f 条/秒)", elapsed, len(texts) / elapsed)

        # 写回向量
        for chunk, vec in zip(chunks, vectors, strict=True):
            chunk.embedding = vec
            chunk.embedding_model = search_service.EMBEDDING_MODEL_TAG
        db.flush()
        logger.info("  向量已写回数据库")

        # 用语义查询测试（注意：查询词与原文用词不同，考验语义泛化）
        semantic_queries = [
            ("如何预防手术后伤口愈合不良导致的渗漏", "吻合口漏"),
            ("病人开刀后多久可以吃东西", "术后早期进食"),
        ]
        ok = True
        for query, expect_keyword in semantic_queries:
            qvec, _ = search_service.embed_query(query)
            results = search_repo.search_by_vector(db, qvec, limit=3)
            top = results[0] if results else None
            hit = top is not None and expect_keyword in top.content
            status = "OK  " if hit else "注意"
            if not hit:
                ok = False
            logger.info("  [%s] 查询 '%s'", status, query)
            logger.info("         期望含关键词 '%s'", expect_keyword)
            for r in results[:3]:
                mark = "★" if expect_keyword in r.content else " "
                logger.info(
                    "         %s 相似度=%.4f  %s", mark, r.vector_score, r.content[:36]
                )
        return ok


def step4_hybrid() -> bool:
    """验证 4：实际检索服务（向量 + 重排）。"""
    logger.info("=" * 70)
    logger.info("验证 4：检索服务完整链路（向量召回 → 重排）")
    logger.info("=" * 70)

    from app.schemas.common import SearchRequest

    with session_scope() as db:
        req = SearchRequest(
            query="结直肠癌术后吻合口漏应该怎么处理？",
            top_k=3,
            use_rerank=True,
            recall_limit=10,
        )
        resp = search_service.search(db, req)
        logger.info("  查询: %s", resp.query)
        logger.info("  候选数: %d | 返回: %d | 重排: %s",
                    resp.total_candidates, resp.returned, resp.reranked)
        logger.info("  耗时明细: %s", resp.timings)
        for r in resp.results:
            logger.info(
                "    #%d 重排分=%.4f 向量分=%.4f | %s p.%s",
                r.rank, r.rerank_score or 0, r.vector_score or 0,
                r.doc_title[:26], r.page_start,
            )
        return resp.returned > 0


def main() -> int:
    setup_logging()
    results: dict[str, bool] = {}

    logger.info("写入测试数据...")
    n = seed_test_data()
    logger.info("已写入 %d 条文档与文本块\n", n)

    results["1.中文分词"] = step1_tokenize()
    results["2.关键词检索"] = step2_keyword_search()
    results["3.向量检索"] = step3_vector_search()
    results["4.检索服务"] = step4_hybrid()

    logger.info("=" * 70)
    logger.info("阶段一验证汇总")
    logger.info("=" * 70)
    for name, ok in results.items():
        logger.info("  [%s] %s", "通过" if ok else "未通过", name)

    passed = sum(results.values())
    logger.info("  结果: %d/%d 通过", passed, len(results))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
