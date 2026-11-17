"""用版面块方法重新评估全部 PDF 的抽取质量。

对照目的：验证「按标点合并」这一设计缺陷对多少份 PDF 造成了灾难性后果，
以及版面块方法修复了多少。

输出对照表：旧方法汉字数 / 新方法汉字数 / 提升倍数。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from extract_by_blocks import to_paragraphs  # noqa: E402
from extract_guideline_pdf import extract_lines  # noqa: E402

PDF_DIR = Path(r"D:\AI\毕设\PDF")
HAN = re.compile(r"[\u4e00-\u9fff]")


def han_of(text: str) -> int:
    return len(HAN.findall(text))


def main() -> int:
    files = sorted(PDF_DIR.glob("*.pdf"))
    rows = []
    print("评估 %d 份 PDF（旧方法 vs 版面块方法）" % len(files))
    print()
    print("%-50s %8s %8s %7s %s" % ("文件", "旧(汉字)", "新(汉字)", "倍数", "结论"))
    print("-" * 92)

    for f in files:
        old_n = new_n = 0
        old_err = new_err = ""
        try:
            lines, _ = extract_lines(f)
            old_n = han_of("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            old_err = type(exc).__name__
        try:
            paras, meta = to_paragraphs(f)
            new_n = meta["han"]
        except Exception as exc:  # noqa: BLE001
            new_err = type(exc).__name__

        ratio = (new_n / old_n) if old_n else float("inf")
        if new_err:
            verdict = "新方法失败:%s" % new_err
        elif old_n == 0:
            verdict = "旧方法全丢"
        elif ratio >= 3:
            verdict = "★修复 %dx" % round(ratio)
        elif ratio >= 1.3:
            verdict = "改善 %dx" % round(ratio, 1)
        elif ratio >= 0.8:
            verdict = "相当"
        else:
            verdict = "新方法较少 %.2fx" % ratio
        rows.append((f.stem, old_n, new_n, ratio, verdict))
        print("%-50s %8d %8d %6s %s" % (
            f.stem[:48], old_n, new_n,
            ("%dx" % round(ratio)) if old_n and ratio != float("inf") else "-",
            verdict))

    print()
    fixed = sum(1 for r in rows if r[3] >= 3 and r[2] > 0)
    improved = sum(1 for r in rows if 1.3 <= r[3] < 3)
    same = sum(1 for r in rows if 0.8 <= r[3] < 1.3)
    worse = sum(1 for r in rows if r[3] < 0.8)
    print("汇总: 大幅修复 %d | 改善 %d | 相当 %d | 新方法较少 %d" % (
        fixed, improved, same, worse))
    print("  旧方法总汉字: %d" % sum(r[1] for r in rows))
    print("  新方法总汉字: %d" % sum(r[2] for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
