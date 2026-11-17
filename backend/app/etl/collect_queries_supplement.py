"""补充采集检索式：中文文献与近三年高证据文献。

背景：为什么需要补充
--------------------
初始采集使用的 9 个检索式**既无时间过滤、也无语言过滤**（`mindate=None`，
检索式中没有 `[PDAT]` 与 `[lang]`）。后果经实测确认：

    初始语料 444 篇中 —— 中文文献仅 17 篇（3.8%）
                        近三年文献仅 49 篇（11.0%）

而 PubMed 上实际可得（实测）：

    中文围术期文献          922 篇
    中文指南/共识           132 篇
    中文随机对照试验         168 篇
    近三年各主题文献        数百至上千篇

因此这是**采集策略缺陷，不是数据源限制**。

PubMed 默认按相关度排序，老文献积累的引用更多、相关度评分更高，
于是被大量纳入；加上没有语言过滤，中文文献自然占比极低。
本模块的两组检索式针对性修正这两点。

关于「中文文献的题录是英文」
----------------------------
PubMed 对非英文文献提供**英文译名与英文摘要**（题名以方括号标注），
实测样本：

    lang=chi | year=2024 | [Cancer incidence and mortality in China, 2022].
    lang=chi | year=2023 | [Surgical skills and precautions of pelvic exenteration ...

这对本系统**反而有利**：向量模型与关键词检索都能正常编码英文摘要，
不会因为中文摘要而失效；同时 `language=chi` 元数据保留了「这是中文文献」
这一事实，可用于语种结构统计与中文引用偏好。

两组检索式的分工
----------------
  CN_*       中文文献，按围术期主题细分（术前/术中/术后/营养/影像）
  RECENT_*   近三年文献，按**证据等级**细分（指南/系统评价/RCT）

证据等级是关键筛选维度：近三年文献量极大（单个检索式常上千篇），
若不加区分地采集，会引入大量低价值文献。优先采集指南与系统评价、
RCT，能在有限额度内最大化证据质量。

年份不写死
----------
`_recent_filter()` 按当前年份动态生成 `("2024"[PDAT] : "3000"[PDAT])`。
写死年份会在次年变得陈旧，而这是长期维护的知识库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 结直肠围术期的基础检索式（与主检索式口径一致，保证语料主题范围统一）
BASE = (
    '("Colorectal Neoplasms"[MeSH Terms] OR "Colorectal Surgery"[Title/Abstract] '
    'OR "colorectal cancer"[Title/Abstract] OR "rectal cancer"[Title/Abstract] '
    'OR "colon cancer"[Title/Abstract] OR "rectal neoplasms"[MeSH Terms])'
)


def _recent_filter(years: int = 3) -> str:
    """生成「近 N 年」的 PubMed 日期过滤。

    PubMed 语法：("2024"[PDAT] : "3000"[PDAT]) 表示 2024 年至今。
    上界用 3000 是惯用写法，表示「不设上限」。
    """
    start = datetime.now().year - years + 1
    return f'("{start}"[PDAT] : "3000"[PDAT])'


@dataclass
class CollectQuery:
    """一个采集检索式。

    与 `app.etl.collect_queries.CollectQuery` 结构一致，便于统一处理。
    `phase` 仅作展示与日志用 —— 实际分期由 `classifier.classify_phase()`
    从题名与摘要自动判定（见 `ingest._build_document`）。
    """

    name: str
    phase: str
    query: str
    limit: int = 50
    mindate: str | None = None
    tags: list[str] = field(default_factory=list)


# ======================================================================
#  第一组：中文文献
# ======================================================================
CN_QUERIES: list[CollectQuery] = [
    CollectQuery(
        name="中文-指南与专家共识",
        phase="general",
        query=f'{BASE} AND chinese[lang] AND (guideline[pt] OR consensus[Title/Abstract] '
              f'OR "expert consensus"[Title/Abstract] OR "clinical practice guideline"[Title/Abstract])',
        limit=90,
        tags=["中文", "指南", "高证据"],
    ),
    CollectQuery(
        name="中文-系统评价与Meta分析",
        phase="general",
        query=f'{BASE} AND chinese[lang] AND (systematic review[pt] OR meta-analysis[pt])',
        limit=80,
        tags=["中文", "系统评价", "高证据"],
    ),
    CollectQuery(
        name="中文-随机对照试验",
        phase="general",
        query=f'{BASE} AND chinese[lang] AND randomized controlled trial[pt]',
        limit=90,
        tags=["中文", "RCT", "高证据"],
    ),
    CollectQuery(
        name="中文-吻合口漏",
        phase="postop",
        query=f'{BASE} AND chinese[lang] AND ("anastomotic leak"[Title/Abstract] '
              f'OR "anastomotic leakage"[Title/Abstract] OR "anastomotic dehiscence"[Title/Abstract])',
        limit=80,
        tags=["中文", "术后", "并发症"],
    ),
    CollectQuery(
        name="中文-加速康复外科",
        phase="postop",
        query=f'{BASE} AND chinese[lang] AND ("enhanced recovery"[Title/Abstract] '
              f'OR "ERAS"[Title/Abstract] OR "fast track"[Title/Abstract])',
        limit=40,
        tags=["中文", "术后", "康复"],
    ),
    CollectQuery(
        name="中文-新辅助治疗",
        phase="preop",
        query=f'{BASE} AND chinese[lang] AND ("neoadjuvant"[Title/Abstract] '
              f'OR "chemoradiotherapy"[Title/Abstract] OR "neoadjuvant therapy"[MeSH Terms])',
        limit=80,
        tags=["中文", "术前", "治疗"],
    ),
    CollectQuery(
        name="中文-微创手术技术",
        phase="intraop",
        query=f'{BASE} AND chinese[lang] AND ("laparoscopic"[Title/Abstract] '
              f'OR "robotic"[Title/Abstract] OR "minimally invasive"[Title/Abstract] '
              f'OR "TaTME"[Title/Abstract] OR "transanal"[Title/Abstract])',
        limit=80,
        tags=["中文", "术中", "术式"],
    ),
    CollectQuery(
        name="中文-围术期营养与预康复",
        phase="general",
        query=f'{BASE} AND chinese[lang] AND ("nutrition"[Title/Abstract] '
              f'OR "prehabilitation"[Title/Abstract] OR "bowel preparation"[Title/Abstract] '
              f'OR "oral nutritional supplement"[Title/Abstract])',
        limit=50,
        tags=["中文", "营养", "预康复"],
    ),
    CollectQuery(
        name="中文-影像与灌注评估",
        phase="intraop",
        query=f'{BASE} AND chinese[lang] AND ("imaging"[Title/Abstract] '
              f'OR "indocyanine green"[Title/Abstract] OR "fluorescence"[Title/Abstract] '
              f'OR "MRI"[Title/Abstract] OR "computed tomography"[Title/Abstract])',
        limit=80,
        tags=["中文", "影像", "术中"],
    ),
]


# ======================================================================
#  第二组：近三年高证据文献
# ======================================================================
RECENT_QUERIES: list[CollectQuery] = [
    CollectQuery(
        name="近三年-指南与共识",
        phase="general",
        query=f'{BASE} AND (guideline[pt] OR consensus[Title/Abstract] '
              f'OR "expert consensus"[Title/Abstract] OR "clinical practice guideline"[Title/Abstract]) '
              f'AND {_recent_filter(3)}',
        limit=120,
        tags=["近三年", "指南", "高证据"],
    ),
    CollectQuery(
        name="近三年-系统评价与Meta分析",
        phase="general",
        query=f'{BASE} AND (systematic review[pt] OR meta-analysis[pt]) AND {_recent_filter(3)}',
        limit=120,
        tags=["近三年", "系统评价", "高证据"],
    ),
    CollectQuery(
        name="近三年-随机对照试验",
        phase="general",
        query=f'{BASE} AND randomized controlled trial[pt] AND {_recent_filter(3)}',
        limit=120,
        tags=["近三年", "RCT", "高证据"],
    ),
    CollectQuery(
        name="近三年-吻合口漏",
        phase="postop",
        query=f'{BASE} AND ("anastomotic leak"[Title/Abstract] '
              f'OR "anastomotic leakage"[Title/Abstract]) AND {_recent_filter(3)}',
        limit=100,
        tags=["近三年", "术后", "并发症"],
    ),
    CollectQuery(
        name="近三年-加速康复外科",
        phase="postop",
        query=f'{BASE} AND ("enhanced recovery"[Title/Abstract] OR "ERAS"[Title/Abstract] '
              f'OR "enhanced recovery after surgery"[MeSH Terms]) AND {_recent_filter(3)}',
        limit=60,
        tags=["近三年", "术后", "康复"],
    ),
    CollectQuery(
        name="近三年-术中荧光与灌注",
        phase="intraop",
        query=f'{BASE} AND ("indocyanine green"[Title/Abstract] OR "fluorescence angiography"[Title/Abstract] '
              f'OR "perfusion assessment"[Title/Abstract]) AND {_recent_filter(3)}',
        limit=80,
        tags=["近三年", "术中", "影像"],
    ),
    CollectQuery(
        name="近三年-微创与机器人手术",
        phase="intraop",
        query=f'{BASE} AND ("robotic"[Title/Abstract] OR "minimally invasive"[Title/Abstract] '
              f'OR "laparoscopic"[Title/Abstract]) AND {_recent_filter(3)}',
        limit=100,
        tags=["近三年", "术中", "术式"],
    ),
    CollectQuery(
        name="近三年-新辅助治疗",
        phase="preop",
        query=f'{BASE} AND ("neoadjuvant"[Title/Abstract] OR "total neoadjuvant therapy"[Title/Abstract] '
              f'OR "watch and wait"[Title/Abstract]) AND {_recent_filter(3)}',
        limit=80,
        tags=["近三年", "术前", "治疗"],
    ),
]


# 全部补充检索式。CLI 与任务执行器统一从这里取，避免两处定义不一致。
SUPPLEMENT_QUERIES: list[CollectQuery] = CN_QUERIES + RECENT_QUERIES


def total_limit() -> int:
    """补充采集的条数上限合计。"""
    return sum(q.limit for q in SUPPLEMENT_QUERIES)


def summary() -> dict:
    """采集计划摘要（用于 CLI 预览与接口返回）。"""
    return {
        "groups": {
            "中文文献": {
                "queries": len(CN_QUERIES),
                "limit": sum(q.limit for q in CN_QUERIES),
            },
            "近三年高证据": {
                "queries": len(RECENT_QUERIES),
                "limit": sum(q.limit for q in RECENT_QUERIES),
            },
        },
        "total_queries": len(SUPPLEMENT_QUERIES),
        "total_limit": total_limit(),
        "recent_filter": _recent_filter(3),
    }
