"""生成本科毕业论文（设计）开题报告。

严格遵循模板 SJQU-QR-JW-106(A2) 的表结构与《开题报告文献综述格式要求》的字体规范：

  封面标题：黑体 三号 居中 1.5 倍行距
  一级标题：黑体 小三 居左 1.5 倍行距 段后 11 磅 段前 0
  二级标题：黑体 四号 居左 1.5 倍行距 段后 0 段前 0.5 行
  三级标题：黑体 小四 居左 1.5 倍行距 段后 0 段前 0.5 行
  正文：宋体 小四 首行缩进 2 字 多倍行距 1.25 段前段后 0 取消网格对齐

中文必须额外设置 w:eastAsia 字体，否则仅设 run.font.name 对汉字无效。

用法：
    python scripts/make_proposal.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

# ----------------------------------------------------------------------
#  配置
# ----------------------------------------------------------------------
TITLE = "基于术中影像与RAG的结直肠手术围术期多模态知识库智能问答系统"
OUT_PATH = r"D:\AI\毕设\开题报告-基于术中影像与RAG的结直肠手术围术期多模态知识库智能问答系统.docx"

SONG, HEI, TIMES = "宋体", "黑体", "Times New Roman"
# 表格总宽度（cm）。
#
# 可用宽度由页边距决定：21.0 - 3.17 × 2 = 14.66 cm。
# 这里取 13.4 cm 而非铺满 14.66 cm，原因有二：
#   ① 实测 LibreOffice 渲染表格时按内容自适应放大约 1.5 倍，
#      铺满可用宽度会导致右边框越出页面（Word 无此问题，但需兼容两种渲染器）
#   ② 留出余量后表格在 Word 与 LibreOffice 中均完整显示
TOTAL_W_CM = 13.4
BODY_PT = 12.0      # 小四
H1_PT = 15.0        # 小三
H2_PT = 14.0        # 四号
H3_PT = 12.0        # 小四


# ======================================================================
#  格式工具
# ======================================================================
def set_run_font(run, *, ascii_font=TIMES, east_asia=SONG, size_pt=None, bold=False):
    """设置 run 字体。中文必须显式设置 w:eastAsia。"""
    run.font.name = ascii_font
    run.font.bold = bold
    if size_pt is not None:
        run.font.size = Pt(size_pt)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), ascii_font)
    rfonts.set(qn("w:hAnsi"), ascii_font)
    rfonts.set(qn("w:eastAsia"), east_asia)


def set_para_format(
    para, *, align=None, line_spacing=None, space_before_pt=0.0,
    space_after_pt=0.0, indent_chars=0.0, size_pt=BODY_PT, snap_to_grid=True,
):
    """设置段落格式。indent_chars 用 w:firstLineChars 实现按字符缩进。"""
    pf = para.paragraph_format
    if align is not None:
        para.alignment = align
    if line_spacing is not None:
        pf.line_spacing = line_spacing
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.space_before = Pt(space_before_pt)
    pf.space_after = Pt(space_after_pt)

    ppr = para._p.get_or_add_pPr()
    if not snap_to_grid:
        sg = ppr.find(qn("w:snapToGrid"))
        if sg is None:
            sg = ppr.makeelement(qn("w:snapToGrid"), {})
            ppr.append(sg)
        sg.set(qn("w:val"), "0")
    if indent_chars:
        ind = ppr.find(qn("w:ind"))
        if ind is None:
            ind = ppr.makeelement(qn("w:ind"), {})
            ppr.append(ind)
        ind.set(qn("w:firstLineChars"), str(int(indent_chars * 100)))
        ind.set(qn("w:firstLine"), str(int(indent_chars * size_pt * 20)))


def add_para(
    container, text="", *, ascii_font=TIMES, east_asia=SONG, size_pt=BODY_PT,
    bold=False, align=None, line_spacing=1.25, space_before_pt=0.0,
    space_after_pt=0.0, indent_chars=0.0, snap_to_grid=False,
):
    """添加格式化段落，返回 paragraph 对象。"""
    para = container.add_paragraph()
    if text:
        run = para.add_run(text)
        set_run_font(run, ascii_font=ascii_font, east_asia=east_asia,
                     size_pt=size_pt, bold=bold)
    set_para_format(
        para, align=align, line_spacing=line_spacing,
        space_before_pt=space_before_pt, space_after_pt=space_after_pt,
        indent_chars=indent_chars, size_pt=size_pt, snap_to_grid=snap_to_grid,
    )
    return para


def h1(container, text):
    """一级标题：黑体 小三 居左 1.5 倍行距 段后 11 磅 段前 0。"""
    return add_para(container, text, ascii_font=HEI, east_asia=HEI,
                    size_pt=H1_PT, bold=True, align=WD_ALIGN_PARAGRAPH.LEFT,
                    line_spacing=1.5, space_before_pt=0, space_after_pt=11)


def h2(container, text):
    """二级标题：黑体 四号 居左 1.5 倍行距 段后 0 段前 0.5 行。"""
    return add_para(container, text, ascii_font=HEI, east_asia=HEI,
                    size_pt=H2_PT, bold=True, align=WD_ALIGN_PARAGRAPH.LEFT,
                    line_spacing=1.5, space_before_pt=7.5, space_after_pt=0)


def body(container, text):
    """正文：宋体 小四 首行缩进 2 字 多倍行距 1.25 段前后 0 取消网格对齐。"""
    return add_para(container, text, indent_chars=2, line_spacing=1.25,
                    space_before_pt=0, space_after_pt=0, snap_to_grid=False)


def body_noindent(container, text, **kw):
    return add_para(container, text, indent_chars=0, line_spacing=1.25,
                    space_before_pt=0, space_after_pt=0, snap_to_grid=False, **kw)


def set_table_layout(tbl, total_width_cm: float, col_widths_cm: list[float]) -> None:
    """设置表格为固定布局，并统一 tblW / tblGrid / tcW 三处宽度。

    ⚠️ 三处宽度必须一致，否则表格会宽于预期：

        w:tblW     声明表格总宽
        w:tblGrid  声明各栅格列宽  ← 在 fixed 布局下这是**权威值**
        w:tcW      声明各单元格宽

    python-docx 建表时按均分写入 tblGrid（例如两列各 4156）。
    若只改 tblW 而不改 tblGrid，两者不一致（tblW=7597 而栅格合计=8312），
    LibreOffice 取栅格宽度渲染，表格因此比预期宽约 9% 而越出页面右边距。
    该缺陷在 Word 中不显现（Word 优先取 tblW），故只在特定渲染器下暴露。

    另外，w:tblW 必须**改写**已有元素而非追加：python-docx 已写入
    `<w:tblW w:type="auto" w:w="0"/>`，追加会产生两个同名字段，
    LibreOffice 读取第一个（auto）从而完全忽略设定宽度。
    """
    tbl.autofit = False
    tbl_pr = tbl._tbl.tblPr

    # 1) 固定布局
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = tbl_pr.makeelement(qn("w:tblLayout"), {})
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")

    # 2) 表格总宽（改写已有元素）
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = tbl_pr.makeelement(qn("w:tblW"), {})
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(int(total_width_cm * 567)))
    tbl_w.set(qn("w:type"), "dxa")

    # 3) 表格缩进归零（部分样式带默认缩进）
    ind = tbl_pr.find(qn("w:tblInd"))
    if ind is None:
        ind = tbl_pr.makeelement(qn("w:tblInd"), {})
        tbl_pr.append(ind)
    ind.set(qn("w:w"), "0")
    ind.set(qn("w:type"), "dxa")

    # 4) 栅格列宽（fixed 布局下的权威值）
    grid = tbl._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        for idx, gc in enumerate(grid.findall(qn("w:gridCol"))):
            if idx < len(col_widths_cm):
                gc.set(qn("w:w"), str(int(col_widths_cm[idx] * 567)))

    # 5) 单元格宽度
    for row in tbl.rows:
        for idx, cell in enumerate(row.cells):
            if idx < len(col_widths_cm):
                cell.width = Cm(col_widths_cm[idx])


def set_cell_borders(cell, sz=8):
    """给单元格加全框线。"""
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.find(qn("w:tcBorders"))
    if borders is None:
        borders = tc_pr.makeelement(qn("w:tcBorders"), {})
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right"):
        el = borders.find(qn(f"w:{edge}"))
        if el is None:
            el = borders.makeelement(qn(f"w:{edge}"), {})
            borders.append(el)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:color"), "000000")


def clear_cell(cell):
    """清空单元格默认空段落。"""
    for p in list(cell.paragraphs):
        p._element.getparent().remove(p._element)


def fill_cell(cell, blocks, *, size_pt=BODY_PT, border=True):
    """用 (类型, 文本) 列表填充单元格。

    blocks 中每项为 (kind, text)：
        kind = 'h1' | 'h2' | 'body' | 'plain' | 'label'
    """
    clear_cell(cell)
    if border:
        set_cell_borders(cell)
    for kind, text in blocks:
        if kind == "h1":
            h1(cell, text)
        elif kind == "h2":
            h2(cell, text)
        elif kind == "body":
            body(cell, text)
        elif kind == "label":
            add_para(cell, text, ascii_font=HEI, east_asia=HEI,
                     size_pt=size_pt, bold=True, line_spacing=1.25,
                     space_before_pt=5, space_after_pt=0, snap_to_grid=False)
        elif kind == "ref":
            add_para(cell, text, indent_chars=0, line_spacing=1.25,
                     space_before_pt=0, space_after_pt=0, snap_to_grid=False)
        elif kind == "blank":
            add_para(cell, "", line_spacing=1.25, snap_to_grid=False)
        else:
            add_para(cell, text, line_spacing=1.25, snap_to_grid=False)


# ======================================================================
#  文档内容
# ======================================================================
BACKGROUND = [
    ("body", "结直肠癌是全球范围内发病率与死亡率均居前列的恶性肿瘤。根据世界卫生组织国际癌症研究机构发布的统计资料，2022年全球结直肠癌新发病例约192万例，死亡约90万例，在全部恶性肿瘤中发病率位列第三、死亡率位列第二。我国是结直肠癌高发国家，国家卫生健康委员会发布的《中国结直肠癌诊疗规范（2023版）》指出，随着居民饮食结构改变与人口老龄化，我国结直肠癌发病率呈持续上升趋势，且发病人群呈现年轻化特征，已成为严重威胁国民健康的重大疾病负担。"),
    ("body", "手术切除是结直肠癌最主要的根治手段，围术期管理质量直接决定手术安全性与远期预后。围术期通常涵盖术前评估与准备、术中决策与操作、术后并发症防治与康复三个阶段，各阶段均涉及大量需要循证依据支持的临床决策。以术后最常见且最凶险的并发症吻合口漏为例：法国全国多中心调查结果显示，该并发症的诊断与处理方案在各国、各中心之间差异显著，目前尚缺乏公认的统一诊疗路径；我国学者开展的卫生经济学分析亦表明，结直肠手术后并发症会显著延长住院时间并大幅增加医疗费用。上述情况说明，把分散在文献与指南中的循证证据及时、准确地送到临床决策现场，具有明确的现实需求。"),
    ("body", "然而，临床医师获取这类知识的现状并不理想，具体存在三个层面的问题。"),
    ("body", "第一，检索效率低。结直肠手术围术期知识分散在诊疗规范、系统评价、随机对照试验与综述等海量文献中，仅PubMed收录的相关文献即达数十万篇。临床医师在门诊或手术间隙难以完成有效检索，而中文提问与英文文献之间还存在语言壁垒，进一步抬高了获取成本。"),
    ("body", "第二，通用大语言模型的回答不可溯源。以ChatGPT为代表的大语言模型虽具备较强的自然语言理解与生成能力，但其知识来源于训练语料且存在滞后，在医学场景下易生成看似专业、实则无据的表述。医学决策对错误的容忍度极低，一个被编造出来的具体数值或结论即可能误导临床判断，因此缺乏依据支撑与来源标注的回答难以被采纳。现有研究已注意到该问题，有学者在《Journal of the American Medical Informatics Association》上对检索增强生成在生物医学中的应用进行了系统评价与元分析，并提出了临床开发指南，明确指出必须通过外部知识检索来约束模型的生成行为。"),
    ("body", "第三，现有资料形态单一、缺乏语义关联。围术期决策既需要文献证据，也需要参考术中影像的客观所见；同时，疾病、术式、并发症、药物等概念之间存在复杂的关联关系，仅依靠基于文本相似度的检索难以支撑多跳关联查询。"),
    ("body", "针对上述问题，本课题拟设计并实现一个基于术中影像与检索增强生成（Retrieval-Augmented Generation，RAG）的结直肠手术围术期多模态知识库智能问答系统。"),
    ("body", "本课题的意义在于三个方面。其一，将结直肠手术围术期的文献、指南与影像资料整合为专科知识库，使临床医师可以用自然语言直接提问并获得有文献依据的答案，从而降低循证知识的获取成本。其二，通过强制引用标注、引用编号校验与数值主张核验等机制，使每一处结论均可回溯至具体文献与原文片段，并为每个结论标注证据等级，满足医学场景对可溯源性的硬性要求。其三，本课题将对检索策略与生成质量开展量化评测，系统比较向量检索、关键词检索、知识图谱检索与交叉编码器重排等方案的实际贡献，其评测结论与工程经验可为同类医学RAG系统的构建提供参考。"),
]

MAIN_CONTENT = [
    ("body", "本课题需完成的主要工作围绕多模态知识库构建、问答引擎实现、场景应用开发与系统评测四个方面展开。"),
    ("h2", "1．多模态知识库构建"),
    ("body", "围绕结直肠手术围术期全流程，采集并整理三类资料。一是医学文献，通过程序接口从PubMed等数据库批量采集结直肠手术围术期相关文献题录与摘要，完成格式解析、文本切分与结构化标引；每个文本块须携带文献编号、章节、页码、围术期分期与证据等级等元数据，元数据是后续引用溯源的前提，不可或缺。二是临床指南与诊疗规范，录入国内外结直肠手术相关指南与专家共识，按术前、术中、术后进行分类标引。三是术中影像资料，建立影像知识库以存储公开的结直肠手术样例影像，支持影像上传、预览与元数据管理。"),
    ("h2", "2．RAG智能问答引擎"),
    ("body", "实现从提问到带引用答案的完整闭环。检索层采用多路召回策略，包括基于稠密向量的语义检索、面向中文与英文的关键词检索、以及基于知识图谱的关联检索，多路结果经融合算法合并后由交叉编码器重排模型精排。生成层基于大语言模型，依据检索到的证据片段生成答案，并在正文中以编号形式标注每处结论的来源。溯源层解析答案中的引用标记，剔除模型编造的无效编号，将有效引用映射回具体的文献与原文片段，同时逐个核验答案中出现的数值是否能在证据中找到依据。"),

    ("h2", "3．医学知识图谱构建"),
    ("body", "基于医学本体从知识库文本中抽取实体与关系，构建结直肠手术知识图谱，实现疾病、术式、并发症、药物、解剖部位、检查手段等实体的语义关联。图谱以节点与关系的形式存储，每条关系同时记录其来源文本块，以保证图谱检索的结果同样具备可溯源性。图谱检索路径与上述多路召回融合，用于支撑需要多跳关联的查询。"),
    ("h2", "4．影像辅助解读"),
    ("body", "实现影像的辅助解读功能。用户上传术中影像后，系统先调用云端多模态大模型对影像进行客观描述，再依据该描述构造检索查询、从知识库中检索相关文献证据，最后由大语言模型综合影像所见与文献证据给出辅助解读。该模块定位为辅助解读而非诊断：客观描述仅用于转述影像所见，所有结论性表述必须由检索到的文献证据支撑并标注来源，输出同时附带合规声明。"),
    ("h2", "5．围术期场景问答与多轮对话"),
    ("body", "针对术前、术中、术后三个阶段提供专业化问答服务，并支持上下文感知的多轮对话。由于检索依赖独立完整的查询串，而临床追问普遍使用指代与省略，系统需结合对话历史将追问改写为独立查询后再行检索，同时识别问题所属的围术期分期以缩小检索范围。"),
    ("h2", "6．系统管理与前端界面"),
    ("body", "实现知识库维护、问答日志记录与系统配置管理功能；开发前端界面，提供智能问答、多轮会诊、影像解读、知识库概览与系统状态五个功能页面，使系统可直接用于演示而无需命令行操作。"),
    ("h2", "7．评测与实验"),
    ("body", "构建带有标准答案来源标注的评测集，从检索质量与生成质量两个维度量化系统性能，并通过消融实验比较各检索路径与处理策略的实际贡献。"),
]

TECH_ROUTE = [
    ("h2", "1．软件开发环境"),
    ("body", "后端采用Python 3.12作为开发语言，以FastAPI框架提供RESTful接口与服务器推送事件（SSE）流式输出；前端采用Vue 3.5、TypeScript与Vite构建单页应用，界面组件库使用Element Plus，通过Pinia管理应用状态。数据库方面，采用PostgreSQL 16存储文献题录、文本块及其元数据，并启用pgvector扩展存储文本嵌入向量以支持近似最近邻检索；采用Neo4j 5存储知识图谱的实体与关系，支撑多跳关联检索；采用Redis 7作为缓存与会话存储。检索模型采用BGE-M3生成文本嵌入向量，采用bge-reranker-v2-m3完成交叉编码器重排，两者均在本机部署。答案生成调用DeepSeek大语言模型接口，影像解读调用智谱GLM-4V-Flash多模态模型接口。文献采集、向量化与知识图谱构建等批处理任务以脚本形式实现；各数据库服务通过Docker容器化部署，以保证环境一致性。"),
    ("h2", "2．硬件环境"),
    ("body", "开发主机配置独立显卡NVIDIA GeForce RTX 5060 Laptop（显存8 GB），用于本地部署嵌入模型与重排模型，以保障检索链路的响应速度并避免调用外部接口带来的数据外传风险；处理器为x86-64多核CPU，内存32 GB，固态硬盘预留不少于50 GB空间用于存放模型权重、文献数据与数据库文件。"),
    ("h2", "3．技术路线"),
    ("body", "系统整体按“知识入库—多路检索—融合重排—证据生成—引用溯源”的主线实现。离线阶段完成文献采集、文本切分、元数据标引、向量化入库与知识图谱构建；在线阶段接收用户提问后先进行查询改写与意图识别，再执行多路召回并融合重排，筛选出相关性达标的证据片段送入大语言模型生成答案，最后完成引用解析与数值核验。若检索不到有效证据或证据相关性低于阈值，系统不调用大语言模型，直接返回证据不足的提示，从源头上避免无依据作答。"),
    ("h2", "4．关键算法"),
    ("body", "多路召回结果的融合采用倒数排名融合（Reciprocal Rank Fusion，RRF）算法，该算法仅依据各路径结果的名次进行加权求和，无需对不同来源的分值做归一化，工程上更稳健。证据等级加权采用乘法形式，依据临床指南、系统评价、随机对照试验等不同证据等级对结果分值进行调整。重排阶段采用交叉编码器对查询与候选文本逐对打分，以获得比向量内积更精确的相关性排序。"),
    ("h2", "5．评测方法"),
    ("body", "从知识库文本块反向生成临床问题并标注答案来源，构成评测集；检索维度采用Recall@K、平均倒数排名（MRR）与归一化折损累计增益（NDCG）作为指标，生成维度采用忠实度、相关性与引用准确率作为指标，其中生成质量由大语言模型担任评审进行打分，并辅以数值主张核验这一不依赖评审的结构化检查。"),
]

EXPECTED = [
    ("body", "本课题预期完成一套可运行的系统并形成以下成果。"),
    ("body", "第一，建成结直肠手术围术期多模态知识库：文献规模不少于400篇，结构化文本块不少于1500个且元数据完整，并建成包含不少于1000个实体、1000条关系的知识图谱。"),
    ("body", "第二，实现完整的RAG智能问答闭环：支持输入自然语言问题后返回带可点击引用的答案，点击引用可定位至具体文献与原文片段，并为每个结论标注证据等级。"),
    ("body", "第三，实现影像辅助解读与多轮对话功能，支持上传术中影像获得有文献依据的辅助解读，支持同一会话内连续追问且系统能正确解析指代。"),
    ("body", "第四，完成前端界面，提供智能问答、多轮会诊、影像解读、知识库概览与系统状态五个功能页面，可直接用于毕业答辩演示。"),
    ("body", "第五，形成量化评测结论：在自建评测集上，检索召回率Recall@5预期不低于0.60，生成忠实度预期不低于0.75，引用准确率预期不低于0.80，并完成各检索路径与处理策略的消融实验对比。"),
    ("body", "第六，撰写毕业设计论文，系统阐述多模态知识库构建、检索增强生成链路设计、知识图谱构建、影像辅助解读与系统评测等工作的技术方案与实验结果。"),
]


# ----------------------------------------------------------------------
#  参考文献（GB/T 7714-2015 顺序编码制）
#
#  来源：10 篇为用户提供的中文指南/共识全文（已整理入库），
#  2 篇为自建语料库中的英文期刊文献。未复用任务书中的参考文献。
# ----------------------------------------------------------------------
REFERENCES = [
    "[1] 国家卫生健康委员会. 中国结直肠癌诊疗规范(2025版)[J]. 中华消化外科杂志, 2025.",
    "[2] 中国医师协会外科医师分会, 中华医学会外科学分会胃肠外科学组, 中华医学会外科学分会结直肠外科学组, 等. 结直肠癌肝转移诊断和综合治疗指南(2025版)[J]. 中华胃肠外科杂志, 2025.",
    "[3] 中华医学会外科学分会结直肠外科学组. 精准结直肠癌外科诊疗专家共识(2025版)[J]. 中华胃肠外科杂志, 2025.",
    "[4] 中华医学会肿瘤学分会. 早期结直肠癌全程管理指南(2025版)[J]. 中华消化外科杂志, 2025.",
    "[5] 中华医学会外科学分会结直肠外科学组. 机器人结直肠癌手术专家共识(2025版)[J]. 中华胃肠外科杂志, 2025.",
    "[6] 中华医学会外科学分会结直肠外科学组. 直肠肿瘤三维可视化指导精准手术专家共识(2025版)[J]. 中华消化外科杂志, 2025.",
    "[7] 中国抗癌协会. 中国恶性肿瘤整合诊治指南-直肠癌(2024版)[J]. 中国肿瘤临床, 2024.",
    "[8] 中华医学会外科学分会结直肠外科学组. 直肠癌新辅助治疗后等待观察策略中国专家共识(2024版)[J]. 中华胃肠外科杂志, 2024.",
    "[9] 中华医学会外科学分会结直肠外科学组. 吲哚菁绿荧光造影技术在腹腔镜结直肠癌根治术中的应用[J]. 中华胃肠外科杂志, 2024.",
    "[10] 中华医学会外科学分会腹腔镜与内镜外科学组, 中华医学会外科学分会结直肠外科学组. 腹腔镜结直肠癌根治术操作指南(2023版)[J]. 中华胃肠外科杂志, 2023.",
    "[11] LIU S, MCCOY A B, WRIGHT A. Improving large language model applications in biomedicine with retrieval-augmented generation: a systematic review, meta-analysis, and clinical development guidelines[J]. Journal of the American Medical Informatics Association, 2025, 32(4): 605-615.",
    "[12] WANG F, CHEN G, ZHANG Z, et al. The Chinese Society of Clinical Oncology (CSCO): clinical guidelines for the diagnosis and treatment of colorectal cancer, 2024 update[J]. Cancer Communications, 2025, 45(3): 332-379.",
]


# ----------------------------------------------------------------------
#  进度计划
#
#  时间节点参考任务书；若学校公布的节点不同，请修改下方 period 字段。
# ----------------------------------------------------------------------
SCHEDULE = [
    ("第1周", "课题调研与需求分析：查阅结直肠手术围术期与检索增强生成相关资料，明确系统功能需求与技术选型，完成外文文献翻译。"),
    ("第2周", "完成文献综述，撰写开题报告，参加开题答辩。"),
    ("第3—4周", "知识库构建：完成文献批量采集、格式解析、文本切分与元数据标引，实现向量化入库，搭建后端基础框架。"),
    ("第5—7周", "问答引擎实现：完成多路召回、融合重排、答案生成与引用溯源，打通“提问—检索—生成—引用”完整闭环。"),
    ("第8周", "参加中期检查：提交中期报告并演示可运行的系统原型。"),
    ("第9—10周", "知识图谱构建：完成本体设计、实体关系抽取与图谱入库，实现图谱检索路径并接入问答链路。"),
    ("第11周", "影像辅助解读模块：实现影像上传管理、多模态模型客观描述与基于文献证据的辅助解读。"),
    ("第12周", "多轮对话与场景问答：实现查询改写、意图识别与围术期分期路由，支持术前、术中、术后分场景问答。"),
    ("第13周", "前端界面与系统管理：完成五个功能页面，实现知识库维护与系统配置管理。"),
    ("第14—15周", "系统评测与实验：构建评测集，完成检索指标与生成质量评测，开展消融实验并整理实验数据。"),
    ("第16周", "撰写毕业论文，完善系统与文档，准备并参加毕业答辩。"),
]


# ======================================================================
#  文档组装
# ======================================================================
def setup_document() -> Document:
    """创建文档并设置 A4 页面与页边距。"""
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)      # A4
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(2.54)
    sec.bottom_margin = Cm(2.54)
    sec.left_margin = Cm(3.17)
    sec.right_margin = Cm(3.17)

    # 默认样式设为宋体小四，避免未显式设置的段落字体漂移
    normal = doc.styles["Normal"]
    normal.font.name = TIMES
    normal.font.size = Pt(BODY_PT)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), TIMES)
    rfonts.set(qn("w:hAnsi"), TIMES)
    rfonts.set(qn("w:eastAsia"), SONG)
    return doc


def build_cover(doc: Document) -> None:
    """封面：标题 + 信息表。

    ⚠️ 表格结构须与模板一致：模板把「题　目」标签单独放在第 0 行，
    题目正文独占第 1 行（整行合并）。若把题目正文放在第 0 行的第二列，
    11 cm 宽的单元格内长题目会紧贴右边界，视觉上像溢出页面。
    """
    add_para(doc, "", line_spacing=1.0, space_after_pt=24)

    # "本科毕业论文（设计）" —— 黑体 三号 居中
    add_para(doc, "本科毕业论文（设计）", ascii_font=HEI, east_asia=HEI,
             size_pt=16, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
             line_spacing=1.5, space_after_pt=6)
    # "开题报告" —— 黑体 二号 居中
    add_para(doc, "开题报告", ascii_font=HEI, east_asia=HEI,
             size_pt=22, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
             line_spacing=1.5, space_after_pt=30)

    tbl = doc.add_table(rows=8, cols=2)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_layout(tbl, TOTAL_W_CM, [3.6, TOTAL_W_CM - 3.6])

    # 行 0：题目标签 + 空值格（与模板一致）
    r0 = tbl.rows[0]
    r0.height = Cm(1.2)
    c0, c1 = r0.cells[0], r0.cells[1]
    clear_cell(c0)
    set_cell_borders(c0)
    add_para(c0, "题　　目", ascii_font=HEI, east_asia=HEI, size_pt=14,
             align=WD_ALIGN_PARAGRAPH.CENTER, line_spacing=1.0,
             snap_to_grid=False)
    clear_cell(c1)
    set_cell_borders(c1)

    # 行 1：题目正文，整行合并居中（长题目在此可完整换行显示）
    r1 = tbl.rows[1]
    r1.height = Cm(1.8)
    merged = r1.cells[0].merge(r1.cells[1])
    clear_cell(merged)
    set_cell_borders(merged)
    # 不设 merged.width：合并单元格跨越两列，其宽度由栅格决定；
    # 若在此再设一个值（如整表宽度），会反向撑宽第一列导致列宽失衡。
    add_para(merged, TITLE, size_pt=12,
             align=WD_ALIGN_PARAGRAPH.CENTER, line_spacing=1.25,
             snap_to_grid=False)

    # 行 2~7：其余信息项
    fields = [
        ("学生姓名", ""),
        ("学　　号", ""),
        ("学　　院", ""),
        ("专业班级", ""),
        ("指导教师", "徐漫涛"),
        ("交稿日期", ""),
    ]
    for offset, (label, value) in enumerate(fields):
        row = tbl.rows[2 + offset]
        row.height = Cm(1.3)
        cc0, cc1 = row.cells[0], row.cells[1]
        clear_cell(cc0)
        set_cell_borders(cc0)
        add_para(cc0, label, ascii_font=HEI, east_asia=HEI, size_pt=14,
                 align=WD_ALIGN_PARAGRAPH.CENTER, line_spacing=1.0,
                 snap_to_grid=False)
        clear_cell(cc1)
        set_cell_borders(cc1)
        if value:
            add_para(cc1, value, size_pt=12,
                     align=WD_ALIGN_PARAGRAPH.CENTER, line_spacing=1.25,
                     snap_to_grid=False)

    add_para(doc, "", line_spacing=1.0, space_after_pt=12)


def build_main_table(doc: Document) -> None:
    """正文主表：8 个内容行，全部为整行合并单元格。"""
    rows_spec = [
        # (标签, 是否含标签文字, 内容块)
        ("毕业论文（设计）题目：", True,
         [("plain", TITLE)]),
        ("课题背景和意义：", True, BACKGROUND),
        ("研究的主要内容：", True, MAIN_CONTENT),
        ("研究方法（或技术路线）：", True, TECH_ROUTE),
        ("预期结果：", True, EXPECTED),
        ("参考文献：", True,
         [("ref", r) for r in REFERENCES]),
        ("毕业论文（设计）进度计划（含起止时间、毕业论文（设计）各阶段内容）：", True,
         [("body", f"{period}：{desc}") for period, desc in SCHEDULE]),
        ("指导教师意见：（给出学生完成毕业论文（设计）任务的建议、注意事项，明确是否同意开题等内容）", True,
         [("blank", ""), ("blank", ""), ("blank", ""), ("blank", ""),
          ("blank", ""), ("blank", ""),
          ("plain", "指导教师（签名）：　　　　　　　　　　"),
          ("plain", "　　　　　　　年　　月　　日　　　　　")]),
        ("开题检查小组意见：", True,
         [("blank", ""), ("blank", ""), ("blank", ""), ("blank", ""),
          ("blank", ""),
          ("plain", "总体评价：□同意开题　　　　□修改后开题　　　　□修改后重新开题"),
          ("blank", ""),
          ("plain", "开题小组成员（签名）：　　　　　　　　　　　　　"),
          ("plain", "　　　　　　　年　　月　　日　　　　　")]),
    ]

    tbl = doc.add_table(rows=len(rows_spec), cols=2)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    # 先按两列设置固定布局（合并后单元格宽度需单独设，见循环内）
    set_table_layout(tbl, TOTAL_W_CM, [TOTAL_W_CM / 2, TOTAL_W_CM / 2])

    for ri, (label, has_label, blocks) in enumerate(rows_spec):
        row = tbl.rows[ri]
        c0, c1 = row.cells[0], row.cells[1]
        # 合并为整行单元格：模板中除封面外均为整行
        merged = c0.merge(c1)
        clear_cell(merged)
        set_cell_borders(merged)
        # 不设 merged.width：合并单元格跨越两列，其宽度由栅格决定；
    # 若在此再设一个值（如整表宽度），会反向撑宽第一列导致列宽失衡。

        if has_label:
            # 标签以黑体加粗呈现，随后接内容
            add_para(merged, label, ascii_font=HEI, east_asia=HEI,
                     size_pt=BODY_PT, bold=True, line_spacing=1.25,
                     space_before_pt=2, space_after_pt=2, snap_to_grid=False)
        for kind, text in blocks:
            if kind == "body":
                body(merged, text)
            elif kind == "ref":
                add_para(merged, text, indent_chars=0, line_spacing=1.25,
                         space_before_pt=0, space_after_pt=0, snap_to_grid=False)
            elif kind == "blank":
                add_para(merged, "", line_spacing=1.5, snap_to_grid=False)
            else:
                add_para(merged, text, line_spacing=1.25, snap_to_grid=False)


def build_notes(doc: Document) -> None:
    """第二页说明文字（模板中位于表后）。"""
    add_para(doc, "说明：", ascii_font=HEI, east_asia=HEI, size_pt=BODY_PT,
             bold=True, line_spacing=1.25, space_before_pt=12,
             space_after_pt=4, snap_to_grid=False)
    for text in [
        "1、封面单面打印，正文双面打印；",
        "2、正文格式同论文要求；",
        "3、文献综述（或读书报告）、文献翻译、外文文献各学院自行规定，"
        "文献综述（或调研报告）、文献翻译格式同论文要求。",
    ]:
        add_para(doc, text, indent_chars=2, line_spacing=1.25,
                 space_before_pt=0, space_after_pt=0, snap_to_grid=False)

    add_para(doc, "文献综述写法说明：", ascii_font=HEI, east_asia=HEI,
             size_pt=BODY_PT, bold=True, line_spacing=1.25,
             space_before_pt=12, space_after_pt=4, snap_to_grid=False)
    for text in [
        "1、根据现在新的毕业设计系统，文献综述需要单独形成一个文件，作为开题报告的附件进行上传。",
        "2、文献综述无需额外制作封面，只需在第一行写上文献综述的名称。",
        "3、文献综述的课题名称不可以和毕业设计课题名称相同。"
        "一般毕设的课题名称为****的设计与实现，文献综述则可以使用****的分析与研究。",
        "4、文献综述部分要求字数3000字，文献综述是学生在阅读文献后写的读书笔记，"
        "建议：由背景意义、现状分析、技术路线、课题的主要内容、实施的意义及参考文献，6个部分构成。",
        "5、通过文献综述，需要体现出对参考文献的阅读后的分析和理解，"
        "建议可以采用***在《******》论文中，提出****。然后结合论文的相关观点，"
        "阐述对当前课题的理解和帮助，描述自己对课题的观点。",
        "6、文献综述，是对开题报告ISO表格中内容的补充，开题报告因需控制页数，只是做了概述，"
        "可以在文献综述的6个部分中，进行更加详细的说明。",
        "7、文献综述的字体格式，参见《本科毕业论文（设计）模板及格式》中的相关要求。",
        "8、文献综述中的内容，同样作为开题报告的一部分，同任务书一起，"
        "在中期作品检查答辩时，进行核对、检查。",
    ]:
        add_para(doc, text, indent_chars=2, line_spacing=1.25,
                 space_before_pt=0, space_after_pt=0, snap_to_grid=False)


def main() -> int:
    out = Path(OUT_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = setup_document()
    build_cover(doc)
    build_main_table(doc)
    build_notes(doc)
    doc.save(out)

    print("已生成: %s" % out)
    print("  段落数: %d" % len(doc.paragraphs))
    print("  表格数: %d" % len(doc.tables))
    print("  封面表: %d 行" % len(doc.tables[0].rows))
    print("  主表  : %d 行" % len(doc.tables[1].rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
