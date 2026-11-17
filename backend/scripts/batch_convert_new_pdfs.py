"""批量把新增的中文 PDF 转为知识库录入文件（版面块流程）。

背景
----
用户补齐了文献：D:\\AI\\毕设\\PDF 现共 82 份，其中 41 份为上一批。
本脚本只处理**新增的 38 份**（82 − 41 旧有 − 3 排除）。

排除的 3 份
----------
1. 直肠癌新辅助治疗后等待观察策略中国专家共识(2024版)
   —— 与已入库的（2024版）同一文献，仅括号全角/半角不同，属重复下载。
2. 基于人工智能的临床决策支持系统在ICU中应用的范围综述
   —— 7 页仅 133 个汉字，扫描版，无法提取文字。
3. 直肠癌侧方淋巴结清扫的争议与共识
   —— 6 页 0 汉字，扫描版；且同主题已有《中国直肠癌侧方淋巴结转移
      诊疗专家共识（2024版）》（11796 汉字）可用。

输出与校验
----------
写入 data/manual_staging2/，并逐篇报告「PDF 可提取汉字数」与
「转换后汉字数」，两者差异过大说明转换环节丢了内容，需单独排查。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pymupdf

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pdf_to_manual import build  # noqa: E402

PDF_DIR = Path(r"D:\AI\毕设\PDF")
STAGING = Path(r"D:\AI\RAG_project\data\manual_staging2")

OLD_41 = {
    "_机器人_结直肠肿瘤经自然腔道取标本手术专家共识（2025版）",
    "MRI联合人工智能技术在直肠癌微观高危病理因素术前预测中的研究进展",
    "多序列MRI影像组学联合KRAS突变列线图模型预测直肠癌患者新辅助放化疗敏感性的价值",
    "腹腔镜结直肠癌根治术操作指南（2023版）",
    "腹腔镜直肠癌经括约肌间切除术后顽固性吻合口狭窄的影响因素及列线图模型构建",
    "国家卫生健康委员会中国结直肠癌诊疗规范（2025版）（精简版）",
    "机器人结直肠癌手术专家共识（2025版）",
    "机器人直肠癌经自然腔道取标本手术的难点与对策",
    "基于磁共振的深度学习重建方法在直肠癌术前评估中的应用研究",
    "基于大语言模型的电子病历创新应用研究进展",
    "基于人工智能的直肠癌MRI壁外血管侵犯自动评分模型的建立及其预测预后的价值",
    "基于人工智能和医疗大数据的肿瘤荧光手术导航与量化评估策略",
    "结肠镜辅助结肠肿瘤经自然腔道取标本手术专家共识及操作标准",
    "结直肠癌4K腹腔镜手术操作标准专家共识（2020版）",
    "结直肠癌CT和MRI标注专家共识（2020）",
    "结直肠癌肠造口患者全程营养管理专家共识（2025版）",
    "结直肠癌肝转移诊断和综合治疗指南（2025版）",
    "结直肠癌根治术后并发症的影响因素分析",
    "精准结直肠癌外科诊疗专家共识（2025版）",
    "精准外科胃癌转化治疗专家共识（2025版）",
    "人工智能时代机器人结直肠癌手术的现状及进展",
    "人工智能在结直肠癌微创诊疗中的应用与前景",
    "医学影像人工智能发展现状、困境剖析与前景展望",
    "医学专科领域大语言模型的应用与发展",
    "吲哚菁绿荧光显像技术在腹腔镜直肠癌手术中的应用进展",
    "吲哚菁绿荧光造影技术在腹腔镜结直肠癌根治术中的应用",
    "优化系膜裂孔缝合技术在腹腔镜辅助右半结肠切除术中的应用价值",
    "右半结肠癌手术中国专家共识（2024版）",
    "早期结直肠癌全程管理指南（2025版）",
    "直肠癌MR扫描及结构式报告规范专家共识",
    "直肠癌手术盆腔器官功能保护专家共识（2025版）",
    "直肠癌新辅助治疗后等待观察策略中国专家共识（2024版）",
    "直肠肿瘤三维可视化指导精准手术专家共识（2025版）",
    "智慧医学时代微创外科的进展与展望",
    "中国12余万例经自然腔道取标本手术（NOSES）临床应用现况分析",
    "中国恶性肿瘤整合诊治指南-结肠癌（2024版）",
    "中国恶性肿瘤整合诊治指南-直肠癌（2024版）",
    "中国结直肠癌肝转移诊断和综合治疗指南（2023版）",
    "中国结直肠癌诊疗规范（2023版）",
    "中国胃肠道肿瘤双镜联合手术临床实践指南（2025，深圳）",
    "中国直肠癌手术吻合口漏诊断、预防及处理专家共识（2019版）",
}

EXCLUDE = {
    "直肠癌新辅助治疗后等待观察策略中国专家共识(2024版)",
    "基于人工智能的临床决策支持系统在ICU中应用的范围综述",
    "直肠癌侧方淋巴结清扫的争议与共识",
}

HAN = re.compile(r"[\u4e00-\u9fff]")

# 文件名含这些词 → 判为指南/共识，否则为研究论文
GUIDE_KWS = ("指南", "共识", "规范", "标准")


def infer_meta(stem: str) -> dict:
    m = re.search(r"(20\d{2})", stem)
    year = m.group(1) if m else "2025"
    # 从标题里取刊名线索（多数文件名不含刊名，留空待补）
    journal = ""
    if "中华" in stem:
        journal = "中华系列期刊"
    return {
        "title": stem,
        "year": year,
        "journal": journal or None,
        "evidence": "guideline" if any(k in stem for k in GUIDE_KWS) else "review",
        "phase": "general",
    }


def pdf_han(p: Path) -> int:
    doc = pymupdf.open(p)
    n = sum(len(HAN.findall(pg.get_text() or "")) for pg in doc)
    doc.close()
    return n


def main() -> int:
    STAGING.mkdir(parents=True, exist_ok=True)
    files = sorted(PDF_DIR.glob("*.pdf"))
    todo = [f for f in files
            if f.stem not in OLD_41 and f.stem not in EXCLUDE]
    print("目录内 %d 份 | 旧有 %d | 排除 %d | 待转换 %d"
          % (len(files), len(OLD_41), len(EXCLUDE), len(todo)))
    print()

    rows = []
    for i, f in enumerate(todo, 1):
        try:
            src_han = pdf_han(f)
            md, st = build(f, infer_meta(f.stem))
            out = STAGING / (re.sub(r'[\\/:*?"<>|]', "_", f.stem) + ".md")
            out.write_text(md, encoding="utf-8")
            keep = st["han"] / src_han * 100 if src_han else 0
            rows.append({"stem": f.stem, "src": src_han, "out": st["han"],
                         "keep": keep, "sections": st["sections"],
                         "ok": True})
            print("[%2d/%d] %-46s %6d → %6d 汉字 (%5.1f%%) 节点%3d"
                  % (i, len(todo), f.stem[:46], src_han, st["han"], keep,
                     st["sections"]))
        except Exception as exc:  # noqa: BLE001
            rows.append({"stem": f.stem, "ok": False,
                         "err": f"{type(exc).__name__}: {exc}"})
            print("[%2d/%d] %-46s 失败: %s"
                  % (i, len(todo), f.stem[:46], str(exc)[:40]))

    ok = [r for r in rows if r["ok"]]
    print()
    print("=== 汇总 ===")
    print("  成功 %d | 失败 %d" % (len(ok), len(rows) - len(ok)))
    if ok:
        print("  源汉字合计 %d | 输出汉字合计 %d | 总保留率 %.1f%%"
              % (sum(r["src"] for r in ok), sum(r["out"] for r in ok),
                 sum(r["out"] for r in ok) / sum(r["src"] for r in ok) * 100))
        low = [r for r in ok if r["keep"] < 60]
        if low:
            print()
            print("  ⚠ 保留率 <60%% 的文件（需单独检查）: %d 份" % len(low))
            for r in low:
                print("     %-50s %d → %d (%.1f%%)"
                      % (r["stem"][:50], r["src"], r["out"], r["keep"]))
    print()
    print("暂存目录: %s" % STAGING)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
