"""补语料：把已下载但未入库的 PDF 转成录入文件（目标 120 篇）。

背景
----
任务书要求文献不少于 120 篇，当前 109 篇。`D:\\AI\\毕设` 下已有用户自行下载的
PDF，`plan_corpus_topup` 算出差集为：未入库 57 篇，其中中文可用 3 篇、英文可用 53 篇。
本脚本只处理**点名清单**，不做自动挑选 —— 补哪几篇是研究决策，不交给脚本猜。

处理流程
--------
    PDF → pdf_to_manual.build（版面块抽取，不按标点猜行）
        → 质量闸门（中文汉字数 / 英文词数）
        → data/manual_staging_topup/*.md
        → 人工 dry-run 预览后再用 app.etl.cli manual 导入

为什么分两步：转换是启发式的，批量导入前必须先看一遍统计，
否则一批有系统性问题的文本会污染检索结果（历史上踩过 PDF 字体缺 ToUnicode 的坑）。

用法：
    python -m scripts.topup_corpus --dry-run
    python -m scripts.topup_corpus
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pdf_to_manual import build  # noqa: E402

OUT = Path(r"D:\AI\RAG_project\data\manual_staging_topup")
EN_MANIFEST = Path(r"D:\AI\RAG_project\data\eval\topup_en_manifest.json")

# 中文三篇：期刊与年份取自 PDF 首页页眉（已人工核对）
ZH_MANIFEST = [
    {
        "pdf": r"D:\AI\毕设\PDF\中国结直肠癌肝转移诊断和综合治疗指南（2023版）.pdf",
        "title": "中国结直肠癌肝转移诊断和综合治疗指南（2023版）",
        "journal": "中华消化外科杂志",
        "year": "2023",
        "evidence": "guideline",
        "phase": "general",
    },
    {
        "pdf": r"D:\AI\毕设\PDF\_机器人_结直肠肿瘤经自然腔道取标本手术专家共识（2025版）.pdf",
        "title": "“机器人”结直肠肿瘤经自然腔道取标本手术专家共识（2025版）",
        "journal": "中华结直肠疾病电子杂志",
        "year": "2025",
        "evidence": "guideline",
        "phase": "intraop",
    },
    {
        "pdf": r"D:\AI\毕设\PDF\中国结直肠癌诊疗规范（2023版）.pdf",
        "title": "中国结直肠癌诊疗规范（2023版）",
        "journal": "中华消化外科杂志",
        "year": "2023",
        "evidence": "guideline",
        "phase": "general",
    },
]

MIN_HAN = 800      # 中文正文汉字数下限（低于此值多半是抽取失败）
MIN_WORDS = 800    # 英文正文词数下限


def han_count(text: str) -> int:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")


def clean_title(t: str) -> str:
    """清理英文题名：HTML 实体、软连字符、多余空白。"""
    t = t.replace("\u00ad", "").replace("\u2010", "-").replace("\u2011", "-")
    t = re.sub(r"&#x0*2010;", "-", t)
    t = re.sub(r"&#x0*([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), t)
    t = re.sub(r"&amp;", "&", t)
    return re.sub(r"\s+", " ", t).strip()


def safe_name(title: str, limit: int = 90) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", title)[:limit] + ".md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只转换并打印统计，不写文件")
    ap.add_argument("--en-manifest", default=None, help="英文清单路径（默认 data/eval/topup_en_manifest.json）")
    ap.add_argument("--zh-only", action="store_true", help="只处理中文清单")
    ap.add_argument("--en-only", action="store_true", help="只处理英文清单")
    ap.add_argument("--zh-manifest", default=None, help="中文清单 JSON（覆盖内置三条）")
    args = ap.parse_args()

    zh_source = list(ZH_MANIFEST)
    if args.zh_manifest and Path(args.zh_manifest).exists():
        zh_source = json.loads(Path(args.zh_manifest).read_text(encoding="utf-8"))
    tasks = [] if args.en_only else zh_source
    en_path = Path(args.en_manifest) if args.en_manifest else EN_MANIFEST
    if args.zh_only:
        en_path = Path("__none__")
    if en_path.exists():
        for row in json.loads(en_path.read_text(encoding="utf-8")):
            tasks.append({
                "pdf": row["pdf"],
                "title": clean_title(row["title"]),
                "journal": None,
                "year": None,
                "evidence": "review",
                "phase": "general",
            })
    else:
        print("  ⚠ 未找到英文清单 topup_en_manifest.json，只处理中文")

    OUT.mkdir(parents=True, exist_ok=True)
    rows, ok = [], 0
    for t in tasks:
        pdf = Path(t["pdf"])
        name = pdf.name[:44]
        if not pdf.exists():
            rows.append({"pdf": name, "ok": False, "why": "文件不存在"})
            print(f"  ✗ {name}: 文件不存在")
            continue
        try:
            md, _st = build(pdf, {
                "title": t["title"], "year": t["year"],
                "journal": t["journal"], "evidence": t["evidence"], "phase": t["phase"],
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({"pdf": name, "ok": False, "why": f"{type(exc).__name__}"})
            print(f"  ✗ {name}: {type(exc).__name__}")
            continue

        han = han_count(md)
        words = len(re.findall(r"[A-Za-z]+", md))
        sections = len(re.findall(r"^## ", md, flags=re.M))
        # 按语种选门限：中文看汉字数，英文看词数。
        # 此前用 evidence=="guideline" 判语种，导致中文综述（evidence=systematic）
        # 被英文词数门限误杀（6507 汉字 / 795 词被判为"内容过少"）。
        is_zh = han >= 300 or han > words
        good = (han >= MIN_HAN) if is_zh else (words >= MIN_WORDS)
        status = "✓" if good else "✗ 内容过少"
        print(f"  {status} 汉字{han:>6} 词{words:>6} 章节{sections:>2} | {t['title'][:46]}")
        rows.append({"pdf": name, "title": t["title"], "han": han, "words": words,
                     "sections": sections, "ok": good, "why": "" if good else "too_short"})
        if good:
            ok += 1
            if not args.dry_run:
                (OUT / safe_name(t["title"])).write_text(md, encoding="utf-8")

    print()
    print(f"共 {len(tasks)} 篇，合格 {ok} 篇" + ("（dry-run，未写文件）" if args.dry_run else f" → {OUT}"))
    report = Path(r"D:\AI\RAG_project\data\eval\corpus_topup_result.json")
    report.write_text(json.dumps({"total": len(tasks), "ok": ok, "rows": rows},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
