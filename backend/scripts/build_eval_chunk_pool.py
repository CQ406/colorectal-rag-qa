"""为新评测集挑选候选文本块（供人工出题）。

相比第一版，本版加了「正文判别」过滤，因为实测发现按 chunk_index 排序时
前面几块经常是题名/作者单位/期刊页眉，后面又会混入参考文献，这些块无法出题：

  - 跳过每篇前 3 块（题名、作者、摘要页眉）
  - 跳过 section 形如 Title/Abstract/references/Supplement 的块
  - 中文块要求汉字占比 ≥ 0.5 且含句末标点
  - 英文块要求不含大量 "et al"/"doi"/"pp." 这类参考文献特征

输出 data/eval/_chunk_pool.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import session_scope  # noqa: E402
from app.models.knowledge import Chunk, Document  # noqa: E402
from sqlalchemy import select  # noqa: E402

OUT = Path(r"D:\AI\RAG_project\data\eval\_chunk_pool.json")
BAD_SECTIONS = {"title", "abstract", "supplement", "references", "reference", "conclusion"}


def han_ratio(s: str) -> float:
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff") / max(len(s), 1)


def looks_like_refs(s: str) -> bool:
    """参考文献/作者单位块的判别。"""
    low = s.lower()
    if low.count("et al") >= 2 or low.count("doi") >= 2 or low.count("pp.") >= 2:
        return True
    # 邮箱/机构串
    if s.count("@") >= 1 or s.count("University") >= 2:
        return True
    return False


def main() -> int:
    with session_scope() as db:
        rows = db.execute(
            select(
                Chunk.id, Chunk.content, Chunk.section, Chunk.page_start,
                Chunk.chunk_index, Document.title, Document.evidence_level,
                Document.language, Document.created_at,
            )
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Document.is_active,
                Chunk.char_count >= 350,
                Chunk.char_count <= 1100,
                Chunk.chunk_index >= 3,
            )
            .order_by(Document.created_at.desc(), Document.id, Chunk.chunk_index)
        ).all()

    seen: dict[str, int] = {}
    pool = []
    for cid, content, section, page, idx, title, level, lang, created in rows:
        key = str(title)
        if seen.get(key, 0) >= 3:
            continue
        text = " ".join((content or "").split())
        if not text or looks_like_refs(text):
            continue
        sec = (section or "").strip().lower()
        if sec in BAD_SECTIONS:
            continue
        # 中文块要求汉字为主且像正文（有句末标点）
        if han_ratio(text) >= 0.5:
            if not re.search(r"[。；！？]", text):
                continue
        else:
            if len(re.findall(r"[A-Za-z]{3,}", text)) < 60:
                continue
        seen[key] = seen.get(key, 0) + 1
        pool.append({
            "id": str(cid),
            "title": key,
            "section": (section or "")[:20],
            "page": page,
            "level": level,
            "lang": (lang or "")[:3],
            "new_today": created.strftime("%m-%d") == "11-14",
            "text": text[:300],
        })

    OUT.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f" 正文候选块 {len(pool)} 块（每篇≤3），今日新增 {sum(1 for x in pool if x['new_today'])} 块")
    print(f" 已写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
