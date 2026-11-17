"""批量把 PDF 转换为知识库录入文件。

为每份 PDF 配置元数据（标题/期刊/年份/分期），然后调用 pdf_to_manual 的
版面块流程生成 Markdown。

暂存到 data/manual_staging/，人工核对后再移入 data/manual/ 并导入。
分批入库的原因：一次导入 38 篇如果抽取有系统性问题，回滚成本高。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pdf_to_manual import build  # noqa: E402

PDF_DIR = Path(r"D:\AI\毕设\PDF")
STAGING = Path(r"D:\AI\RAG_project\data\manual_staging")

# 已完成（不再重复处理）
DONE = {
    "中国直肠癌手术吻合口漏诊断、预防及处理专家共识（2019版）",
    "国家卫生健康委员会中国结直肠癌诊疗规范（2025版）（精简版）",
    "中国恶性肿瘤整合诊治指南-直肠癌（2024版）",
}

# PDF 文件名关键词 → (期刊, 年份, 分期)
# 分期为人工判断：按文献主要适用阶段
META_MAP: list[tuple[str, str, str, str]] = [
    # (文件名关键词, 期刊, 年份, 分期)
    ("NOSES", "中华胃肠外科杂志", "2025", "intraop"),
    ("机器人", "中华胃肠外科杂志", "2025", "intraop"),
    ("三维可视化", "中华消化外科杂志", "2025", "intraop"),
    ("吲哚菁绿", "中华胃肠外科杂志", "2024", "intraop"),
    ("4K腹腔镜", "中华胃肠外科杂志", "2020", "intraop"),
    ("腹腔镜结直肠癌根治术操作指南", "中华胃肠外科杂志", "2023", "intraop"),
    ("结肠镜辅助", "中华胃肠外科杂志", "2024", "intraop"),
    ("双镜联合", "中华消化外科杂志", "2025", "intraop"),
    ("右半结肠癌", "中华胃肠外科杂志", "2024", "intraop"),
    ("系膜裂孔", "中华胃肠外科杂志", "2023", "intraop"),
    ("括约肌间切除", "中华胃肠外科杂志", "2023", "postop"),
    ("吻合口狭窄", "中华胃肠外科杂志", "2023", "postop"),
    ("根治术后并发症", "中华胃肠外科杂志", "2023", "postop"),
    ("肠造口", "中华胃肠外科杂志", "2025", "postop"),
    ("盆腔器官功能保护", "中华胃肠外科杂志", "2025", "postop"),
    ("等待观察", "中华胃肠外科杂志", "2024", "preop"),
    ("肝转移", "中华胃肠外科杂志", "2025", "general"),
    ("早期结直肠癌", "中华消化外科杂志", "2025", "general"),
    ("精准结直肠癌", "中华胃肠外科杂志", "2025", "general"),
    ("精准外科胃癌", "中华消化外科杂志", "2025", "general"),
    ("结肠癌（2024版）", "中国肿瘤临床", "2024", "general"),
    ("诊疗规范（2023版）", "中华消化外科杂志", "2023", "general"),
    ("MR扫描", "中华放射学杂志", "2020", "preop"),
    ("CT和MRI标注", "中华放射学杂志", "2020", "preop"),
    # 研究进展与综述类（非指南，证据等级按 review）
    ("MRI联合人工智能", "中华放射学杂志", "2024", "preop"),
    ("医学影像人工智能", "中华放射学杂志", "2024", "general"),
    ("医学专科领域大语言模型", "中华医学杂志", "2024", "general"),
    ("电子病历", "中华医院管理杂志", "2024", "general"),
    ("人工智能在结直肠癌微创", "中华胃肠外科杂志", "2024", "intraop"),
    ("人工智能时代机器人", "中华胃肠外科杂志", "2024", "intraop"),
    ("荧光手术导航", "中华消化外科杂志", "2024", "intraop"),
    ("智慧医学时代微创外科", "中华消化外科杂志", "2024", "intraop"),
    ("壁外血管侵犯", "中华放射学杂志", "2024", "preop"),
    ("影像组学联合KRAS", "中华放射学杂志", "2024", "preop"),
    ("NOSES）临床应用现况", "中华胃肠外科杂志", "2025", "intraop"),
    ("机器人直肠癌经自然腔道", "中华胃肠外科杂志", "2025", "intraop"),
]


def guess_meta(stem: str) -> dict:
    """按文件名推断元数据。"""
    for kw, journal, year, phase in sorted(META_MAP, key=lambda x: -len(x[0])):
        if kw in stem:
            return {"journal": journal, "year": year, "phase": phase}
    return {"journal": None, "year": None, "phase": "general"}


def is_guideline(stem: str) -> bool:
    return any(k in stem for k in ("指南", "共识", "规范", "标准"))


def main() -> int:
    STAGING.mkdir(parents=True, exist_ok=True)
    files = sorted(PDF_DIR.glob("*.pdf"))
    todo = [f for f in files if f.stem not in DONE]
    print("待处理 %d 份（已跳过 %d 份已完成）" % (len(todo), len(files) - len(todo)))
    print()

    rows = []
    for i, f in enumerate(todo, 1):
        m = guess_meta(f.stem)
        title = f.stem
        meta = {
            "title": title,
            "journal": m["journal"],
            "year": m["year"],
            "phase": m["phase"],
            "evidence": "guideline" if is_guideline(f.stem) else "review",
        }
        try:
            md, st = build(f, meta)
            out = STAGING / (re.sub(r'[\\/:*?"<>|]', "_", f.stem) + ".md")
            out.write_text(md, encoding="utf-8")
            rows.append({
                "file": f.stem, "ok": True, "han": st["han"],
                "sections": st["sections"], "paras": st["paras"],
                "out": out.name,
            })
            print("[%2d/%d] %-46s 汉字 %6d | 章节 %3d | 段落 %4d" % (
                i, len(todo), f.stem[:46], st["han"], st["sections"], st["paras"]))
        except Exception as exc:  # noqa: BLE001
            rows.append({"file": f.stem, "ok": False, "err": f"{type(exc).__name__}: {exc}"})
            print("[%2d/%d] %-46s 失败: %s" % (i, len(todo), f.stem[:46], str(exc)[:40]))

    print()
    ok = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    print("成功 %d 份 | 失败 %d 份" % (len(ok), len(bad)))
    if ok:
        print("  汉字合计 %d | 平均 %d 汉字/篇" % (
            sum(r["han"] for r in ok), sum(r["han"] for r in ok) // len(ok)))
        print("  章节合计 %d" % sum(r["sections"] for r in ok))
    low = [r for r in ok if r["han"] < 2000]
    if low:
        print()
        print("  ⚠ 汉字偏少（<2000）的 %d 份，需人工检查：" % len(low))
        for r in low:
            print("    %-48s %d 汉字" % (r["file"][:48], r["han"]))
    if bad:
        print()
        print("  失败明细:")
        for r in bad:
            print("    %-48s %s" % (r["file"][:48], r["err"][:50]))
    print()
    print("暂存目录: %s" % STAGING)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
