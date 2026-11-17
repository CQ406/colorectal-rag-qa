"""最终确认：PubMed 是否为中文文献提供任何中文文本。

这个结论决定「能否通过 PubMed 补充中文语料」，必须用大样本一次性定论。

背景（我此前给出的两个矛盾结论，其中一个必然是错的）：
  结论 A：抽样 12 篇发现 4 篇含中文摘要段（33%）—— **没有可靠依据**
  结论 B：抽样 516 篇发现 0 篇含中文摘要（0%）

本脚本直接检查**原始 HTTP 响应文本**中的汉字数量，不经过任何解析器，
也不依赖任何正则转义（用 chr() 构造字符类，排除转义写法的干扰）。
"""
import sys

sys.path.insert(0, r"D:\AI\RAG_project\backend")

from app.etl.pubmed_client import PubMedClient

# 用 chr() 显式构造汉字区间，彻底避免 raw string / 转义歧义
CJK_START, CJK_END = 0x4E00, 0x9FFF
CJK_CHARS = set(chr(c) for c in range(CJK_START, CJK_END + 1))

BASE = (
    '("Colorectal Neoplasms"[MeSH Terms] OR "colorectal cancer"[Title/Abstract] '
    'OR "rectal cancer"[Title/Abstract] OR "colon cancer"[Title/Abstract])'
)

with PubMedClient() as c:
    pmids = c.search(f"{BASE} AND chinese[lang]", max_results=200)
    print("取样: %d 篇 language=chi 的文献" % len(pmids))

    total_cjk = 0
    docs_with_cjk = 0
    checked = 0

    # 分批取原始 XML 文本，直接数字符
    for i in range(0, len(pmids), 50):
        batch = pmids[i : i + 50]
        resp = c._get(
            "efetch.fcgi",
            {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"},
        )
        text = resp.text
        # 按 PubmedArticle 切分，逐篇统计
        parts = text.split("<PubmedArticle>")
        for p in parts[1:]:
            checked += 1
            n = sum(1 for ch in p if ch in CJK_CHARS)
            total_cjk += n
            if n > 0:
                docs_with_cjk += 1
        print("  已检查 %d 篇..." % checked)

    print()
    print("=" * 68)
    print("结论")
    print("=" * 68)
    print("  检查文献数            : %d" % checked)
    print("  原始响应中的汉字总数   : %d" % total_cjk)
    print("  含汉字的文献数         : %d" % docs_with_cjk)
    print()
    if docs_with_cjk == 0:
        print("  → PubMed 对中文文献**不提供任何中文文本**。")
        print("    题名与摘要均为英文（题名带方括号表示原文非英文）。")
        print("    因此无法通过 PubMed 补充可被中文检索命中的语料。")
    else:
        print("  → 有 %d 篇含中文，占比 %.1f%%" % (
            docs_with_cjk, docs_with_cjk / max(checked, 1) * 100))
