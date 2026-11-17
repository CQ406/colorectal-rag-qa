"""生成开题答辩 PPT（10 页 + 结束页）。

设计原则
--------
1. **开题答辩讲「计划」而非「成果」** —— 前期基础单列一页，但措辞用
   「已完成原型与初步实验」，不宣称全部完成（否则评委会问剩余时间做什么）
2. **文字量控制**：每页不超过 5 条要点，每条不超过 30 字，避免小字堆砌
3. **数据只用实测值**：所有数字来自项目实际运行，便于评委追问时有据可答
4. 中文字体显式设置 eastAsia，否则 PowerPoint 可能回退到默认字体
"""
from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE
from pptx.util import Emu, Inches, Pt

import os

# 输出路径允许用环境变量覆盖。
#
# 现实场景：重新生成时 PPT 往往正被 PowerPoint/WPS 打开，
# 直接写原文件会 PermissionError。此时设置 PPT_OUT 指向临时路径，
# 生成完成后再由调用方决定何时覆盖（等用户关闭文件）。
OUT = Path(
    os.environ.get(
        "PPT_OUT",
        r"D:\AI\毕设\开题答辩-基于术中影像与RAG的结直肠手术围术期多模态知识库智能问答系统.pptx",
    )
)

# ---- 配色 ----
NAVY = RGBColor(0x1B, 0x36, 0x5D)       # 主色：深蓝
TEAL = RGBColor(0x17, 0x8A, 0x8A)       # 强调色：青
GREY = RGBColor(0x5A, 0x6A, 0x78)       # 次要文字
LIGHT = RGBColor(0xF2, 0xF6, 0xF9)      # 浅底
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
ACCENT = RGBColor(0xC0, 0x6A, 0x2B)     # 橙色（用于提示/难点）

CN_FONT = "微软雅黑"

W, H = Inches(13.333), Inches(7.5)


def set_font(run, size=None, bold=None, color=None, font=CN_FONT):
    """设置字体，同时指定 eastAsia，保证中文不回退。"""
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    run.font.name = font
    from pptx.oxml.ns import qn
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        rPr.append(ea)
    ea.set("typeface", font)


def add_text(slide, left, top, width, height, text, *, size=16, bold=False,
             color=GREY, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
             line_spacing=1.3, wrap=True):
    """加一个文本框。"""
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = wrap
    # 必须显式禁用自动调整：python-pptx 默认写入 <a:spAutoFit/>，
    # LibreOffice 据此会重新计算文本框几何，导致渲染位置与 XML 坐标不符
    # （实测某文本框 XML 为 x=8.30in，渲染却在 x≈12.4in 处并被裁切）。
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Emu(0)
    tf.margin_top = tf.margin_bottom = Emu(0)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align
        para.line_spacing = line_spacing
        run = para.add_run()
        run.text = line
        set_font(run, size=size, bold=bold, color=color)
    return box


def add_bullets(slide, left, top, width, height, items, *, size=15,
                color=GREY, bullet_color=TEAL, line_spacing=1.45, gap=8):
    """加要点列表。items 中每项可为 (标题, 说明) 元组或纯字符串。"""
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = Emu(0)
    tf.margin_top = tf.margin_bottom = Emu(0)

    for i, item in enumerate(items):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.line_spacing = line_spacing
        para.space_after = Pt(gap)
        if isinstance(item, tuple):
            head, detail = item
            r0 = para.add_run()
            r0.text = "▸ "
            set_font(r0, size=size, bold=True, color=bullet_color)
            r1 = para.add_run()
            r1.text = head
            set_font(r1, size=size, bold=True, color=NAVY)
            if detail:
                r2 = para.add_run()
                r2.text = "　" + detail
                set_font(r2, size=size - 1, color=color)
        else:
            r0 = para.add_run()
            r0.text = "▸ "
            set_font(r0, size=size, bold=True, color=bullet_color)
            r1 = para.add_run()
            r1.text = item
            set_font(r1, size=size, color=color)
    return box


def add_title_bar(slide, text, page_no=None):
    """统一页眉：左侧竖条 + 标题。"""
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.7), Inches(0.52),
                                 Inches(0.075), Inches(0.42))
    bar.fill.solid()
    bar.fill.fore_color.rgb = TEAL
    bar.line.fill.background()
    add_text(slide, Inches(0.9), Inches(0.5), Inches(10.5), Inches(0.5),
             text, size=26, bold=True, color=NAVY)
    if page_no is not None:
        add_text(slide, Inches(12.2), Inches(0.55), Inches(0.6), Inches(0.35),
                 str(page_no), size=12, color=RGBColor(0xAA, 0xB6, 0xC0))


def blank(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def card(slide, left, top, width, height, title, body, *, accent=TEAL):
    """卡片：浅底 + 顶部色条。"""
    box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
    box.fill.solid()
    box.fill.fore_color.rgb = LIGHT
    box.line.color.rgb = RGBColor(0xD8, 0xE2, 0xEA)
    box.line.width = Pt(0.75)
    box.adjustments[0] = 0.06

    strip = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, Inches(0.09))
    strip.fill.solid()
    strip.fill.fore_color.rgb = accent
    strip.line.fill.background()

    add_text(slide, left + Inches(0.22), top + Inches(0.22), width - Inches(0.44),
             Inches(0.35), title, size=14, bold=True, color=NAVY)
    add_text(slide, left + Inches(0.22), top + Inches(0.66), width - Inches(0.44),
             height - Inches(0.85), body, size=11.5, color=GREY, line_spacing=1.35)


def _fix_sldsz_type(prs) -> None:
    """修正 sldSz 的 type 属性。

    python-pptx 的默认模板是 4:3，其 presentation.xml 里写着
    `type="screen4x3"`。修改 slide_width/height 只会改 cx/cy，
    **不会更新 type**，于是出现「尺寸是 16:9、type 却是 4:3」的矛盾。

    LibreOffice 渲染时会参考 type，导致画布尺寸误判、内容被整体缩放。
    实测症状：某形状 XML 坐标 0.70 in，渲染却出现在 1.05 in 处（1.5 倍），
    且右侧内容被裁切。

    cx/cy 是权威尺寸，type 仅用于显示比例提示，直接删除即可。
    """
    from pptx.oxml.ns import qn

    sldSz = prs._element.find(qn("p:sldSz"))
    if sldSz is not None and "type" in sldSz.attrib:
        del sldSz.attrib["type"]


def main() -> int:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    _fix_sldsz_type(prs)

    # ================= 1. 封面 =================
    s = blank(prs)
    bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, W, Inches(3.05))
    bg.fill.solid()
    bg.fill.fore_color.rgb = NAVY
    bg.line.fill.background()

    add_text(s, Inches(0.95), Inches(0.85), Inches(11.4), Inches(0.4),
             "本科毕业设计（论文）开题答辩", size=15, color=RGBColor(0x9C, 0xC8, 0xCB))
    add_text(s, Inches(0.95), Inches(1.35), Inches(11.4), Inches(1.3),
             "基于术中影像与RAG的结直肠手术\n围术期多模态知识库智能问答系统",
             size=31, bold=True, color=WHITE, line_spacing=1.25)

    info = [
        ("答辩人", "程　权"), ("学　号", "2511622"),
        ("学　院", "信息技术学院"), ("专业班级", "计科 8 班"),
        ("指导教师", "徐漫涛"),
    ]
    top = Inches(3.75)
    for label, value in info:
        add_text(s, Inches(1.15), top, Inches(1.5), Inches(0.4),
                 label, size=13, color=RGBColor(0x8A, 0x99, 0xA8))
        add_text(s, Inches(2.65), top, Inches(4.5), Inches(0.4),
                 value, size=14, bold=True, color=NAVY)
        top += Inches(0.52)

    add_text(s, Inches(8.3), Inches(3.9), Inches(4.2), Inches(2.2),
             "开题答辩要点\n\n① 题目价值与创新性\n② 技术路线可行性\n③ 进度安排合理性",
             size=13, color=TEAL, line_spacing=1.6)
    notes(s, "各位老师好，我是计科8班的程权，学号2511622，指导教师是徐漫涛老师。"
             "我的课题是《基于术中影像与RAG的结直肠手术围术期多模态知识库智能问答系统》。"
             "下面我从研究背景、问题提出、技术路线、前期基础和进度安排五个方面汇报，大约12分钟。")

    # ================= 2. 研究背景 =================
    s = blank(prs)
    add_title_bar(s, "一、研究背景", 2)
    add_bullets(s, Inches(0.9), Inches(1.35), Inches(6.1), Inches(4.6), [
        ("结直肠癌疾病负担重", "我国发病率与死亡率均居恶性肿瘤前列，且呈持续上升趋势"),
        ("手术是主要根治手段", "而围术期管理质量直接影响手术安全性与患者远期预后"),
        ("围术期分三个阶段", "术前评估与准备、术中决策与操作、术后并发症防治与康复"),
        ("每个阶段都需循证支持", "涉及的决策点密集，且证据更新快、来源分散"),
    ], size=15)

    card(s, Inches(7.35), Inches(1.35), Inches(5.15), Inches(1.55),
         "证据在哪",
         "诊疗规范、系统评价、随机对照试验、专家共识…\n"
         "仅本课题采集范围内即有 1200 余篇文献",
         accent=TEAL)
    card(s, Inches(7.35), Inches(3.1), Inches(5.15), Inches(1.55),
         "现状的落差",
         "医师在门诊或手术间隙难以完成有效检索；\n"
         "中文提问与英文文献之间存在语言壁垒",
         accent=ACCENT)
    card(s, Inches(7.35), Inches(4.85), Inches(5.15), Inches(1.3),
         "本课题的定位",
         "面向临床教学与科研的可溯源知识服务，\n不构成诊疗建议",
         accent=NAVY)
    notes(s, "先讲为什么做这个题。结直肠癌是我国发病率死亡率都很高的恶性肿瘤，"
             "手术是主要根治手段，而围术期管理直接决定手术安全性。"
             "围术期分术前、术中、术后三个阶段，每个阶段都有大量需要循证依据支撑的决策。"
             "但证据分散在海量文献里，仅我课题的采集范围内就有1200多篇，"
             "医师在门诊或手术间隙很难完成有效检索，而且中文提问和英文文献之间有语言壁垒。"
             "所以本课题的定位是：面向临床教学与科研的可溯源知识服务，不构成诊疗建议。")

    # ================= 3. 问题提出 =================
    s = blank(prs)
    add_title_bar(s, "二、问题提出：三个具体问题", 3)

    problems = [
        ("问题一", "检索效率低",
         "证据分散于诊疗规范、系统评价与随机对照试验等海量文献中，\n"
         "且中文提问与英文文献存在语言壁垒，难以在临床现场快速获取"),
        ("问题二", "大模型回答不可溯源",
         "通用大语言模型知识来自训练语料且存在滞后，医学场景下\n"
         "易生成看似专业、实则缺乏依据的表述，而医学决策对错误容忍度极低"),
        ("问题三", "资料形态单一、缺乏语义关联",
         "现有资料以纯文本为主，未纳入术中影像；\n"
         "疾病、术式、并发症、药物之间的多跳关联难以用文本相似度覆盖"),
    ]
    top = Inches(1.4)
    for tag, title, body in problems:
        box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                 Inches(0.9), top, Inches(11.6), Inches(1.5))
        box.fill.solid()
        box.fill.fore_color.rgb = LIGHT
        box.line.color.rgb = RGBColor(0xD8, 0xE2, 0xEA)
        box.adjustments[0] = 0.08

        badge = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                   Inches(1.1), top + Inches(0.42), Inches(0.95), Inches(0.44))
        badge.fill.solid()
        badge.fill.fore_color.rgb = TEAL
        badge.line.fill.background()
        badge.adjustments[0] = 0.2
        tf = badge.text_frame
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        para = tf.paragraphs[0]
        para.alignment = PP_ALIGN.CENTER
        r = para.add_run(); r.text = tag
        set_font(r, size=12, bold=True, color=WHITE)

        add_text(s, Inches(2.25), top + Inches(0.18), Inches(9.9), Inches(0.38),
                 title, size=16, bold=True, color=NAVY)
        add_text(s, Inches(2.25), top + Inches(0.62), Inches(9.9), Inches(0.75),
                 body, size=12, color=GREY, line_spacing=1.35)
        top += Inches(1.68)
    notes(s, "针对上面说的落差，我把它归纳为三个具体问题。"
             "第一是检索效率低，证据分散在海量文献里，还有语言壁垒。"
             "第二是大模型回答不可溯源，它能生成流畅的回答，但知识来自训练语料、存在滞后，"
             "医学场景下容易生成看似专业实则无据的内容，而一个被编造的数值就可能误导判断。"
             "第三是资料形态单一，现有资料以纯文本为主，没纳入术中影像，"
             "而且疾病、术式、并发症之间的多跳关联，用文本相似度覆盖不了。"
             "这三个问题正好对应我课题的四个模块。")

    # ================= 4. 研究现状 =================
    s = blank(prs)
    add_title_bar(s, "三、国内外研究现状", 4)
    add_bullets(s, Inches(0.9), Inches(1.35), Inches(6.3), Inches(4.8), [
        ("检索增强生成（RAG）",
         "已形成朴素 RAG、进阶 RAG、模块化 RAG 三种范式，检索质量是效果关键"),
        ("医学 RAG 的实证证据",
         "系统评价与元分析表明可提升回答准确性与可靠性，但强调需外部知识约束与可验证评测"),
        ("医学知识图谱",
         "可表达实体间语义关联，支撑多跳查询；共性难点在实体规范化与关系抽取准确性"),
        ("多模态影像解读",
         "已有术中荧光造影的实时 AI 解读系统，验证了术中自动判读的可行性"),
    ], size=14.5)

    card(s, Inches(7.55), Inches(1.4), Inches(5.0), Inches(2.2),
         "现有研究的三点不足",
         "① 中文专科语料匮乏，公开文献库以英文为主\n"
         "② 结论的可溯源性不足，多数系统只给答案不给出处\n"
         "③ 缺少对数值可靠性的校验机制",
         accent=ACCENT)
    card(s, Inches(7.55), Inches(3.8), Inches(5.0), Inches(2.05),
         "本课题的切入点",
         "以「可溯源」为主线：\n"
         "多路召回保证找得到，交叉编码器重排保证排得准，\n"
         "引用校验与数值核验保证说得有据",
         accent=TEAL)
    notes(s, "研究现状我梳理了四条线。RAG 已有三种范式，检索质量是效果关键。"
             "医学 RAG 方面，2025年JAMIA的系统评价和元分析表明它确实能提升准确性与可靠性，"
             "但特别强调必须建立外部知识约束机制和可验证的评测流程。"
             "知识图谱能表达实体关联、支撑多跳查询。"
             "多模态方面已有术中荧光造影的实时AI解读系统，证明术中自动判读可行。"
             "综合起来，现有研究有三点不足：中文专科语料匮乏、结论可溯源不足、"
             "缺少数值可靠性校验。所以我的切入点是围绕「可溯源」这条主线。")

    # ================= 5. 研究目标与内容 =================
    s = blank(prs)
    add_title_bar(s, "四、研究目标与主要研究内容", 5)
    add_text(s, Inches(0.9), Inches(1.1), Inches(11.6), Inches(0.38),
             "目标：构建一个「找得到、排得准、说得有据」的结直肠手术围术期多模态智能问答系统",
             size=14, bold=True, color=TEAL)

    # 六块内容并列：前五块是功能模块，第六块是评测任务。
    #
    # 上一版为腾出评测指标的位置把四模块压成 2×2，结果**影像辅助解读**
    # 被并进「场景应用」的一句话里，等于没有独立呈现。
    # 它是本课题「多模态」定位的落点，必须单独成块，故改为 3×2。
    modules = [
        ("模块一　多模态知识库",
         "文献与指南采集、解析、切分与元数据标引；\n影像库与知识图谱构建\n"
         "产出：带完整元数据的文本块 + 知识图谱"),
        ("模块二　RAG 问答引擎",
         "多路召回、融合重排、答案生成、\n引用溯源与数值核验\n"
         "产出：带可点击引用的答案"),
        ("模块三　影像辅助解读",
         "视觉模型客观描述 → 由描述构造检索 →\n结合文献证据给出辅助解读\n"
         "产出：结论有出处、附合规声明"),
        ("模块四　场景问答与多轮对话",
         "术前/术中/术后分场景问答；\n查询改写与指代解析，支持连续追问\n"
         "产出：三类场景的连续会话"),
        ("模块五　系统管理与用户",
         "用户与权限、知识库维护、\n问答质量监控、运行参数配置\n"
         "产出：可长期运维的管理后台"),
        ("模块六　评测任务与量化指标",
         "语料规模、检索 Recall@5、\n忠实度与引用准确率、消融实验\n"
         "产出：中英文双评测集与消融结论"),
    ]
    left, top = Inches(0.9), Inches(1.58)
    CW, CH, GX, GY = Inches(3.72), Inches(2.24), Inches(0.22), Inches(0.2)
    for i, (title, body) in enumerate(modules):
        col, row = i % 3, i // 3
        x = left + col * (CW + GX)
        y = top + row * (CH + GY)
        accent = ACCENT if i == 5 else (TEAL if i % 2 == 0 else NAVY)
        card(s, x, y, CW, CH, title, body, accent=accent)

    add_text(s, Inches(0.9), Inches(6.55), Inches(11.8), Inches(0.55),
             "说明：模块三为「多模态」定位的落点，独立成模块；"
             "模块六的量化指标为课题验收依据，指标口径与消融设计见第 8 页前期基础。",
             size=10.5, color=GREY, line_spacing=1.3)

    notes(s, "研究内容分六个模块，前五个是功能模块，第六个是评测任务。"
             "模块一多模态知识库；模块二RAG问答引擎；"
             "模块三影像辅助解读——这是本课题多模态定位的落点：系统先让视觉模型做客观描述，"
             "再由描述构造检索查询，结合文献证据给出辅助解读，所有结论必须有出处，不直接下诊断。"
             "模块四是分场景问答与多轮对话，包括查询改写和指代解析。"
             "模块五是系统管理与用户，包括权限、知识库维护、质量监控和参数配置。"
             "模块六是评测任务，语料规模、检索召回、生成忠实度与引用准确率这些量化指标都归在这里，"
             "也是课题的验收依据。")


    # ================= 6. 技术路线 =================
    s = blank(prs)
    add_title_bar(s, "五、技术路线", 6)

    # 离线
    # 离线：知识入库
    add_text(s, Inches(0.9), Inches(1.14), Inches(3.0), Inches(0.32),
             "离线：知识入库", size=13.5, bold=True, color=NAVY)
    # 四步与代码实现一一对应（app/etl/manual_import.py、app/etl/ingest.py）：
    #   ① 中文指南与期刊以 PDF/Markdown 录入（英文文献亦走全文 PDF，
    #      不复用 PubMed 题录——那批记录已下架，仅存摘要无法支撑全文引用）
    #   ② parse_markdown 抽元数据（期刊/年份/证据等级/分期/语种）+ 清洗控制字符
    #   ③ _find_existing 按 PMID→DOI→标题归一化去重；build_chunks 按章节切分并留页码与字符偏移
    #   ④ embed_texts 用 BGE-M3 生成向量；tokenize_for_search 生成中文 bigram
    offline = ["中文指南与期刊\nPDF / Markdown 录入", "结构化解析\n元数据抽取 + 清洗",
               "去重 · 按章节切分\n留页码与字符偏移", "BGE-M3 向量 + 中文 bigram\n写入 PostgreSQL"]
    x = Inches(0.9)
    # 4 个框：宽 2.60 + 间距 0.35，右端落在 12.35 in，留出页边距
    OFF_W, OFF_PITCH = Inches(2.60), Inches(2.95)
    for i, step in enumerate(offline):
        box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(1.5),
                                 OFF_W, Inches(0.82))
        box.fill.solid(); box.fill.fore_color.rgb = LIGHT
        box.line.color.rgb = RGBColor(0xC8, 0xD6, 0xE2); box.adjustments[0] = 0.12
        tf = box.text_frame; tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.word_wrap = True
        for j, line in enumerate(step.split("\n")):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run(); r.text = line
            set_font(r, size=10.5, color=GREY)
        if i < len(offline) - 1:
            add_text(s, x + Inches(2.62), Inches(1.76), Inches(0.32), Inches(0.3),
                     "→", size=13, bold=True, color=TEAL)
        x += OFF_PITCH

    add_text(s, Inches(0.9), Inches(2.38), Inches(11.6), Inches(0.28),
             "　　　　　　　　　　　　　　　　　　↓　实体关系抽取 → Neo4j 知识图谱",
             size=10.5, color=TEAL)

    # 在线一：文本问答
    add_text(s, Inches(0.9), Inches(2.74), Inches(3.2), Inches(0.32),
             "在线：文本问答", size=13.5, bold=True, color=NAVY)
    online = ["用户提问\n查询改写", "多路召回\n向量/关键词/图谱", "RRF 融合\n交叉编码器重排",
              "证据充足？\n否则直接提示", "LLM 生成\n引用+数值校验"]
    x = Inches(0.9)
    # 5 个框：宽 2.05 + 间距 0.37，右端落在 12.63 in
    # （原为 2.35/2.68，第 5 个框右端达 13.97 in，超出 13.33 in 的页宽被裁切）
    ON_W, ON_PITCH = Inches(2.05), Inches(2.42)
    for i, step in enumerate(online):
        is_gate = "充足" in step
        box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(3.08),
                                 ON_W, Inches(0.9))
        box.fill.solid()
        box.fill.fore_color.rgb = ACCENT if is_gate else LIGHT
        box.line.color.rgb = RGBColor(0xC8, 0xD6, 0xE2); box.adjustments[0] = 0.12
        tf = box.text_frame; tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.word_wrap = True
        for j, line in enumerate(step.split("\n")):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run(); r.text = line
            # 深橙底必须配浅色文字；原先统一用灰色，实测对比度不足、几乎不可读
            set_font(r, size=10.5, color=WHITE if is_gate else GREY)
        if i < len(online) - 1:
            add_text(s, x + ON_W + Inches(0.02), Inches(3.38), Inches(0.34), Inches(0.3),
                     "→", size=13, bold=True, color=TEAL)
        x += ON_PITCH

    # 在线二：影像辅助解读（三段式）
    #
    # 影像链路与文本问答不同：它多了一次「视觉模型客观描述」，
    # 且结论必须由文献证据支撑而非由视觉模型直接给出，
    # 因此单独画一条链路，避免和文本问答混为一谈。
    add_text(s, Inches(0.9), Inches(4.2), Inches(3.6), Inches(0.32),
             "在线：影像辅助解读", size=13.5, bold=True, color=NAVY)
    # 影像链路第一步写清「谁在做标注」：
    # 元数据是上传表单里由医生手填（术式/部位/分期/来源），
    # 不是模型自动识别——原来的「元数据标注」容易被误读成 AI 自动标注。
    vision = ["上传手术影像\n标引术式 · 部位 · 分期", "视觉模型\n客观描述（非诊断）",
              "由描述构造\n检索查询", "检索文献证据\n+ 综合解读"]
    x = Inches(0.9)
    for i, step in enumerate(vision):
        is_last = i == len(vision) - 1
        box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(4.54),
                                 OFF_W, Inches(0.86))
        box.fill.solid()
        box.fill.fore_color.rgb = RGBColor(0xE8, 0xF3, 0xF1) if is_last else LIGHT
        box.line.color.rgb = TEAL if is_last else RGBColor(0xC8, 0xD6, 0xE2)
        box.adjustments[0] = 0.12
        tf = box.text_frame; tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.word_wrap = True
        for j, line in enumerate(step.split("\n")):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run(); r.text = line
            set_font(r, size=10.5, color=NAVY if is_last else GREY)
        if i < len(vision) - 1:
            add_text(s, x + Inches(2.62), Inches(4.8), Inches(0.32), Inches(0.3),
                     "→", size=13, bold=True, color=TEAL)
        x += OFF_PITCH

    card(s, Inches(0.9), Inches(5.6), Inches(11.6), Inches(1.35),
         "两条链路共同的约束：结论必须有出处",
         "文本问答：检索最高分低于阈值即返回「证据不足」，不让模型凭内部知识作答。\n"
         "影像解读：视觉模型只转述影像所见，结论性表述必须由检索到的文献证据支撑并标注来源，"
         "输出附合规声明——定位为辅助解读，不是诊断。",
         accent=ACCENT)
    notes(s, "技术路线分离线与在线，在线又有两条链路。"
             "离线知识入库分四步：一是录入，中文指南与中文期刊以 PDF 和结构化 Markdown 录入，"
             "英文文献同样用全文 PDF，不用 PubMed 题录——题录只有摘要，支撑不了全文引用。"
             "二是结构化解析，抽取期刊、年份、证据等级、分期、语种等元数据，并清洗 PDF 带进来的控制字符。"
             "三是去重与切分，按 PMID、DOI、标题归一化三级判重，再按章节切分并保留页码和字符偏移，"
             "这样引用才能定位到原文位置。"
             "四是建立两类索引：BGE-M3 生成向量，同时生成中文 bigram 做关键词倒排，一起写入 PostgreSQL；"
             "另外抽实体关系进 Neo4j 建知识图谱。"
             "在线文本问答：查询改写与分期识别、三路召回、RRF 融合、交叉编码器重排、"
             "判断证据是否充足，再由大模型生成并做引用与数值校验。"
             "在线影像解读：上传影像后先由视觉模型做客观描述，再拿这段描述去检索文献，"
             "最后结合证据给出辅助解读——视觉模型只描述所见，不下结论。"
             "两条链路共用一个约束：证据不足就直说，结论必须能追到出处。")

    # ================= 7. 关键技术与难点 =================
    s = blank(prs)
    add_title_bar(s, "六、关键技术与预期难点", 7)
    add_bullets(s, Inches(0.9), Inches(1.24), Inches(6.3), Inches(5.1), [
        ("多路召回与融合重排",
         "向量+关键词+图谱三路召回，RRF 融合后交叉编码器重排"),
        ("引用溯源",
         "解析答案中的引用编号，剔除模型编造的无效编号并记录"),
        ("数值主张核验",
         "逐个核验答案中的百分比、比值、样本量能否在证据中找到依据"),
        ("多轮查询改写",
         "结合对话历史把「它的发生率是多少」还原为独立可检索的查询"),
        ("影像解读的可溯源性",
         "视觉模型只做客观描述；由描述检索文献，结论必须有出处，不直接下诊断"),
    ], size=13.5, line_spacing=1.4, gap=9)

    card(s, Inches(7.45), Inches(1.35), Inches(5.1), Inches(2.35),
         "预期难点：医学场景的忠实度",
         "初步实验观察到：即使提示词明确禁止，模型仍会编造\n"
         "「看起来专业的精确数值」。\n\n"
         "因此本课题把重心放在结构化校验上，而不是依赖提示词约束。",
         accent=ACCENT)
    card(s, Inches(7.45), Inches(3.95), Inches(5.1), Inches(2.1),
         "应对思路",
         "① 检索层拒绝无据作答\n"
         "② 引用编号校验剔除编造\n"
         "③ 数值逐个回溯证据\n"
         "④ 消融实验量化各环节实际贡献",
         accent=TEAL)
    notes(s, "关键技术四点：多路召回与重排、引用溯源、数值核验、多轮查询改写。"
             "预期难点我放在医学场景的忠实度上。从初步实验看，即使提示词明确禁止，"
             "模型仍会编造看起来专业的精确数值——而临床医师看到具体百分比会本能信任，"
             "这类错误危害最大。所以我的重心是结构化校验，而不是依赖提示词。"
             "应对是四层：检索层拒绝无据作答、引用编号校验、数值回溯、以及用消融实验量化每个环节的贡献。")

    # ================= 8. 前期基础 =================
    s = blank(prs)
    add_title_bar(s, "七、前期基础（已完成原型与初步实验）", 8)

    # 数字取自实测（2026-10 复核）：
    #   上架 109 篇 = 中文 75（指南/共识 30 + 中文论文 45）+ 英文全文 34
    #   文本块 3,772；Neo4j 实体 1,423 / 关系 1,380（10 类关系）
    stats = [
        ("109", "篇", "已入库语料", "中文 75（指南 30）\n英文全文 34"),
        ("3772", "个", "文本块", "元数据完整\n含向量与 bigram"),
        ("1423", "实体", "知识图谱", "1380 条关系\n10 类关系"),
        ("70", "个", "后端接口", "6 个前端功能页\n49 项单元测试通过"),
    ]
    x = Inches(0.9)
    for num, unit, label, detail in stats:
        box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(1.42),
                                 Inches(2.85), Inches(1.95))
        box.fill.solid(); box.fill.fore_color.rgb = LIGHT
        box.line.color.rgb = RGBColor(0xD8, 0xE2, 0xEA); box.adjustments[0] = 0.08
        tf = box.text_frame; tf.word_wrap = True
        tf.margin_left = Inches(0.18); tf.margin_top = Inches(0.14)
        p = tf.paragraphs[0]
        r1 = p.add_run(); r1.text = num
        set_font(r1, size=30, bold=True, color=TEAL)
        r2 = p.add_run(); r2.text = " " + unit
        set_font(r2, size=12, color=GREY)
        p2 = tf.add_paragraph()
        r3 = p2.add_run(); r3.text = label
        set_font(r3, size=12, bold=True, color=NAVY)
        for line in detail.split("\n"):
            p3 = tf.add_paragraph()
            r4 = p3.add_run(); r4.text = line
            set_font(r4, size=9.5, color=GREY)
        x += Inches(3.0)

    add_text(s, Inches(0.9), Inches(3.62), Inches(5.8), Inches(0.35),
             "初步评测结果（自建中英文评测集）", size=13, bold=True, color=NAVY)
    metrics = [
        ("检索 Recall@5", "英文 0.632　中文 0.449"),
        ("生成忠实度", "0.766"),
        ("引用准确率", "0.807"),
        ("无效引用率", "0.000"),
    ]
    top = Inches(4.02)
    for name, value in metrics:
        add_text(s, Inches(1.05), top, Inches(2.6), Inches(0.34),
                 name, size=12, color=GREY)
        add_text(s, Inches(3.7), top, Inches(2.9), Inches(0.34),
                 value, size=12, bold=True, color=TEAL)
        top += Inches(0.4)

    card(s, Inches(7.0), Inches(3.62), Inches(5.55), Inches(2.55),
         "已完成的工作",
         "① 十阶段系统实现全部完成，可端到端运行\n"
         "② 中文指南全文入库，语料含中文文本块 76.5%\n"
         "③ 完成中英文双评测集的检索消融实验\n"
         "④ 完成高相关文献采集与人工整理\n\n"
         "剩余工作：评测深化、论文撰写、系统完善",
         accent=NAVY)
    notes(s, "这一页讲前期基础。目前系统已经可以端到端跑通：121篇精选语料、1720个文本块、"
             "1423个实体和1380条关系的知识图谱，后端60个接口、7个前端页面、49项单元测试通过。"
             "初步评测结果：检索Recall@5英文0.632、中文0.449，生成忠实度0.766，"
             "引用准确率0.807，无效引用率是0——也就是说没有任何一条答案编造了不存在的文献编号。"
             "需要说明的是，这是前期原型与初步实验，剩余工作还有评测深化、论文撰写和系统完善。")

    # ================= 9. 进度安排（按任务书日期） =================
    s = blank(prs)
    add_title_bar(s, "八、进度安排（按任务书阶段与日期）", 9)
    # 8 个阶段严格取自任务书，日期区间不可改写；
    # 第 1、2 阶段已完成，第 3 阶段正在进行。
    plan = [
        ("1", "查阅文献、调研并撰写开题报告", "2026-09-11", "2026-09-30", "done"),
        ("2", "开题答辩", "2026-10-01", "2026-11-07", "done"),
        ("3", "程序设计及调试", "2026-11-08", "2026-12-18", "now"),
        ("4", "中期检查准备，中期答辩", "2026-12-19", "2027-01-16", "todo"),
        ("5", "完善系统功能，完成毕业设计文档撰写", "2027-01-17", "2027-03-19", "todo"),
        ("6", "论文评审、查重，毕业设计答辩", "2027-03-20", "2027-05-14", "todo"),
        ("7", "按答辩意见修改完善并定稿", "2027-05-15", "2027-05-19", "todo"),
        ("8", "毕业设计文档整理归档", "2027-05-20", "2027-05-24", "todo"),
    ]
    top = Inches(1.32)
    for no, content, start, end, state in plan:
        badge = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                   Inches(0.9), top + Inches(0.03),
                                   Inches(0.34), Inches(0.3))
        badge.fill.solid()
        badge.fill.fore_color.rgb = {
            "done": TEAL, "now": ACCENT, "todo": RGBColor(0xC5, 0xCF, 0xD8)
        }[state]
        badge.line.fill.background()
        badge.adjustments[0] = 0.25
        tf = badge.text_frame
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = no
        set_font(r, size=10, bold=True, color=WHITE)

        add_text(s, Inches(1.36), top, Inches(7.15), Inches(0.36),
                 content, size=12, color=NAVY if state != "todo" else GREY)
        add_text(s, Inches(8.65), top, Inches(3.0), Inches(0.36),
                 "%s — %s" % (start, end), size=11, color=GREY)

        tag_text = {"done": "已完成", "now": "进行中", "todo": "计划中"}[state]
        tag_color = {"done": TEAL, "now": ACCENT, "todo": RGBColor(0x9A, 0xA8, 0xB4)}[state]
        add_text(s, Inches(11.95), top, Inches(1.1), Inches(0.36),
                 tag_text, size=10.5, bold=True, color=tag_color)
        top += Inches(0.68)

    add_text(s, Inches(0.9), Inches(6.7), Inches(11.8), Inches(0.4),
             "阶段划分与日期取自任务书，共 8 个阶段；第 3 阶段（程序设计及调试）为本学期主体工作。",
             size=10.5, color=RGBColor(0x8A, 0x99, 0xA8))
    notes(s, "进度安排严格按任务书的 8 个阶段和日期。"
             "第 1 阶段调研与开题报告、第 2 阶段开题答辩已完成；"
             "现在处于第 3 阶段程序设计及调试，到 12 月 18 日；"
             "第 4 阶段中期检查与中期答辩，到明年 1 月 16 日；"
             "第 5 阶段完善功能并完成文档撰写，到 3 月 19 日；"
             "第 6 阶段论文评审查重与毕业答辩，到 5 月 14 日；"
             "最后两个阶段是答辩后修改定稿与文档归档。")

    # ================= 10. 预期成果（按阶段成果逐条列出） =================
    s = blank(prs)
    add_title_bar(s, "九、预期成果（按阶段成果列示）", 10)
    # 审核意见：预期结果须按阶段成果逐条列出，
    # 知识库规模与检索生成指标已移至「研究内容之评测部分」（第 5 页）。
    outcomes = [
        ("开题报告", "选题依据、研究内容、技术路线与进度安排，通过开题答辩"),
        ("外文翻译与文献综述", "完成外文文献翻译；文献综述覆盖 RAG、知识图谱与围术期应用"),
        ("各模块设计、编程与测试", "五个模块完成编码与联调，单元测试与接口测试通过"),
        ("中期报告与中期答辩", "提交中期报告并演示可运行的系统原型"),
        ("毕业论文", "完整阐述系统设计、实现与实验，通过查重与评审"),
        ("作品完善与答辩", "按答辩意见完善系统与文档，定稿并归档"),
    ]
    left, top = Inches(0.9), Inches(1.35)
    for i, (title, body) in enumerate(outcomes):
        col, row = i % 2, i // 2
        x = left + col * Inches(5.9)
        y = top + row * Inches(1.72)
        card(s, x, y, Inches(5.55), Inches(1.5), "%d. %s" % (i + 1, title), body,
             accent=TEAL if i % 2 == 0 else NAVY)

    add_text(s, Inches(0.9), Inches(6.62), Inches(11.8), Inches(0.4),
             "说明：知识库规模与检索、生成指标属研究内容的评测部分，已在第 5 页列示，此处不再重复。",
             size=10.5, color=TEAL)
    notes(s, "预期成果按阶段成果列示，共六项：开题报告、外文翻译与文献综述、"
             "各模块设计编程与测试、中期报告与中期答辩、毕业论文、作品完善与答辩。"
             "知识库规模和检索生成指标属于研究内容的评测部分，放在第 5 页，这里不重复。")


    # ================= 11. 结束页 =================
    s = blank(prs)
    bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, W, H)
    bg.fill.solid(); bg.fill.fore_color.rgb = NAVY
    bg.line.fill.background()
    add_text(s, Inches(0.95), Inches(2.7), Inches(11.4), Inches(0.85),
             "恳请各位老师批评指正", size=36, bold=True, color=WHITE,
             align=PP_ALIGN.CENTER)
    add_text(s, Inches(0.95), Inches(3.75), Inches(11.4), Inches(0.45),
             "基于术中影像与RAG的结直肠手术围术期多模态知识库智能问答系统",
             size=14, color=RGBColor(0x9C, 0xC8, 0xCB), align=PP_ALIGN.CENTER)
    add_text(s, Inches(0.95), Inches(4.5), Inches(11.4), Inches(0.4),
             "程　权　·　信息技术学院　计科 8 班　·　指导教师：徐漫涛",
             size=12.5, color=RGBColor(0x7E, 0x93, 0xA8), align=PP_ALIGN.CENTER)
    notes(s, "谢谢各位老师。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(OUT))
    print("已生成: %s" % OUT)
    print("  幻灯片 %d 页 | 尺寸 %.2f x %.2f 英寸" % (
        len(prs.slides), prs.slide_width / 914400, prs.slide_height / 914400))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
