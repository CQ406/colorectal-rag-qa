"""把 40 份英文 PDF 转为录入文件，使用「最大字号」提取的真实题名。

与前一版的差别
--------------
前一版按「首页最长的行」猜题名，得到的是作者单位、期刊页眉
（如「1Department of Artificial Intelligence…」），无法用于引用与检索。
本版改用 pdf_title_extract 的字号法，取得真实题名，并做三项清洗：

1. 去出版务前缀（Original Article / Research Article / Review 等）
2. 解码 HTML 实体（部分出版社导出为 `&#x02010;` 形式）
3. 题名仍不可用时，用「期刊-卷-页」式文件名兜底并单独列出待人工核对

排除的 6 份
----------
临床领域与本课题无关（关节置换、器官移植、减重代谢、食管切除、胆囊切除、神经外科）。
保留的若干 AI/RAG 方法类文献虽非结直肠主题，但属于本系统技术路线的直接依据，
可用于回答「系统如何实现」类问题，因此保留。
"""
from __future__ import annotations

import html
import re
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
import sys

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pdf_title_extract import extract_title  # noqa: E402
from pdf_to_manual import build  # noqa: E402

EN_DIR = Path(r"D:\AI\毕设\外网")
OUT = Path(r"D:\AI\RAG_project\data\manual_staging_en")

# 临床领域与本课题无关，排除
OFF_TOPIC = {
    "42836_2026_Article_437",   # 关节置换
    "11596_2026_Article_219",   # 器官移植
    "11695_2026_Article_8746",  # 减重代谢手术
    "464_2026_Article_12985",   # 机器人食管切除
    "464_2026_Article_13119",   # 单孔腹腔镜胆囊切除
    "fnins-19-1606038",         # 神经外科
}

# 出版务前缀，需剥离
PREFIXES = (
    "original article", "research article", "original research",
    "systematic review", "review article", "review", "article",
    "clinical investigation", "editorial", "letter",
)

NOISE = re.compile(r"^(Downloaded from|This article is|Published in partnership|"
                   r"Copyright|Received|Accepted|Academic Editor|Edited by)", re.I)


def clean_title(t: str) -> str:
    t = html.unescape(t)
    t = t.replace("\u00ad", "").replace("\u2010", "-").replace("\u2011", "-")
    t = re.sub(r"\s+", " ", t).strip()
    low = t.lower()
    for pre in PREFIXES:
        if low.startswith(pre + " ") or low.startswith(pre + ":"):
            t = t[len(pre):].lstrip(" :.-")
            break
    return t.strip()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.md"):
        old.unlink()

    rows = []
    for p in sorted(EN_DIR.glob("*.pdf")):
        if p.stem in OFF_TOPIC:
            continue
        title, how = extract_title(p)
        title = clean_title(title)
        if NOISE.match(title) or len(title) < 15:
            how = "need-check"
        try:
            md, st = build(p, {"title": title, "year": None, "journal": None,
                               "evidence": "review", "phase": "general"})
            fn = re.sub(r'[\\/:*?"<>|]', "_", title)[:90] + ".md"
            (OUT / fn).write_text(md, encoding="utf-8")
            rows.append({"stem": p.stem, "title": title, "how": how,
                         "words": len(re.findall(r"[A-Za-z]+", md)), "ok": True})
        except Exception as exc:  # noqa: BLE001
            rows.append({"stem": p.stem, "title": title, "how": how,
                         "err": f"{type(exc).__name__}", "ok": False})

    print("转换成功 %d 份（排除 %d 份临床无关）" % (
        sum(1 for r in rows if r["ok"]), len(OFF_TOPIC)))
    print()
    print("%-30s %-10s %s" % ("来源PDF", "方式", "题名"))
    print("-" * 108)
    for r in rows:
        print("%-30s %-10s %s" % (r["stem"][:30], r["how"], r["title"][:70]))

    need = [r for r in rows if r["how"] == "need-check"]
    if need:
        print()
        print("⚠ 需人工核对题名的 %d 份:" % len(need))
        for r in need:
            print("   %s → %r" % (r["stem"], r["title"]))

    print()
    print("输出: %s（%d 个 .md）" % (OUT, len(list(OUT.glob("*.md")))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
