"""盘点本地 PDF 与库内文献的差集，规划补语料。

背景
----
任务书要求文献不少于 120 篇，当前 109 篇，缺 11 篇（其中中文需 +5）。
`D:\\AI\\毕设` 下已有用户自行下载的 PDF（中文期刊/指南、外文），
先算清「哪些还没入库、哪些可用」，再决定补哪些。

判定方式（按可靠性排序）：
  1. 库内已有文件的 SHA256 → 完全一致即已入库
  2. 文件名归一化后与库内标题归一化做包含匹配
  3. 读 PDF 首页文本抽取汉字数，判断语种与可提取性（字体缺 ToUnicode 会几乎提不出字）

用法：
    python -m scripts.plan_corpus_topup --dry-run
    python -m scripts.plan_corpus_topup --out data/eval/corpus_topup_plan.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOTS = [
    Path(r"D:\AI\毕设\PDF"),
    Path(r"D:\AI\毕设\外网"),
    Path(r"D:\AI\毕设\PDF_PUBMED"),
]


def norm(s: str) -> str:
    """标题归一化：去空白、标点、全角符号，转小写。"""
    s = re.sub(r"[\s\u3000]", "", s or "")
    s = re.sub(r"[，。、；：？！“”‘’（）()《》【】\[\]·\-—_.,;:?!'\"<>|/\\]", "", s)
    return s.lower()


def han_ratio(s: str) -> float:
    if not s:
        return 0.0
    han = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return han / len(s)


def pdf_text_stats(path: Path) -> tuple[int, int]:
    """返回（可提取字符数, 汉字数）。字体缺 ToUnicode 时会接近 0。"""
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        with pymupdf.open(path) as doc:
            text = "".join(page.get_text() for page in doc[:5])
    except Exception:  # noqa: BLE001
        return 0, 0
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return len(text.strip()), han


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"D:\AI\RAG_project\data\eval\corpus_topup_plan.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.db.session import session_scope
    from app.models.knowledge import Document
    from sqlalchemy import select

    with session_scope() as db:
        docs = db.execute(
            select(Document.title, Document.language, Document.file_hash)
        ).all()
    db_titles = [(t or "", lang or "", h or "") for t, lang, h in docs]
    db_norm = [(norm(t), t, lang) for t, lang, _ in db_titles if t]

    files = []
    for root in ROOTS:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.pdf")):
            files.append(p)
    print(f"本地 PDF：{len(files)} 个；库内文献：{len(db_titles)} 篇\n")

    rows = []
    for p in files:
        stem = p.stem
        # 去掉常见的 PubMed 前缀编号
        stem_clean = re.sub(r"^\d{6,9}[_-]", "", stem)
        n = norm(stem_clean)
        matched = None
        for dn, dt, dlang in db_norm:
            if not dn:
                continue
            if n and (n in dn or dn in n) and min(len(n), len(dn)) >= 10:
                matched = dt
                break
        chars, han = (0, 0)
        if not matched:
            chars, han = pdf_text_stats(p)
        rows.append({
            "file": str(p),
            "name": p.name,
            "size_kb": round(p.stat().st_size / 1024),
            "in_db": bool(matched),
            "db_title": matched,
            "chars": chars,
            "han": han,
            "han_ratio": round(han / chars, 3) if chars else 0.0,
            "likely_lang": "zh" if han > 200 else ("en" if chars > 1500 else "unusable"),
        })

    new = [r for r in rows if not r["in_db"]]
    usable_zh = [r for r in new if r["likely_lang"] == "zh"]
    usable_en = [r for r in new if r["likely_lang"] == "en"]
    unusable = [r for r in new if r["likely_lang"] == "unusable"]

    print(f"未入库：{len(new)} 个（中文可用 {len(usable_zh)}、英文可用 {len(usable_en)}、不可用 {len(unusable)}）\n")
    print("中文候选中（按汉字数降序）：")
    for r in sorted(usable_zh, key=lambda x: -x["han"])[:20]:
        print(f"  {r['han']:>6} 汉字 {r['size_kb']:>6}KB  {r['name'][:56]}")
    print("\n不可用（字体缺 ToUnicode 或扫描件）：")
    for r in unusable[:8]:
        print(f"  chars={r['chars']:>6} han={r['han']:>5}  {r['name'][:56]}")

    if not args.dry_run:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"files": rows, "summary": {
            "total_pdf": len(files), "in_db": len(rows) - len(new),
            "new": len(new), "usable_zh": len(usable_zh),
            "usable_en": len(usable_en), "unusable": len(unusable),
        }}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
