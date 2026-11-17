"""生成用于版式检查的 PPT 副本（抵消渲染器的 1.5 倍缩放）。

问题
----
本轮使用的 LibreOffice 渲染器对 PPTX 存在**固定 1.5 倍的内容缩放**：

    形状 XML 坐标 0.70 in → 渲染出现在 1.05 in 处（×1.5）
    最小对照实验（单个矩形）复现，且与声明的幻灯片尺寸无关
    （把 sldSz 改成 8.889×5.0 后，画布变小但内容仍按 1.5 倍绘制）

后果：画布宽度等于声明的幻灯片宽度，而内容被放大 1.5 倍，
因此**超出 slide_width/1.5 的部分被裁掉**。对 13.333 英寸宽的 16:9 幻灯片，
8.889 英寸以右的内容全部不可见，正是本 PPT 右侧卡片区域。

这不影响交付文件本身（XML 坐标正确，PowerPoint 中显示正常），
但使渲染检查无法覆盖右三分之一。

绕过方式
--------
保持幻灯片尺寸不变，把所有形状的 left/top/width/height 与所有字号的
数值缩小 1/1.5。渲染时那 1.5 倍放大正好抵消，输出与真实效果一致。

    真实坐标 10.0 in → 副本中 6.667 in → 渲染 ×1.5 → 10.0 in ✓

注意：本副本**仅用于版式检查**，不作为交付物。字号与线宽同步缩放，
因此文字相对于文本框的比例不变，溢出判断仍然有效。
"""
from __future__ import annotations

import sys
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu, Pt

K = 1.5


def scale_emu(value, k: float = K):
    return None if value is None else Emu(int(value / k))


def shrink(src: Path, dst: Path, k: float = K) -> tuple[int, int]:
    prs = Presentation(str(src))
    n_shapes = 0
    n_runs = 0

    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.left is not None:
                shape.left = scale_emu(shape.left, k)
            if shape.top is not None:
                shape.top = scale_emu(shape.top, k)
            if shape.width is not None:
                shape.width = scale_emu(shape.width, k)
            if shape.height is not None:
                shape.height = scale_emu(shape.height, k)
            n_shapes += 1

            # 线宽同步缩放（避免细线在小尺寸下消失或变粗）
            try:
                if shape.line is not None and shape.line.width is not None:
                    shape.line.width = Pt(max(0.25, shape.line.width.pt / k))
            except (AttributeError, ValueError):
                pass

            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    if run.font.size is not None:
                        run.font.size = Pt(max(1.0, run.font.size.pt / k))
                        n_runs += 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(dst))
    return n_shapes, n_runs


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python make_ppt_qa_copy.py <源.pptx> <QA副本.pptx>")
        raise SystemExit(1)
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    shapes, runs = shrink(src, dst)
    prs = Presentation(str(dst))
    print("QA 副本已生成: %s" % dst)
    print("  幻灯片 %d 页 | 缩放形状 %d 个、字号 %d 处 | 尺寸 %.2f x %.2f in" % (
        len(prs.slides), shapes, runs,
        prs.slide_width / 914400, prs.slide_height / 914400))
