"""生成《文献综述》。

严格遵循《（学生教师通用）开题报告文献综述格式要求.docx》的字体规范，
该规范中的字号/间距数值已从要求文档的示例段落中**实测提取**，非凭空推测：

  文献综述名称：黑体 居中 三号(16pt) 1.5 倍行距
  一级标题：黑体 居左 小三(15pt) 1.5 倍行距 段后 11 磅 段前 0
  二级标题：黑体 居左 四号(14pt) 1.5 倍行距 段后 0 段前 0.5 行(7.8pt)
  三级标题：黑体 居左 小四(12pt) 1.5 倍行距 段后 0 段前 0.5 行(7.8pt)
  正文：宋体 小四(12pt) 首行缩进 2 字 多倍行距 1.25 段前段后 0 取消网格对齐

结构按模板说明的建议：背景意义 / 现状分析 / 技术路线 / 主要内容 /
实施的意义 / 参考文献，共 6 个部分，正文约 3000 字。

文献引用方式按说明要求采用叙述式，如「***在《***》中提出***」，
以体现阅读后的分析与理解，而非单纯罗列。

用法：
    python scripts/make_review.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

# 文献综述名称。
#
# 模板要求：不可与毕设课题名称相同 —— 毕设为「…系统的设计与实现」，
# 文献综述用「…的分析与研究」。
#
# 这里未直接使用「检索增强生成」全称而用「RAG 技术」：
# 全称会让标题超过一行宽度，居中排版下末行仅余两三字，视觉上不美观。
# 缩略词已在正文首次出现处给出全称解释。
TITLE = "结直肠手术围术期多模态知识库与RAG技术的分析与研究"
OUT_PATH = r"D:\AI\毕设\文献综述-结直肠手术围术期多模态知识库与RAG技术的分析与研究.docx"

SONG, HEI, TIMES = "宋体", "黑体", "Times New Roman"
BODY_PT, H1_PT, H2_PT, H3_PT, TITLE_PT = 12.0, 15.0, 14.0, 12.0, 16.0

# 正文可用宽度（A4 21cm，左右页边距各 3.17cm）
AVAIL_CM = 21.0 - 3.17 * 2
# 表格/文本框宽度取略小于可用宽度，兼容 LibreOffice 的渲染偏差
SAFE_W_CM = 13.4


# ======================================================================
#  格式工具
# ======================================================================
def set_run_font(run, *, ascii_font=TIMES, east_asia=SONG, size_pt=None, bold=False):
    """设置 run 字体。中文必须显式设置 w:eastAsia，否则汉字回退到默认字体。"""
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
    """设置段落格式。indent_chars 通过 w:firstLineChars 实现按字符缩进。"""
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
    space_after_pt=0.0, indent_chars=0.0, snap_to_grid=False, first=False,
):
    """添加格式化段落。first=True 时插入到文档最前（用于标题）。"""
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


def doc_title(container, text):
    """文献综述名称：黑体 居中 三号 1.5 倍行距。"""
    return add_para(container, text, ascii_font=HEI, east_asia=HEI,
                    size_pt=TITLE_PT, align=WD_ALIGN_PARAGRAPH.CENTER,
                    line_spacing=1.5, space_before_pt=0, space_after_pt=12)


def h1(container, text):
    """一级标题：黑体 小三 居左 1.5 倍行距 段后 11 磅 段前 0。"""
    return add_para(container, text, ascii_font=HEI, east_asia=HEI,
                    size_pt=H1_PT, align=WD_ALIGN_PARAGRAPH.LEFT,
                    line_spacing=1.5, space_before_pt=0, space_after_pt=11)


def h2(container, text):
    """二级标题：黑体 四号 居左 1.5 倍行距 段后 0 段前 0.5 行(7.8 磅)。"""
    return add_para(container, text, ascii_font=HEI, east_asia=HEI,
                    size_pt=H2_PT, align=WD_ALIGN_PARAGRAPH.LEFT,
                    line_spacing=1.5, space_before_pt=7.8, space_after_pt=0)


def h3(container, text):
    """三级标题：黑体 小四 居左 1.5 倍行距 段后 0 段前 0.5 行(7.8 磅)。"""
    return add_para(container, text, ascii_font=HEI, east_asia=HEI,
                    size_pt=H3_PT, align=WD_ALIGN_PARAGRAPH.LEFT,
                    line_spacing=1.5, space_before_pt=7.8, space_after_pt=0)


def body(container, text):
    """正文：宋体 小四 首行缩进 2 字 多倍行距 1.25 段前后 0 取消网格对齐。"""
    return add_para(container, text, indent_chars=2, line_spacing=1.25,
                    space_before_pt=0, space_after_pt=0, snap_to_grid=False)


def ref_item(container, text):
    """参考文献条目：不缩进，便于编号对齐。"""
    return add_para(container, text, indent_chars=0, line_spacing=1.25,
                    space_before_pt=0, space_after_pt=0, snap_to_grid=False)


# ======================================================================
#  设置表格/页面
# ======================================================================
def setup_document() -> Document:
    """创建文档并设置 A4 页面与页边距，默认样式设为宋体小四。"""
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(2.54)
    sec.bottom_margin = Cm(2.54)
    sec.left_margin = Cm(3.17)
    sec.right_margin = Cm(3.17)

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


def build(doc: Document) -> None:
    """按六个部分组装文献综述。"""
    from review_content import (
        REFS, SEC1, SEC2_1, SEC2_2, SEC2_3, SEC2_4, SEC2_5, SEC2_INTRO,
        SEC3, SEC4, SEC5,
    )

    # 文献综述名称（无需封面，首行即名称）
    doc_title(doc, TITLE)

    sections = [
        ("一、背景意义", SEC1),
        ("二、现状分析", SEC2_INTRO + SEC2_1 + SEC2_2 + SEC2_3 + SEC2_4 + SEC2_5),
        ("三、技术路线", SEC3),
        ("四、课题的主要内容", SEC4),
        ("五、实施的意义", SEC5),
    ]

    for heading, blocks in sections:
        h1(doc, heading)
        for kind, text in blocks:
            if kind == "h2":
                h2(doc, text)
            elif kind == "h3":
                h3(doc, text)
            else:
                body(doc, text)

    # 第六部分：参考文献
    h1(doc, "六、参考文献")
    for ref in REFS:
        ref_item(doc, ref)


def main() -> int:
    # 允许从 scripts 目录直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    out = Path(OUT_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = setup_document()
    build(doc)
    doc.save(out)

    # 统计正文中文字数（不含参考文献）
    total = sum(
        len(p.text.replace(" ", ""))
        for p in doc.paragraphs
    )
    print("已生成: %s" % out)
    print("  段落数: %d" % len(doc.paragraphs))
    print("  中文字符合计（含标题与参考文献）: %d" % total)
    from review_content import REFS as _R
    print("  参考文献: %d 篇" % len(_R))
    return 0


if __name__ == "__main__":
    sys.exit(main())
