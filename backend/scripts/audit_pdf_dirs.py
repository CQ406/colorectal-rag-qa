"""清点 D:\\AI\\毕设\\PDF 与 外网 两个文献目录，核对新增与重复。

用途
----
用户新下载了一批文献，需要：
1. 识别旧有的 41 份（避免重复入库）
2. 找出文件名异常（hash 命名、无标题）
3. 找出内容重复（同一文献下两次，括号全角半角不同）
4. 逐一测定可提取的汉字数，标出扫描版/乱码文件
5. 与知识库已有文献比对，避免重复

输出两份清单：中文、英文。
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pymupdf

CN_DIR = Path(r"D:\AI\毕设\PDF")
EN_DIR = Path(r"D:\AI\毕设\外网")

# 此前已下载并已入院的 41 份（按文件名，不含扩展名）
OLD_41 = {
    "_机器人_结直肠肿瘤经自然腔道取标本手术专家共识（2025版）",
    "MRI联合人工智能技术在直肠癌微观高危病理因素术前预测中的研究进展",
    "多序列MRI影像组学联合KRAS突变列线图模型预测直肠癌患者新辅助放化疗敏感性的价值",
    "腹腔镜结直肠癌根治术操作指南（2023版）",
    "腹腔镜直肠癌经括约肌间切除术后顽固性吻合口狭窄的影响因素及列线图预测模型构建",
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

HAN = re.compile(r"[\u4e00-\u9fff]")


def norm_title(s: str) -> str:
    """归一化标题：统一全角半角括号，去空格与标点。"""
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("，", ",").replace("：", ":")
    return re.sub(r"[\s\(\)\[\]【】《》,.:;；\-—_·]", "", s).lower()


def probe_pdf(p: Path) -> dict:
    """探测 PDF：页数、可提取字符数、汉字数、首页首行。"""
    info = {"pages": 0, "chars": 0, "han": 0, "first_line": "", "error": ""}
    try:
        doc = pymupdf.open(p)
        info["pages"] = doc.page_count
        total_chars = 0
        total_han = 0
        for page in doc:
            t = page.get_text() or ""
            total_chars += len(t)
            total_han += len(HAN.findall(t))
        info["chars"] = total_chars
        info["han"] = total_han
        if doc.page_count:
            first = (doc[0].get_text() or "").strip().split("\n")
            first = [x.strip() for x in first if x.strip()]
            info["first_line"] = first[0][:60] if first else ""
        doc.close()
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}"
    return info


def scan(dir_path: Path, label: str) -> list[dict]:
    files = sorted(dir_path.glob("*.pdf"))
    print("=" * 96)
    print("%s：%d 份" % (label, len(files)))
    print("=" * 96)

    rows: list[dict] = []
    for p in files:
        info = probe_pdf(p)
        info["name"] = p.stem
        info["size_kb"] = p.stat().st_size / 1024
        rows.append(info)

    # 1) 旧有 / 新增
    old = [r for r in rows if r["name"] in OLD_41]
    new = [r for r in rows if r["name"] not in OLD_41]
    print()
    print("  旧有（此前已下载）: %d 份" % len(old))
    print("  本次新增          : %d 份" % len(new))

    # 2) 异常文件名
    odd = [r for r in new if re.fullmatch(r"[0-9a-f]{20,}", r["name"])]
    if odd:
        print()
        print("  ⚠ 文件名异常（疑似 hash 命名）: %d 份" % len(odd))
        for r in odd:
            print("     %s  → 首页首行: %s" % (r["name"], r["first_line"] or "(空)"))

    # 3) 内容重复
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[norm_title(r["name"])].append(r)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    if dups:
        print()
        print("  ⚠ 标题归一化后重复: %d 组" % len(dups))
        for k, v in dups.items():
            print("     组: %s" % " | ".join(x["name"][:48] for x in v))

    # 4) 质量问题
    bad = [r for r in new if r["error"] or r["han"] < 500]
    print()
    print("  新增文献的可提取汉字分布:")
    buckets = {"0 (扫描版)": 0, "1-499": 0, "500-1999": 0, "2000-7999": 0, "≥8000": 0}
    for r in new:
        h = r["han"]
        if h == 0:
            buckets["0 (扫描版)"] += 1
        elif h < 500:
            buckets["1-499"] += 1
        elif h < 2000:
            buckets["500-1999"] += 1
        elif h < 8000:
            buckets["2000-7999"] += 1
        else:
            buckets["≥8000"] += 1
    for k, v in buckets.items():
        print("     %-12s %d 份" % (k, v))

    if bad:
        print()
        print("  ⚠ 需人工处理（汉字 <500 或读取失败）: %d 份" % len(bad))
        for r in bad:
            print("     %-52s 汉字 %5d  页 %2d  %s" % (
                r["name"][:52], r["han"], r["pages"], r["error"] or "文字极少"))

    print()
    print("  新增文献明细（汉字数降序）:")
    print("     %-56s %7s %5s" % ("文件名", "汉字", "页数"))
    print("     " + "-" * 72)
    for r in sorted(new, key=lambda x: -x["han"]):
        print("     %-56s %7d %5d" % (r["name"][:56], r["han"], r["pages"]))
    return rows


if __name__ == "__main__":
    scan(CN_DIR, "中文文献")
    print()
    scan(EN_DIR, "英文文献")
