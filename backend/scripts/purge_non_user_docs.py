"""清理知识库：只保留用户提供的文献，其余永久删除。

用户的指令
----------
「知识库文献只留我给的，其他都删掉」

需要删除的两类
--------------
1. **11 篇早期种子文献**（从课题报审表手工录入，非用户下载的 PDF）：
   中文 5 篇（筛查指南 2020、肠外营养指南、预康复、卫生经济学、NOSES 质控）
   英文 6 篇（Attention is all you need、三篇 RAG 综述、医疗知识图谱综述、AUGUR-AI）
2. **1,253 条已下架记录**（其中 1,252 条为 PubMed 题录级文献）——
   它们已不在检索池内，此处一并物理清除，使库中只剩用户提供的文献。

保留
----
109 篇 = 中文 75（来自 D:\\AI\\毕设\\PDF 的 79 份可用 PDF）+ 英文 34（来自 D:\\AI\\毕设\\外网）

安全措施
--------
删除前把待删记录的**全部字段**导出为 JSON，并写出被删清单 CSV。
记录本身可据此重建（这些记录均无文本块：kb_chunk 总行数等于上架文献的
块数之和，说明下架记录并无子块，删除不会丢失已切分文本）。

级联说明
--------
kb_chunk 对 kb_document 是 ON DELETE CASCADE，删除文献会连带删除其文本块，
这是预期行为。sys_qa_log / sys_qa_feedback 无外键约束，历史日志保留。
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))

from sqlalchemy import delete, func, select  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.models.knowledge import Chunk, Document  # noqa: E402

BACKUP_DIR = Path(r"D:\AI\RAG_project\data\backup")

# 早期种子文献（课题报审表录入，非用户下载）
SEEDS = [
    "中国结直肠癌筛查与早诊早治指南（2020，北京）",
    "成人肠外营养脂肪乳注射液临床应用指南(2023版)",
    "从加速康复外科到预康复——理念更新与临床实践模式转变",
    "结直肠手术后并发症的卫生经济学分析",
    "经自然腔道取标本机器人手术系统直肠癌手术的质量控制",
    "Attention is all you need",
    "Retrieval-augmented generation for knowledge-intensive NLP tasks",
    "Retrieval-augmented generation for large language models: a survey",
    "A review on knowledge graphs for healthcare: resources, applications, and promises",
    "Benchmarking retrieval-augmented generation for medicine",
    "AUGUR-AI: a real-time artificial intelligence interpreter for indocyanine "
    "green fluorescence angiography in colorectal surgery",
]


def norm(s: str) -> str:
    s = (s or "").replace("（", "(").replace("）", ")")
    return re.sub(r"[\s\(\)\[\]【】《》,.:;；\-—_·/]", "", s).lower()


SEEDS_N = [norm(s) for s in SEEDS]


def is_seed(d: Document) -> bool:
    t = norm(d.title)
    if not t:
        return False
    for s in SEEDS_N:
        if t == s:
            return True
        n = min(len(t), len(s))
        if n >= 25 and t[:n] == s[:n]:
            return True
    return False


def doc_to_dict(d: Document) -> dict:
    return {
        "id": str(d.id), "title": d.title, "title_en": d.title_en,
        "authors": d.authors, "journal": d.journal, "year": d.year,
        "volume": d.volume, "issue": d.issue, "pages": d.pages,
        "doi": d.doi, "pmid": d.pmid, "language": d.language,
        "doc_type": d.doc_type, "evidence_level": d.evidence_level,
        "phase": d.phase, "source_db": d.source_db, "keywords": d.keywords,
        "publication_types": d.publication_types, "status": d.status,
        "is_active": d.is_active, "chunk_count": d.chunk_count,
        "abstract": d.abstract,
    }


def main() -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    with session_scope() as db:
        all_docs = db.execute(select(Document)).scalars().all()
        active = [d for d in all_docs if d.is_active]
        seeds = [d for d in active if is_seed(d)]
        keep = [d for d in active if not is_seed(d)]
        inactive = [d for d in all_docs if not d.is_active]
        to_delete = seeds + inactive

        chunk_total = db.execute(select(func.count()).select_from(Chunk)).scalar_one()
        seed_ids = [d.id for d in seeds]
        seed_chunks = db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.document_id.in_(seed_ids))
        ).scalar_one() if seed_ids else 0

        print("=== 删除前 ===")
        print("  全库记录 %d | 上架 %d | 下架 %d" % (len(all_docs), len(active), len(inactive)))
        print("  其中种子文献（上架）: %d 篇" % len(seeds))
        print("  你提供的文献（上架）: %d 篇" % len(keep))
        print("  文本块总数 %d（种子文献占 %d）" % (chunk_total, seed_chunks))
        print()
        print("  将删除: %d 篇（种子 %d + 下架 %d）" % (len(to_delete), len(seeds), len(inactive)))
        if len(to_delete) != len(seeds) + len(inactive):
            print("  ⚠ 计数异常，中止")
            return 1

        # 1) 全量备份
        backup = BACKUP_DIR / f"deleted_documents_{stamp}.json"
        backup.write_text(json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "只保留用户提供的文献，删除种子文献与已下架记录",
            "count": len(to_delete),
            "seeds": [doc_to_dict(d) for d in seeds],
            "inactive": [doc_to_dict(d) for d in inactive],
        }, ensure_ascii=False, indent=1), encoding="utf-8")

        # 2) 清单 CSV
        manifest = BACKUP_DIR / f"deleted_manifest_{stamp}.csv"
        with manifest.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["类别", "来源", "语种", "标题", "PMID", "DOI"])
            for d in seeds:
                w.writerow(["种子文献", d.source_db, d.language, d.title, d.pmid, d.doi])
            for d in inactive:
                w.writerow(["已下架", d.source_db, d.language, d.title, d.pmid, d.doi])

        print("  备份: %s" % backup.name)
        print("  清单: %s" % manifest.name)

        # 3) 删除（分批）
        ids = [d.id for d in to_delete]
        for i in range(0, len(ids), 200):
            db.execute(delete(Document).where(Document.id.in_(ids[i:i + 200])))
        db.flush()

        after = db.execute(select(Document)).scalars().all()
        chunk_after = db.execute(select(func.count()).select_from(Chunk)).scalar_one()
        zh = [d for d in after if re.search(r"[\u4e00-\u9fff]", d.title or "")]
        en = [d for d in after if d not in zh]

        print()
        print("=== 删除后 ===")
        print("  全库记录 %d 篇（上架 %d / 下架 %d）" % (
            len(after), sum(1 for d in after if d.is_active),
            sum(1 for d in after if not d.is_active)))
        print("  中文 %d 篇 | 英文 %d 篇" % (len(zh), len(en)))
        print("  文本块 %d 个（删除前 %d）" % (chunk_after, chunk_total))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
