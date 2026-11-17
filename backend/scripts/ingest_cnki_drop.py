"""把用户从知网（或其他来源）下载的 PDF 批量补进知识库。

为什么需要这个脚本
------------------
知网没有可用的开放接口，抓取违反服务条款且有法律风险。合规做法是：
用学校/机构的账号在浏览器里下载 PDF，放到一个固定目录，再运行本脚本。

流程
----
    <drop_dir>/*.pdf
      → pdf_to_manual.build（版面块抽取）
      → 质量闸门（中文汉字数 / 英文字词数；字体缺 ToUnicode 的会被判不合格）
      → data/manual_staging_cnki/*.md
      → app.etl.cli manual 导入（按标题去重，可反复执行）

元数据
------
PDF 里通常有期刊页眉与年份，但抽取不稳定，因此**默认留空**，
需要时可在 manifest.csv 里显式指定：

    file,title,journal,year,evidence,phase
    中国结直肠癌筛查指南2024.pdf,中国结直肠癌筛查与早诊早治指南（2024版）,中华消化外科杂志,2024,guideline,preop

没有 manifest 时：标题取文件名（去扩展名），期刊/年份留空。

用法
----
    python -m scripts.ingest_cnki_drop --dry-run
    python -m scripts.ingest_cnki_drop
    python -m scripts.ingest_cnki_drop --dir "D:\\AI\\毕设\\补录"
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pdf_to_manual import build  # noqa: E402

DEFAULT_DROP = Path(r"D:\AI\RAG_project\data\cnki_drop")
STAGING = Path(r"D:\AI\RAG_project\data\manual_staging_cnki")
MIN_HAN = 800
MIN_WORDS = 800


def han_count(text: str) -> int:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")


def safe_name(title: str, limit: int = 90) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", title)[:limit] + ".md"


def load_manifest(drop_dir: Path) -> dict[str, dict]:
    """读取可选的 manifest.csv（按文件名索引）。"""
    p = drop_dir / "manifest.csv"
    if not p.exists():
        return {}
    out = {}
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("file"):
                out[row["file"].strip()] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DROP), help="PDF 放置目录")
    ap.add_argument("--dry-run", action="store_true", help="只转换并打印，不写文件")
    args = ap.parse_args()

    drop = Path(args.dir)
    if not drop.exists():
        drop.mkdir(parents=True, exist_ok=True)
        print(f"目录不存在，已创建：{drop}")
        print("请把从知网下载的 PDF 放进去，然后重跑本脚本。")
        return 0

    pdfs = sorted(drop.glob("*.pdf"))
    if not pdfs:
        print(f"{drop} 里没有 PDF。")
        return 0

    manifest = load_manifest(drop)
    STAGING.mkdir(parents=True, exist_ok=True)
    rows, ok = [], 0

    for pdf in pdfs:
        meta = manifest.get(pdf.name, {})
        title = meta.get("title") or pdf.stem
        info = {
            "title": title,
            "journal": meta.get("journal") or None,
            "year": meta.get("year") or None,
            "evidence": meta.get("evidence") or "guideline",
            "phase": meta.get("phase") or "general",
        }
        try:
            md, _ = build(pdf, info)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {pdf.name}: {type(exc).__name__}")
            rows.append({"file": pdf.name, "ok": False, "why": type(exc).__name__})
            continue

        han = han_count(md)
        words = len(re.findall(r"[A-Za-z]+", md))
        good = han >= MIN_HAN or words >= MIN_WORDS
        print(f"  {'✓' if good else '✗ 内容过少'} 汉字{han:>6} 词{words:>6} | {title[:44]}")
        rows.append({"file": pdf.name, "title": title, "han": han,
                     "words": words, "ok": good})
        if good:
            ok += 1
            if not args.dry_run:
                (STAGING / safe_name(title)).write_text(md, encoding="utf-8")

    print()
    print(f"共 {len(pdfs)} 个 PDF，合格 {ok} 个"
          + ("（dry-run，未写文件）" if args.dry_run else f" → {STAGING}"))
    report = Path(r"D:\AI\RAG_project\data\eval\cnki_drop_result.json")
    report.write_text(json.dumps({"drop_dir": str(drop), "total": len(pdfs), "ok": ok, "rows": rows},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.dry_run and ok:
        print()
        print("下一步（导入并生成向量）：")
        print(f'  python -m app.etl.cli manual --dir "{STAGING}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
