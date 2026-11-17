"""对吻合口漏共识的清理稿做人工级结构修正。

需要修的问题（均由 PDF 抽取造成，已在注释中说明原因）：

1. **标题与正文粘连** 9 处
   例：`### 一 定义 2010 年，直肠手术吻合口漏由...`
   → `### 定义` + 正文

2. **一级小节被误标为三级** 4 处
   `（一）术前因素` 应为 `###`（原文的中文编号子节），
   但抽取时与后续内容粘连，层级判断错误。

3. **表格乱文与页脚水印混入正文** 1 处
   「表1」的表格内容在 PDF 中与正文流混排，抽取后成为乱码段；
   同时页脚水印「中国直肠癌手术吻合口漏诊断、预防及处理专家共识（2019版）」
   与页码「201」被并入。

4. **段落被页码水印截断** 1 处
   `肿瘤分期和直径是吻合口漏发生率增加的  中国直肠癌手术...  201 危险因素。`
   中间的乱文应删除，还原为一句完整的话。

处理方式：用精确的字符串替换，每一步都可由本文件追溯。
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(r"D:\AI\RAG_project\data\manual\中国直肠癌手术吻合口漏诊断、预防及处理专家共识2019版.clean.md")
DST = Path(r"D:\AI\RAG_project\data\manual\中国直肠癌手术吻合口漏诊断、预防及处理专家共识2019版.md")

# ---- 1. 标题与正文粘连：拆成「标题 + 正文」----
SPLITS: list[tuple[str, str, str]] = [
    # (原行首片段, 新标题, 正文前缀保留)
    ("### 一 定义 2010 年", "### 定义", "2010 年"),
    ("### 二 直肠吻合口漏的诊断与分级吻合口漏分级方法",
     "### 直肠吻合口漏的诊断与分级", "吻合口漏分级方法"),
    ("### 1. 性别：男性是术后吻合口漏的独立危险因素。男性患者",
     "### 性别", "男性是术后吻合口漏的独立危险因素。男性患者"),
    ("### 4. 术前合并症：患者术前有糖尿病",
     "### 术前合并症", "患者术前有糖尿病"),
    ("### 1. 手术方式及入路：直肠癌的手术方式及入路",
     "### 手术方式及入路", "直肠癌的手术方式及入路"),
    ("### 6. 其他：保留左结肠动脉",
     "### 其他", "保留左结肠动脉"),
    ("### 三 术后因素术后因素对吻合口漏的影响",
     "### 术后因素", "术后因素对吻合口漏的影响"),
    ("### 一 术前预防积极纠正术前高危因素",
     "### 术前预防", "积极纠正术前高危因素"),
]

# ---- 2. 一级小节层级修正（（一）（二）应为 ###，且去掉重复的「一/二/三」前缀）----
LEVEL_FIXES: list[tuple[str, str]] = [
    ("### 一 术前因素", "### 术前因素"),
    ("### 二 术中因素", "### 术中因素"),
    ("### 二 术中预防", "### 术中预防"),
]

# ---- 3. 编号小节缺少「：」的补上（原文有，抽取时被并入正文）----
SUB_COLON = [
    ("### 1. 预防性造口：预防性造口可以减轻", None),   # 该行本身完整，跳过
]


def main() -> int:
    text = SRC.read_text(encoding="utf-8")
    orig_len = len(text)

    # 1) 标题拆分
    fixed = 0
    for old_head, new_head, body_start in SPLITS:
        # 找到以 old_head 开头的那一行
        pattern = re.compile(r"^" + re.escape(old_head) + r"(.*)$", re.M)
        m = pattern.search(text)
        if not m:
            print("  [跳过] 未找到: %s" % old_head[:42])
            continue
        tail = m.group(1)
        # new_head 已含 '### '，body 为 body_start + 该行剩余部分
        replacement = new_head + "\n\n" + body_start + tail
        text = text[: m.start()] + replacement + text[m.end():]
        fixed += 1
    print("标题拆分: %d 处" % fixed)

    # 2) 层级修正
    lv = 0
    for old, new in LEVEL_FIXES:
        if old in text:
            text = text.replace(old, new, 1)
            lv += 1
    print("层级修正: %d 处" % lv)

    # 3) 剔除表格乱文与页脚水印（第 8 条「肿瘤状况」段）
    bad = re.compile(
        r"肿瘤分期和直径是吻合口漏发生率增加的\s*"
        r"中国直肠癌手术吻合口漏诊断、预防及处理专家共识（2019版）"
        r"的吻合口漏分级临床表现.*?201\s*危险因素。",
        re.S,
    )
    text, n = bad.subn("肿瘤分期和直径是吻合口漏发生率增加的危险因素。", text)
    print("表格/水印乱文清理: %d 处" % n)

    # 4) 「家赞成率93.18%）202 5. 引流」——丢失括号与编号，修回
    bad2 = re.compile(r"家赞成率93\.18%）202\s*5\.\s*引流：", re.S)
    text, n2 = bad2.subn("（专家赞成率93.18%）\n\n### 引流\n\n", text)
    print("缺失括号与编号修复: %d 处" % n2)

    # 5) 把「### 引流」下面紧接的内容中，把「5. 引流：盆腔引流：」这类残留清掉
    text = text.replace("### 引流\n\n盆腔引流：", "### 引流\n\n盆腔引流：")
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 6) 片段句归并：把以（专家赞成率...）开头的孤立段落接到上一段末尾
    lines = text.split("\n")
    merged: list[str] = []
    rate_re = re.compile(r"^[（(]\s*(依情况做\S*?，?\s*)?专家赞成率\s*[\d.]+\s*%\s*[）)]\s*$")
    for ln in lines:
        if rate_re.match(ln.strip()) and merged and merged[-1].strip() and not merged[-1].startswith("#"):
            merged[-1] = merged[-1].rstrip() + ln.strip()
        else:
            merged.append(ln)
    text = "\n".join(merged)
    text = re.sub(r"\n{3,}", "\n\n", text)

    DST.write_text(text, encoding="utf-8")

    # 校验
    heads = re.findall(r"^#{2,3} .+$", text, re.M)
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    paras = [x for x in text.split("\n") if x.strip() and not x.startswith(("#", "*", ">"))]
    print()
    print("已写出: %s" % DST)
    print("  标题 %d 个 | 汉字 %d | 段落 %d | 最长段 %d" % (
        len(heads), han, len(paras), max((len(p) for p in paras), default=0)))
    print("  字数变化: %d -> %d" % (orig_len, len(text)))
    print()
    print("=== 最终结构 ===")
    for h in heads:
        print("  %s" % h[:66])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
