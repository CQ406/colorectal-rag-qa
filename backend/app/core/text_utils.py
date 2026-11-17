"""中文文本处理工具。

为什么需要这个模块
------------------
实测结论：PostgreSQL 的 pg_trgm 扩展**完全忽略非 ASCII 字符**。

    SELECT show_trgm('abc');        --> {"  a"," ab",abc,"bc "}   正常
    SELECT show_trgm('结直肠癌');    --> {}                        空集
    SELECT similarity('结直肠癌','结直肠癌术后吻合口漏');  --> 0

因此「中文用 pg_trgm」的常见方案在本环境不成立，会导致关键词检索完全失效。

采用的方案：中文二元切分（bigram）+ PostgreSQL 数组倒排索引
----------------------------------------------------------
  1. 把中文按相邻两字切成 bigram：'吻合口漏' -> {吻合, 合口, 口漏}
  2. 存入 text[] 列并建 GIN 索引
  3. 检索时用数组重叠运算符 `&&`（可走 GIN 索引），按交集大小排序

实测效果：查询 '吻合口漏' 对相关文档命中 3 个 bigram，对无关文档命中 0 个。

为什么不用 zhparser / pg_bigm：
  - zhparser 需要自行编译安装到 PostgreSQL 镜像，部署复杂度高，答辩演示风险大
  - pg_bigm 同样需要额外安装扩展
  - bigram 方案纯应用层实现 + 标准 GIN 索引，零额外依赖，效果对本课题足够

局限性（论文中需如实说明）：
  - bigram 粒度较粗，可能产生个别误匹配
  - 短查询（2 字）退化为单字，区分度下降
  - 已通过「向量检索为主、关键词为辅」的融合策略缓解
"""

from __future__ import annotations

import re

# 中文（含扩展区）与日韩文字范围
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
# 英文/数字词
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]*|\d+")


def is_cjk(ch: str) -> bool:
    """判断单个字符是否为中日韩文字。"""
    return bool(_CJK_PATTERN.match(ch))


def extract_cjk_bigrams(text: str) -> list[str]:
    """把中文切分为 bigram 列表。

    只对连续的中文片段做切分，中英混排时英文部分单独处理。

    >>> extract_cjk_bigrams('吻合口漏')
    ['吻合', '合口', '口漏']
    >>> extract_cjk_bigrams('术后 ERAS 管理')
    ['术后', '管理']
    """
    if not text:
        return []

    grams: list[str] = []
    # 抽出所有连续中文片段
    for run in re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+", text):
        if len(run) == 1:
            # 单字无法组 bigram，直接保留该字作为特征
            grams.append(run)
        else:
            grams.extend(run[i : i + 2] for i in range(len(run) - 1))
    return grams


def extract_ascii_terms(text: str) -> list[str]:
    """抽取英文词与数字，统一小写。

    >>> extract_ascii_terms('ERAS 与 Enhanced Recovery 2023')
    ['eras', 'enhanced', 'recovery', '2023']
    """
    if not text:
        return []
    return [m.group(0).lower() for m in _ASCII_WORD.finditer(text)]


def tokenize_for_search(text: str) -> list[str]:
    """生成用于关键词检索的完整 token 列表（中文 bigram + 英文词）。

    结果去重后返回，作为 text[] 列的内容存入数据库。
    """
    if not text:
        return []
    tokens = extract_cjk_bigrams(text) + extract_ascii_terms(text)
    # 去重但保持顺序（dict 保序）
    return list(dict.fromkeys(tokens))


def tokenize_query(text: str) -> list[str]:
    """生成查询 token（与 tokenize_for_search 一致）。"""
    return tokenize_for_search(text)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数，用于统计与展示。

    经验值：中文约 1 字 ≈ 1 token（BGE-M3 的 XLM-R 词表），
    英文约 4 字符 ≈ 1 token。仅用于展示，不用于精确计费。
    """
    if not text:
        return 0
    cjk = len(_CJK_PATTERN.findall(text))
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk
