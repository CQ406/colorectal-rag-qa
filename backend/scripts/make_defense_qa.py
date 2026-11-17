"""生成开题答辩问答手册（DOCX）。

内容取自项目实际数据与前期实验结论，每条回答都给到可直接照说的完整话术，
并标注该问题考察什么、以及哪些说法会踩坑。

设计考虑
--------
1. **按考察意图分类**，而非按知识点 —— 答辩时评委的追问路径是
   「价值 → 可行性 → 工作量 → 数据」，按此顺序编排便于临场翻找
2. **敏感问题单列一章**，因为这类问题最需要提前想清楚而不是临场编
3. **附「必须记住的数字」与「临场检查清单」** —— 这两页是答辩前十分钟真正会看的内容
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

OUT = Path(r"D:\AI\毕设\开题答辩问答手册.docx")

SONG, HEI, TIMES = "宋体", "黑体", "Times New Roman"
NAVY = RGBColor(0x1B, 0x36, 0x5D)
GREY = RGBColor(0x5A, 0x6A, 0x78)
TEAL = RGBColor(0x17, 0x8A, 0x8A)
RED = RGBColor(0xB0, 0x3A, 0x2B)


def set_run(run, *, ascii_font=TIMES, east_asia=SONG, size=12, bold=False, color=None):
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


def para(doc, text="", *, size=12, bold=False, color=None, east_asia=SONG,
         ascii_font=TIMES, first_indent=0, before=0, after=6,
         line=1.5, align=WD_ALIGN_PARAGRAPH.LEFT):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = line
    if first_indent:
        pf.first_line_indent = Pt(first_indent)
    p.alignment = align
    if text:
        r = p.add_run(text)
        set_run(r, ascii_font=ascii_font, east_asia=east_asia,
                size=size, bold=bold, color=color)
    return p


def h1(doc, text):
    p = para(doc, text, size=15, bold=True, color=NAVY, east_asia=HEI,
             before=16, after=8, line=1.4)
    return p


def h2(doc, text):
    return para(doc, text, size=12.5, bold=True, color=NAVY, east_asia=HEI,
                before=10, after=4, line=1.4)


def question(doc, idx, text):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(10)
    pf.space_after = Pt(3)
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.4
    r0 = p.add_run("Q%d　" % idx)
    set_run(r0, east_asia=HEI, size=12, bold=True, color=TEAL)
    r1 = p.add_run(text)
    set_run(r1, east_asia=HEI, size=12, bold=True, color=NAVY)
    return p


def answer(doc, text, *, label="回答"):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(4)
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.5
    pf.left_indent = Pt(18)
    r0 = p.add_run("【%s】" % label)
    set_run(r0, east_asia=HEI, size=11, bold=True, color=GREY)
    r1 = p.add_run(text)
    set_run(r1, size=12, color=RGBColor(0x22, 0x22, 0x22))
    return p


def tip(doc, text, *, label="加分点", color=TEAL):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(8)
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.45
    pf.left_indent = Pt(18)
    r0 = p.add_run("【%s】" % label)
    set_run(r0, east_asia=HEI, size=10.5, bold=True, color=color)
    r1 = p.add_run(text)
    set_run(r1, size=10.5, color=color)
    return p


def table(doc, rows, widths_cm, *, header=True, font=10.5):
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ri, row in enumerate(rows):
        for ci, cell_text in enumerate(row):
            cell = t.cell(ri, ci)
            cell.width = Cm(widths_cm[ci])
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.25
            r = p.add_run(str(cell_text))
            set_run(r, east_asia=HEI if (header and ri == 0) else SONG,
                    size=font, bold=(header and ri == 0),
                    color=WHITE if (header and ri == 0) else RGBColor(0x22, 0x22, 0x22))
            if header and ri == 0:
                sh = cell._tc.get_or_add_tcPr()
                fill = sh.makeelement(qn("w:shd"), {})
                fill.set(qn("w:val"), "clear")
                fill.set(qn("w:fill"), "1B365D")
                sh.append(fill)
    for ri in range(len(rows)):
        for ci in range(len(rows[0])):
            t.cell(ri, ci).width = Cm(widths_cm[ci])
    return t


WHITE = RGBColor(0xFF, 0xFF, 0xFF)


def build() -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.4)
    sec.top_margin = sec.bottom_margin = Cm(2.3)

    style = doc.styles["Normal"]
    style.font.name = TIMES
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), SONG)

    # ===== 标题 =====
    para(doc, "开题答辩问答手册", size=20, bold=True, color=NAVY,
         east_asia=HEI, after=2, align=WD_ALIGN_PARAGRAPH.CENTER)
    para(doc, "基于术中影像与RAG的结直肠手术围术期多模态知识库智能问答系统",
         size=11, color=GREY, after=2, align=WD_ALIGN_PARAGRAPH.CENTER)
    para(doc, "程　权　·　信息技术学院　计科 8 班　·　指导教师：徐漫涛",
         size=10, color=GREY, after=14, align=WD_ALIGN_PARAGRAPH.CENTER)

    para(doc, "使用说明：本手册按评委的追问路径编排——先问价值、再问可行性、"
              "然后查工作量、最后看数据。每条给出可直接照说的完整话术；"
              "标【加分点】的可选择性展开，标【避坑】的务必回避。",
         size=10.5, color=GREY, first_indent=21, after=10)

    # ===== 一 =====
    h1(doc, "一、开场与总体印象")
    question(doc, 1, "请用两分钟介绍一下你的课题。")
    answer(doc, "我的课题是做一个面向结直肠手术围术期的智能问答系统。"
                "之所以做这个方向，是因为围术期的循证知识分散在海量文献里，"
                "医师在门诊或手术间隙很难快速检索，而通用大模型虽然能生成流畅回答，"
                "但无法溯源、还会编造数值，在医学场景风险很高。"
                "所以我用检索增强生成（RAG）的技术路线：先建一个经过筛选的专科知识库，"
                "再用它约束模型生成，让每处结论都能回溯到具体文献，"
                "并额外做了引用编号校验和数值核验两道防线。"
                "同时接入多模态模型，使术中影像也能得到有文献依据的辅助解读。"
                "目前系统已可端到端运行，完成了初步的检索与生成评测。")
    tip(doc, "开场控制在 90~120 秒，务必在最后一句提到「已可运行 + 已完成初步评测」，"
             "这会让后面所有质疑的起点从「能不能做」变成「做得怎么样」。")

    question(doc, 2, "你的汇报里哪些是已完成、哪些是计划？")
    answer(doc, "已完成的是前期原型与初步实验：系统六个模块可端到端运行，"
                "语料已建成，检索与生成的初步指标已经测出来。"
                "接下来一年主要做三件事：一是评测深化，包括更大规模评测集和消融实验；"
                "二是系统完善，特别是影像模块的稳定性；三是论文撰写。"
                "所以我今天汇报的是「计划 + 前期基础」，不是「已完成」的成果。")
    tip(doc, "这一问几乎必被问。明确区分「已完成」与「计划」是诚信问题，"
             "也避免评委认为你一年无事可做。", color=RED)

    # ===== 二 =====
    h1(doc, "二、题目价值与创新性")
    question(doc, 3, "这个题目的创新点在哪里？")
    answer(doc, "我把它概括为三点。第一是中文专科语料——公开文献库以英文为主，"
                "而且我实测确认 PubMed 对非英文文献不提供任何中文正文，"
                "所以中文语料只能靠自己整理。我把 41 份中文指南和共识的 PDF 全文"
                "整理入库，现在语料中含中文的文本块占比达到 76.5%，"
                "中文提问的前 10 条结果里都能命中中文文献。"
                "第二是可溯源——不只是给引用编号，还会校验编号是否真实存在，"
                "并逐个核验答案里的数值能否在证据中找到依据。"
                "第三是多模态——把术中影像的客观描述与文献证据结合，"
                "而不是让多模态模型直接下诊断结论。")

    question(doc, 4, "为什么不直接用 ChatGPT 或现成的大模型？")
    answer(doc, "三个理由。第一是知识来源问题，通用模型的医学知识来自训练语料、"
                "存在滞后，而且无法给出可核查的出处。第二是它不会承认不知道——"
                "在我的实测里，即使提示词明确禁止，模型仍会编造看起来专业的精确数值，"
                "而临床医师看到具体百分比会本能信任，这类错误危害最大。"
                "第三是专科深度，通用模型对结直肠手术围术期的具体术式、"
                "吻合口漏的处理流程这类细节，回答往往笼统。"
                "RAG 的思路是用外部证据约束它，本质是把「记忆」换成「查证」。")

    question(doc, 5, "医学知识更新很快，你的知识库怎么跟上？")
    answer(doc, "这也是我选 RAG 而不是微调的原因之一——RAG 的知识是可替换的，"
                "更新语料不需要重新训练模型。系统里做了三层支持："
                "一是可以按检索式自动从 PubMed 增量采集；"
                "二是知识库维护模块支持逐篇审核、批量上下架，并有批次管理记录每次更新的参数和结果；"
                "三是人工录入通道，新增中文指南只要放一个 Markdown 文件就能导入，"
                "重复导入会更新而不是产生重复条目。")

    # ===== 三 =====
    h1(doc, "三、技术可行性")
    question(doc, 6, "文献和影像的版权问题怎么处理？")
    answer(doc, "文献走两个合规渠道：英文文献通过 PubMed 官方接口采集题录与摘要，"
                "只保存元数据和摘要，不保存全文；中文指南是我自己整理的文本，"
                "来源是公开出版的指南和共识。影像用的是公开样例数据，不使用真实患者影像。"
                "系统定位明确写的是「面向临床教学与科研的可溯源知识服务」，"
                "不构成诊疗建议——这一点我在文献综述和系统界面里都写明了。")

    question(doc, 7, "你的电脑跑得动这些模型吗？")
    answer(doc, "可以。硬件是 RTX 5060 Laptop 显卡 8GB 显存。"
                "检索用的两个模型都在本地部署：BGE-M3 负责向量编码，"
                "bge-reranker-v2-m3 负责重排，加载时间分别约 4.4 秒和 5.7 秒。"
                "答案生成和影像解读调用云端接口，不在本地跑。"
                "这样显存压力可控，而且检索是最频繁的操作，本地化能保证响应速度。")

    question(doc, 8, "为什么用 RAG 而不是微调？")
    answer(doc, "四个原因。第一，微调需要大量标注数据，医学领域标注成本极高。"
                "第二，微调后知识仍然固化在参数里，更新要重新训练，"
                "而指南可能一两年就更新一版。第三，微调无法给出处，"
                "而本课题最核心的要求恰恰是可溯源。"
                "第四，微调容易导致模型在训练数据上过拟合，"
                "遇到训练分布外的问题时可能比原模型更差。"
                "RAG 把「知识」和「能力」解耦，更适合知识频繁更新的医学场景。")

    question(doc, 9, "中文提问检索英文文献，效果怎么保证？")
    answer(doc, "这是本课题的技术难点之一，我的做法和实测结果是这样："
                "向量模型 BGE-M3 本身支持多语言，中文查询和英文文献在同一个语义空间里，"
                "这是跨语言检索的基础。但只靠向量不够，所以我加了交叉编码器重排，"
                "重排会把查询和候选文档拼在一起精算相关性，"
                "实测重排让英文召回率 Recall@5 提升了 18.4 个百分点。"
                "另外中文这边我通过人工整理建成了中文全文语料，"
                "现在中文提问基本都能命中中文文献，避免全部依赖跨语言。")
    tip(doc, "如果被追问「跨语言到底差多少」，如实回答：中文 Recall@5 是 0.449，"
             "英文是 0.632，跨语言确实是当前的主要损失来源。"
             "承认差距比硬说「没问题」可信。", color=RED)

    question(doc, 10, "系统架构为什么选这些技术？")
    answer(doc, "后端用 Python 加 FastAPI，因为检索和模型推理的生态都在 Python 侧，"
                "FastAPI 原生支持异步，适合流式返回。"
                "存储用 PostgreSQL 加 pgvector 扩展，把关系数据和向量放在同一个库里，"
                "避免跨库一致性问题；图谱用 Neo4j，因为多跳关系查询用图数据库更自然；"
                "Redis 做缓存。三个组件都通过容器部署，保证环境一致。"
                "前端用 Vue 3 加 TypeScript，主要考虑是开发效率和类型安全。")

    # ===== 四 =====
    h1(doc, "四、工作量与进度")
    question(doc, 11, "时间够吗？一年能做完吗？")
    answer(doc, "我判断够，因为前期已经把风险最高的部分验证过了。"
                "检索链路、生成链路、影像链路都已经跑通，剩下的不是「能不能做」"
                "而是「做到多好」的问题。具体来说，剩下的是评测深化和论文撰写，"
                "这两项风险可控。而且我在进度计划里把中期检查放在第 8 周，"
                "留了缓冲期；如果某个模块延期，可以压缩评测的扩展部分而不影响主线。")

    question(doc, 12, "最难的部分是什么？")
    answer(doc, "最难的是医学场景的忠实度，也就是怎么保证模型不说错。"
                "我做过一个对照实验：同一批题，一组用普通提示词，"
                "一组用明确禁止编造的加固提示词，结果数值编造的比例不但没降，"
                "反而从 22% 升到了 26%，个别编造案例在后一次运行中逐字重现。"
                "这说明提示词工程解决不了这个问题。"
                "所以我把重心转到结构化校验：引用编号校验、数值逐个回溯证据、"
                "以及证据不足时不调用模型。这是我目前认为最有效的方向。")
    tip(doc, "这是全篇最有说服力的一问——因为它展示了「提出假设 → 实验证伪 → 修正方案」"
             "的完整科研过程，而不是罗列功能。", color=TEAL)

    question(doc, 13, "你一个人做这么多模块，会不会摊得太薄？")
    answer(doc, "四个模块里，知识库、问答引擎和系统管理是紧密耦合的一条主线，"
                "已经打通；影像模块是相对独立的分支，接口化的，"
                "如果时间紧张可以降级为「只输出客观描述、不结合文献」的简化版本，"
                "不影响主线完整性。所以风险是有预案的。")

    # ===== 五 =====
    h1(doc, "五、数据与实验")
    question(doc, 14, "你的评测集是怎么构建的？会不会自己出题自己考？")
    answer(doc, "这是评测最容易出问题的地方，我是这样处理的。"
                "评测集是从语料里的文本块反向生成问题，该文本块就是标准答案来源，"
                "所以检索指标是完全客观、可复现的。"
                "但关键在于防词汇泄漏——如果直接让模型「根据这段文字提问」，"
                "它会逐字摘抄原文术语，导致关键词检索虚高、评测失去意义。"
                "所以我在提示词里强制要求换用同义表达和口语化提问。"
                "另外检索指标和生成指标分开评：检索只看能不能命中标准块，"
                "生成才用大模型裁判，并且我人工复核过裁判结论。")
    tip(doc, "「有标准答案来源」和「防词汇泄漏」这两句必须说出来，"
             "它们直接回应了「自建评测集不客观」的质疑。")

    question(doc, 15, "Recall@5 为什么只有 0.45？是不是系统不行？")
    answer(doc, "这个数字要放在口径下看。我的评测是单答案标注——"
                "也就是一道题只标一个标准文本块，指标是「这个块有没有进前 5」。"
                "但一个临床问题的答案常常分布在多个文本块，"
                "比如「吻合口漏怎么预防」，指南在术前、术中、术后三节都有内容，"
                "命中其中任意一节其实都能答对，但只有标的那一节算分。"
                "所以这个指标是召回率的下界，实际可用性高于数字所示。"
                "英文是 0.632，中文 0.449 偏低，主要差在跨语言语义匹配上。")

    question(doc, 16, "你做消融实验了吗？结论是什么？")
    answer(doc, "做了，四轮。最重要的结论是：交叉编码器重排是唯一显著有效的单项优化，"
                "中文 Recall@5 提升 10.1 个百分点、英文提升 18.4 个百分点。"
                "第二个发现是关键词检索对自然语言提问几乎无效，"
                "中文 Recall@5 只有 0.043，英文 0.081，而且会拖累融合结果，"
                "所以我把融合权重从 0.7 降到了 0.3。"
                "第三个发现比较反直觉：知识图谱对中文有帮助，但对英文反而有害，"
                "英文场景下让融合结果下降了 12.5 个百分点，"
                "原因是实体是用中文抽取的，英文查询要跨语言匹配中文实体名。"
                "第四个是证据等级加权会损害召回，但它的设计目标本来就不是提升召回，"
                "而是让前列结果中高等级证据占比更高，所以应该换指标评估。")
    tip(doc, "第三个发现（图谱对英文有害）是加分项——它证明你在做真实测量并如实报告，"
             "而不是为每个模块都说好话。", color=TEAL)

    question(doc, 17, "生成质量怎么评的？")
    answer(doc, "两个层面。一是自动指标：引用准确率 0.807，无效引用率是 0.000，"
                "也就是没有任何一条答案编造了不存在的文献编号。"
                "二是数值核验：我把答案里的百分比、比值、样本量逐个回溯证据全文，"
                "175 处数值里捕获了 8 处无据数值，错误率 4.6%——"
                "而这 8 处大模型裁判并没有指出来，说明结构化校验比裁判更可靠。"
                "忠实度用大模型裁判打的，0.766，并且我人工复核过一部分样本。")

    # ===== 六 =====
    h1(doc, "六、可能被追问的敏感问题")
    question(doc, 18, "你的语料只有 120 篇，够吗？")
    answer(doc, "我理解这个疑问。我的判断是「够用，且质量优先」。"
                "120 篇里临床指南和专家共识占 50%，近三年文献占 69%，"
                "覆盖结直肠手术围术期的主要决策点。"
                "相比之下，如果为了凑数量把大量相关性低的文献放进去，"
                "反而会稀释检索精度——我在前期做过语料扩充，"
                "从 444 篇扩到 1263 篇时，指标并没有随之提升。"
                "所以最终我选择在质量控制下精选，而不是追求数量。"
                "当然如果后续评测发现某个主题覆盖不足，我会针对性补充。")
    tip(doc, "不要在数量上纠缠。把话题拉回「质量结构」和「已经做过的对照」，"
             "并主动给出可补充的路径。", color=RED)

    question(doc, 19, "你的系统能替代医生吗？出了问题谁负责？")
    answer(doc, "不能，也不应该这样定位。系统给的是带出处的文献证据，"
                "帮助医师快速定位依据，最终判断权在医师。"
                "我在设计上做了三重限制：一是证据不足时直接说明而不是猜；"
                "二是每处结论都必须有来源，无法溯源的内容不输出；"
                "三是影像解读只输出客观描述，结论性表述必须由文献支撑。"
                "所以它的定位是「知识检索与教学辅助工具」，不参与诊疗决策。")

    question(doc, 20, "如果评测发现你的方案不如直接用大模型，你怎么处理？")
    answer(doc, "那我会如实报告结论并分析原因。科研上负面结论也是有价值的，"
                "关键是要说清楚在什么条件下不成立。"
                "不过从目前的数据看，RAG 在可溯源性和数值可靠性上是有明确优势的——"
                "通用大模型无法给出一一对应的引用，"
                "而我的系统无效引用率是 0。"
                "如果后续发现某个场景下直接用模型更好，我会在论文里给出适用边界。")

    question(doc, 21, "知识图谱在你的系统里到底有没有用？")
    answer(doc, "诚实说，分语言看。中文场景下它和向量检索基本持平，没有拖累；"
                "英文场景下它明显有害，让 Recall@5 下降了 12.5 个百分点。"
                "原因是图谱的实体是用中文抽取的，英文查询需要跨语言匹配中文实体名。"
                "我的处理是把图谱定位为补充召回源而不是主力，"
                "权重设为 0.6，并在融合阶段让重排来纠偏。"
                "后续计划是做双语实体对齐，这样图谱对英文也能起作用。")
    tip(doc, "明确承认「有用但有条件」，并给出改进路径。"
             "比硬说「很有用」安全得多。", color=RED)

    # ===== 七 =====
    h1(doc, "七、必须记住的数字")
    para(doc, "答辩时被问到具体数字却答不出，会显著削弱前面所有论述的可信度。"
              "以下数字必须记牢。", size=10.5, color=GREY, first_indent=21, after=8)
    table(doc, [
        ["项目", "数值", "被问到的概率"],
        ["语料规模", "121 篇（指南 60 篇、中文全文 48 篇）", "高"],
        ["文本块", "约 1720 个", "中"],
        ["知识图谱", "1423 实体 / 1380 关系", "中"],
        ["系统规模", "60 个后端接口、7 个前端页面、49 项单元测试通过", "低"],
        ["检索 Recall@5", "英文 0.632 / 中文 0.449", "高 ★"],
        ["重排带来的提升", "中文 +10.1pp / 英文 +18.4pp", "高"],
        ["生成忠实度", "0.766", "中"],
        ["引用准确率", "0.807；无效引用率 0.000", "高 ★"],
        ["数值核验", "175 处中捕获 8 处无据数值（4.6%）", "中"],
        ["提示词加固效果", "数值编造 22% → 26%（不降反升）", "高"],
        ["中文文本块占比", "76.5%（整理前 0.2%）", "高 ★"],
        ["评测集规模", "中文 138 题 / 英文 136 题", "中"],
        ["检索延迟", "约 3.2 秒（含重排）", "低"],
    ], [3.6, 9.0, 3.6], font=10)

    # ===== 八 =====
    h1(doc, "八、临场应对技巧")
    question(doc, 22, "被问到不会的问题怎么办？")
    answer(doc, "三步：先确认理解、再说明现状、最后给出路径。"
                "比如「这个指标为什么是这个数量级，我目前还没做针对性分析，"
                "我记录一下，会后补测并反馈给老师」。"
                "切忌编造数据、也不要用「大概是」糊过去——"
                "评委通常比你更熟悉这个领域，编一个数字被追问两层就露了。")

    question(doc, 23, "评委提出与你结论不同的意见怎么办？")
    answer(doc, "先接受再说明。可以说「老师这个角度我之前没考虑到，"
                "我的实测结果是 X，可能是在 Y 条件下才成立，我会按您的提示再验证」。"
                "学术讨论不需要当场分对错，重要的是展示你能听懂问题并愿意修正。"
                "切忌直接反驳或坚持「我的实验就是这么做的」。")

    question(doc, 24, "如果被质疑选题太大、做不完？")
    answer(doc, "承认范围确实较宽，但说明有收敛策略："
                "主线是「知识库 + 问答引擎 + 可溯源」，这三块已经打通；"
                "影像是加分项，如果时间紧张可以降级为只输出客观描述；"
                "评测部分先做核心指标，扩展实验按时间安排取舍。"
                "所以即使压缩，也能保证主线成果完整。")

    # ===== 九 =====
    h1(doc, "九、答辩前 30 分钟检查清单")
    items = [
        "纸质开题报告 3~5 份（问教务确认份数）",
        "PPT 存 U 盘 + 微信文件传输助手双备份（不要只存 U 盘）",
        "确认外文文献翻译是否需要提交（进度计划第 1 周提到此项）",
        "如需演示：提前 30 分钟启动系统并预热模型，否则首次提问要等约 60 秒",
        "把「必须记住的数字」那一页单独拍在手机上，候场时看",
        "准备一句话的自我介绍（姓名、学号、班级、指导教师）",
        "确认答辩地点与时长限制（通常汇报 10~15 分钟、提问 5~10 分钟）",
    ]
    for i, it in enumerate(items, 1):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before = Pt(0)
        pf.space_after = Pt(4)
        pf.line_spacing = 1.5
        pf.left_indent = Pt(18)
        r = p.add_run("□  %s" % it)
        set_run(r, size=11.5, color=RGBColor(0x22, 0x22, 0x22))

    para(doc, "最后提醒：开题答辩考察的是「这个题值不值得做、你能不能做出来、计划是否合理」，"
              "不是最终成果。因此汇报的重心应放在问题定义与技术路线上，"
              "前期基础作为可行性证据点到即可。",
         size=10.5, color=NAVY, first_indent=21, before=12, after=6)

    return doc


def main() -> int:
    doc = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    print("已生成: %s" % OUT)
    print("  段落 %d 个 | 表格 %d 个 | 大小 %.1f KB" % (
        len(doc.paragraphs), len(doc.tables), OUT.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
