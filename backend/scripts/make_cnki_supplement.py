"""生成《知网文献补充检索清单》。

目标
----
把知识库的中文语料从当前约 48 篇补到 80 篇以上，其中知网中文期刊文献
需从约 8 篇补到 30 篇以上，即**至少补 22 篇**。

与上次 41 份 PDF 不重复
-----------------------
上次下载的 41 份集中在：诊疗规范、影像与人工智能、ICG 荧光、机器人/NOSES/
微创术式、肝转移、吻合口漏、新辅助等待观察、造口营养、盆腔功能、三维可视化。

本清单刻意避开这些方向，只针对**围术期管理中缺失的主题**：
ERAS、VTE、手术部位感染、镇痛、肠道准备、围术期营养、LARS、术后肠梗阻、
血糖管理、液体治疗、血液管理、预康复、复发性/侧方淋巴结等。

关于文献题录的说明
------------------
本清单提供的是**检索式与筛选标准**，不是文献题录。
唯一列出具体名称的是少数业内广泛引用的指南/共识，
且均标注「题录须在知网核实」——不提供卷、期、页码，避免给出未经核实的书目信息。
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

OUT = Path(r"D:\AI\毕设\知网文献补充检索清单-语料库.docx")

SONG, HEI, TIMES = "宋体", "黑体", "Times New Roman"
NAVY = RGBColor(0x1B, 0x36, 0x5D)
GREY = RGBColor(0x5A, 0x6A, 0x78)
TEAL = RGBColor(0x17, 0x8A, 0x8A)
RED = RGBColor(0xB0, 0x3A, 0x2B)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)


def set_run(run, *, east_asia=SONG, ascii_font=TIMES, size=11,
            bold=False, color=None):
    run.font.name = ascii_font
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = rPr.makeelement(qn("w:rFonts"), {})
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), ascii_font)
    rFonts.set(qn("w:hAnsi"), ascii_font)
    rFonts.set(qn("w:eastAsia"), east_asia)


def para(doc, text="", *, size=11, bold=False, color=None, east_asia=SONG,
         before=0, after=5, line=1.45, indent=0, align=WD_ALIGN_PARAGRAPH.LEFT):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = line
    if indent:
        pf.left_indent = Pt(indent)
    p.alignment = align
    if text:
        r = p.add_run(text)
        set_run(r, east_asia=east_asia, size=size, bold=bold, color=color)
    return p


def h1(doc, text):
    return para(doc, text, size=14, bold=True, color=NAVY, east_asia=HEI,
                before=14, after=6, line=1.3)


def shade(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    el = tcPr.makeelement(qn("w:shd"), {})
    el.set(qn("w:val"), "clear")
    el.set(qn("w:fill"), hex_fill)
    tcPr.append(el)


def table(doc, rows, widths, *, font=9.5, header_fill="1B365D"):
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            cell = t.cell(ri, ci)
            cell.width = Cm(widths[ci])
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.2
            r = p.add_run(str(val))
            set_run(r, east_asia=HEI if ri == 0 else SONG, size=font,
                    bold=(ri == 0), color=WHITE if ri == 0 else RGBColor(0x22, 0x22, 0x22))
        if ri == 0:
            for ci in range(len(row)):
                shade(t.cell(ri, ci), header_fill)
    for ci, w in enumerate(widths):
        for ri in range(len(rows)):
            t.cell(ri, ci).width = Cm(w)

    # 长表格跨页时：首行重复为表头，且任何行不允许被拆到两页
    # （否则会出现续页顶部只有半行内容、却看不到它属于哪一条的情况）
    for ri, row in enumerate(t.rows):
        trPr = row._tr.get_or_add_trPr()
        cant = trPr.makeelement(qn("w:cantSplit"), {})
        trPr.append(cant)
        if ri == 0:
            hdr = trPr.makeelement(qn("w:tblHeader"), {})
            trPr.append(hdr)
    return t


# 20 个主题检索式：序号、主题、目的、CNKI 专业检索式、目标篇数、优先级
TOPICS: list[tuple[str, str, str, str, str, str]] = [
    ("T1", "加速康复外科（ERAS）",
     "围术期管理主线，当前语料完全缺失",
     'SU=("加速康复" + "ERAS" + "快速康复") * SU=("结直肠" + "直肠" + "结肠")',
     "2", "★★★"),
    ("T2", "围术期静脉血栓栓塞症（VTE）",
     "术后致死性并发症，语料空白",
     'SU=("静脉血栓" + "VTE" + "肺栓塞" + "抗凝") * SU=("围手术期" + "围术期" + "术后")',
     "2", "★★★"),
    ("T3", "手术部位感染与抗菌药物预防",
     "围术期感染防控，语料空白",
     'SU=("手术部位感染" + "切口感染" + "SSI") * SU=("结直肠" + "直肠" + "结肠")',
     "2", "★★★"),
    ("T4", "围术期镇痛与多模式镇痛",
     "术后康复关键环节，语料空白",
     'SU=("多模式镇痛" + "术后镇痛" + "加速康复") * SU=("结直肠" + "直肠手术" + "腹部手术")',
     "2", "★★"),
    ("T5", "术前肠道准备",
     "术前准备核心问题，语料空白",
     'SU=("肠道准备" + "机械性肠道准备" + "口服抗生素") * SU=("结直肠" + "直肠" + "结肠手术")',
     "2", "★★★"),
    ("T6", "围术期营养支持",
     "营养风险筛查与营养治疗，语料仅 1 篇种子文献",
     'SU=("营养风险筛查" + "肠内营养" + "营养支持") * SU=("围手术期" + "结直肠" + "术后")',
     "2", "★★★"),
    ("T7", "低位前切除综合征（LARS）",
     "直肠癌术后特有功能并发症，语料空白",
     'SU=("低位前切除综合征" + "LARS" + "排便功能障碍") * SU=("直肠癌" + "直肠手术")',
     "2", "★★"),
    ("T8", "术后肠梗阻与胃排空障碍",
     "常见术后并发症，语料空白",
     'SU=("术后肠梗阻" + "胃排空障碍" + "胃瘫") * SU=("结直肠" + "腹部手术" + "术后")',
     "1", "★★"),
    ("T9", "围术期血糖管理",
     "合并糖尿病患者的围术期风险，语料空白",
     'SU=("血糖管理" + "糖尿病" + "高血糖") * SU=("围手术期" + "结直肠手术" + "术后")',
     "1", "★★"),
    ("T10", "围术期液体治疗",
     "目标导向液体治疗与容量管理，语料空白",
     'SU=("目标导向液体治疗" + "液体管理" + "容量治疗") * SU=("围手术期" + "腹部手术" + "结直肠")',
     "1", "★★"),
    ("T11", "围术期血液管理与输血",
     "贫血纠正与限制性输血，语料空白",
     'SU=("围手术期血液管理" + "限制性输血" + "术后贫血") * SU=("结直肠" + "外科手术" + "术后")',
     "1", "★"),
    ("T12", "吻合口漏预测模型",
     "补足吻合口漏的量化评估视角（已有共识但缺预测研究）",
     'SU=("吻合口漏" + "吻合口瘘") * SU=("预测模型" + "列线图" + "危险因素" + "nomogram")',
     "2", "★★★"),
    ("T13", "新辅助治疗与病理完全缓解",
     "已有等待观察共识，缺放化疗方案与疗效评估；注意排除已下载的《等待观察策略共识》",
     'SU=("新辅助" + "术前放化疗") * SU=("直肠癌") * SU=("疗效" + "病理完全缓解" + "pCR")',
     "2", "★★"),
    ("T14", "侧方淋巴结清扫",
     "直肠癌术式争议点，语料空白",
     'SU=("侧方淋巴结" + "侧方清扫") * SU=("直肠癌" + "中低位直肠")',
     "1", "★★"),
    ("T15", "中低位直肠癌保肛手术",
     "保肛决策与术式选择，语料仅间接涉及",
     'SU=("保肛" + "括约肌间切除" + "ISR" + "超低位") * SU=("直肠癌" + "中低位直肠")',
     "2", "★★"),
    ("T16", "预康复（Prehabilitation）",
     "围术期干预窗口前移，语料仅 1 篇种子文献",
     'SU=("预康复" + "术前预康复" + "prehabilitation") * SU=("结直肠" + "腹部手术")',
     "1", "★★"),
    ("T17", "术后早期活动与出院标准",
     "ERAS 落地环节，语料空白",
     'SU=("早期下床活动" + "早期活动" + "出院标准") * SU=("结直肠" + "术后" + "加速康复")',
     "1", "★"),
    ("T18", "围术期护理路径与专科护理",
     "护理侧证据，可提升系统对护理问题的覆盖",
     'SU=("围手术期护理" + "专科护理" + "护理路径") * SU=("结直肠癌" + "直肠癌" + "结肠癌")',
     "1", "★"),
    ("T19", "术后随访与复发监测",
     "补足长期管理证据",
     'SU=("随访" + "复发监测" + "复查") * SU=("结直肠癌" + "直肠癌") * SU=("术后")',
     "2", "★★"),
    ("T20", "国内多中心临床研究",
     "优先补权威证据等级，改善证据结构",
     'SU=("多中心" + "前瞻性" + "随机对照") * SU=("结直肠癌" + "直肠癌") * SU=("手术" + "围手术期")',
     "2", "★★★"),
]

# 候选权威指南（仅名称线索，题录须核实）
GUIDE_HINTS: list[tuple[str, str, str]] = [
    ("T1", "中国加速康复外科临床实践指南", "中华医学会外科学分会与麻醉学分会联合发布，含结直肠章节"),
    ("T2", "围手术期静脉血栓栓塞症预防中国专家共识", "另有《中国骨科大手术VTE预防指南》可参照方法学"),
    ("T3", "中国手术部位感染预防指南", "含结直肠手术专项建议"),
    ("T5", "中国消化内镜诊疗相关肠道准备指南", "含结直肠手术肠道准备相关内容"),
    ("T6", "结直肠癌围手术期营养治疗专家共识", "中华医学会肠外肠内营养学分会方向"),
    ("T12", "中国直肠癌手术吻合口漏诊断、预防及处理专家共识", "已下载 2019 版，可另查更新版本"),
]


def build() -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Cm(29.7), Cm(21.0)
    sec.left_margin = sec.right_margin = Cm(1.8)
    sec.top_margin = sec.bottom_margin = Cm(1.6)

    style = doc.styles["Normal"]
    style.font.name = TIMES
    style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), SONG)

    para(doc, "知网文献补充检索清单", size=19, bold=True, color=NAVY,
         east_asia=HEI, after=2, align=WD_ALIGN_PARAGRAPH.CENTER)
    para(doc, "用于补充知识库中文语料（目标：知网中文期刊文献 ≥30 篇）",
         size=11, color=GREY, after=2, align=WD_ALIGN_PARAGRAPH.CENTER)
    para(doc, "与已下载的 41 份 PDF 无重复　·　2026-10",
         size=10, color=GREY, after=10, align=WD_ALIGN_PARAGRAPH.CENTER)

    # ===== 一 =====
    h1(doc, "一、检索前必须设置的三个条件")
    rows = [
        ["设置项", "取值", "理由"],
        ["来源类别", "勾选「北大核心」「CSSCI」「CSCD」",
         "保证文献质量；三选二即可，全勾可能过滤掉部分专业期刊"],
        ["发表时间", "2021 — 2026",
         "与开题报告「近五年」的要求一致；个别经典指南可放宽至 2019"],
        ["文献类型", "优先「期刊」；可含「会议论文」「学位论文」",
         "期刊为主；学位论文可作为补充，但需注意质量参差"],
    ]
    table(doc, rows, [4.0, 9.0, 12.0], font=9.5)

    para(doc, "检索入口：中国知网首页 →「高级检索」→ 切换为「专业检索」，"
              "把下表的检索式整段粘贴进去。若专业检索语法报错，"
              "可改用「主题」字段逐词组合，效果相近。",
         size=10, color=GREY, before=6, after=4, indent=12)

    # ===== 二 =====
    h1(doc, "二、主题检索式（20 条，目标合计约 30 篇）")
    para(doc, "优先级说明：★★★ 为必补（围术期核心主题且当前完全空白）；"
              "★★ 为应补；★ 为可选。建议先做 ★★★ 六条，可覆盖约 13 篇。",
         size=10, color=GREY, after=6, indent=12)

    rows = [["编号", "主题", "为什么补", "CNKI 专业检索式", "篇数", "优先"]]
    for code, topic, why, expr, n, prio in TOPICS:
        rows.append([code, topic, why, expr, n, prio])
    table(doc, rows, [1.3, 4.6, 5.4, 12.0, 1.2, 1.5], font=8.5)

    # ===== 三 =====
    h1(doc, "三、筛选标准（按此顺序取舍）")
    crit = [
        "1. 证据等级优先：指南/专家共识 > 系统评价/Meta 分析 > 多中心随机对照试验 > 前瞻性队列 > 单中心回顾性研究。",
        "2. 主题必须落在围术期的术前、术中或术后三个阶段之一；纯基础研究、纯分子机制研究不收。",
        "3. 排除个案报道、短篇报道、无摘要的会议摘要、重复发表的同一研究。",
        "4. 同一主题不要超过 2 篇，避免语料结构失衡。",
        "5. 期刊优先选择《中华外科杂志》《中华胃肠外科杂志》《中华消化外科杂志》"
        "《中国实用外科杂志》《中华医学杂志》等核心期刊。",
        "6. 下载前先看摘要：摘要中没有具体数据（样本量、发生率、疗程等）的，"
        "对问答系统价值低，不建议收。",
    ]
    for c in crit:
        para(doc, c, size=10.5, indent=14, after=4)

    # ===== 四 =====
    h1(doc, "四、候选权威指南线索（名称供定位，题录须核实）")
    para(doc, "下列名称是这些主题下业内引用较多的指南/共识，可作为检索起点。"
              "**请在知网核实完整题录后再下载**——本清单不提供卷、期、页码，"
              "以免给出未经核对的书目信息。若检索不到，说明该指南可能未在知网收录，"
              "可改从期刊官网或中华医学期刊网获取。",
         size=10, color=RED, after=6, indent=12)

    rows = [["对应主题", "指南/共识名称（供检索定位）", "备注"]]
    for code, name, note in GUIDE_HINTS:
        rows.append([code, name, note])
    table(doc, rows, [3.0, 10.0, 12.0], font=9)

    # ===== 五 =====
    h1(doc, "五、下载与入库规范")
    spec = [
        ("文件命名", "直接用文献完整题名，去掉文件名中的 / \\ : * ? \" < > | 等字符。"
                     "例：加速康复外科在结直肠癌手术中的应用研究.pdf"),
        ("存放位置", "统一放入 D:\\AI\\毕设\\PDF\\。若该目录已有同名文件，说明已下载过，跳过即可。"),
        ("格式要求", "必须是**可复制文字的 PDF**。扫描版（整页是图片）无法提取文字，"
                     "导入会失败。下载后先用 PDF 阅读器试着选中文字，选不中就是扫描版。"),
        ("质量自查", "打开确认：①能选中文字 ②页数完整 ③不是仅首页预览版。"),
    ]
    rows = [["项目", "要求"]]
    for k, v in spec:
        rows.append([k, v])
    table(doc, rows, [3.5, 21.5], font=9.5)

    para(doc, "下载完成后告诉我文件数量，我会运行解析脚本核对每篇的可提取汉字数，"
              "并列出不合格（扫描版、乱码、缺页）的文件供你替换。",
         size=10, color=TEAL, before=6, after=4, indent=12)

    # ===== 六 =====
    h1(doc, "六、下载登记表（可选，便于核对题录）")
    para(doc, "建议下载时顺手填写，后续生成参考文献时可直接使用，避免再回头核对。",
         size=10, color=GREY, after=5, indent=12)
    rows = [["序号", "题名", "第一作者", "期刊", "年", "卷(期): 页", "对应主题", "备注"]]
    for i in range(1, 31):
        rows.append([str(i), "", "", "", "", "", "", ""])
    table(doc, rows, [1.2, 8.0, 2.6, 3.4, 1.3, 3.4, 2.2, 2.9], font=8)

    return doc


def main() -> int:
    doc = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    total = sum(int(n) for *_, n, __ in [(t[0], t[1], t[2], t[3], t[4], t[5]) for t in TOPICS])
    print("已生成: %s" % OUT)
    print("  主题数 %d | 目标篇数合计 %d | 表格 %d 个 | 大小 %.1f KB" % (
        len(TOPICS), total, len(doc.tables), OUT.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
