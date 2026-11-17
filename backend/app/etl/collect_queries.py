"""结直肠手术围术期文献采集配置。

定义按围术期分期划分的 PubMed 检索式，用于系统性构建知识库语料。

设计思路
--------
不按单一宽泛检索式抓取，而是**按分期分别检索**：
  1. 检索式带有明确的领域词，命中文献与研究主题更相关
  2. 检索结果天然带有分期标签预期，可与自动标注结果对比（论文实验）
  3. 各分期语料量可控，避免某一期别文献淹没整个知识库

PubMed 检索语法要点
-------------------
  [Title/Abstract]  限定在标题与摘要中检索
  [MeSH Terms]      按医学主题词检索，召回率高于自由词
  AND/OR/NOT        布尔运算，注意大写
  检索式过长时拆分为多个 query 分次执行（PubMed 对检索式长度有限制）
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CollectQuery:
    """一条采集检索式。"""

    name: str                     # 名称，用于日志与统计
    phase: str                    # 预期围术期分期（用于评估自动标注）
    query: str                    # PubMed 检索式
    limit: int = 100              # 采集条数上限
    mindate: str | None = None    # 起始日期 YYYY/MM/DD
    tags: list[str] = field(default_factory=list)


# 结直肠手术的核心 MeSH 主题词
# 说明：Colorectal Surgery 本身不是标准 MeSH，故用疾病词 + 手术词组合
COLORECTAL_CORE = (
    '("Colorectal Neoplasms"[MeSH Terms] OR "Colorectal Surgery"[Title/Abstract] '
    'OR "colorectal cancer"[Title/Abstract] OR "rectal cancer"[Title/Abstract] '
    'OR "colon cancer"[Title/Abstract] OR "rectal neoplasms"[MeSH Terms])'
)


COLLECT_QUERIES: list[CollectQuery] = [
    # ---------------- 术前 ----------------
    CollectQuery(
        name="术前评估与准备",
        phase="preop",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("Preoperative Care"[MeSH Terms] OR "preoperative"[Title/Abstract] '
            'OR "neoadjuvant"[Title/Abstract] OR "bowel preparation"[Title/Abstract] '
            'OR "prehabilitation"[Title/Abstract] OR "preoperative risk"[Title/Abstract] '
            'OR "preoperative nutrition"[Title/Abstract])'
        ),
        limit=60,
        tags=["术前", "评估", "准备"],
    ),
    CollectQuery(
        name="术前新辅助治疗",
        phase="preop",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("Neoadjuvant Therapy"[MeSH Terms] OR "neoadjuvant chemoradiotherapy"[Title/Abstract] '
            'OR "neoadjuvant chemotherapy"[Title/Abstract])'
        ),
        limit=40,
        tags=["术前", "新辅助", "放化疗"],
    ),
    # ---------------- 术中 ----------------
    CollectQuery(
        name="术式与手术技术",
        phase="intraop",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("surgical technique"[Title/Abstract] OR "laparoscopic"[Title/Abstract] '
            'OR "robotic"[Title/Abstract] OR "anastomosis"[Title/Abstract] '
            'OR "lymph node dissection"[Title/Abstract] OR "total mesorectal excision"[MeSH Terms] '
            'OR "intraoperative"[Title/Abstract])'
        ),
        limit=60,
        tags=["术中", "术式", "技术"],
    ),
    CollectQuery(
        name="术中影像与灌注评估",
        phase="intraop",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("indocyanine green"[MeSH Terms] OR "fluorescence angiography"[Title/Abstract] '
            'OR "intraoperative imaging"[Title/Abstract] OR "perfusion assessment"[Title/Abstract] '
            'OR "artificial intelligence"[Title/Abstract])'
        ),
        limit=40,
        tags=["术中", "影像", "荧光", "AI"],
    ),
    # ---------------- 术后 ----------------
    CollectQuery(
        name="吻合口漏",
        phase="postop",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("Anastomotic Leak"[MeSH Terms] OR "anastomotic leak"[Title/Abstract] '
            'OR "anastomotic leakage"[Title/Abstract] OR "dehiscence"[Title/Abstract])'
        ),
        limit=60,
        tags=["术后", "并发症", "吻合口漏"],
    ),
    CollectQuery(
        name="加速康复外科",
        phase="postop",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("Enhanced Recovery After Surgery"[MeSH Terms] OR "enhanced recovery"[Title/Abstract] '
            'OR "ERAS"[Title/Abstract] OR "fast track surgery"[Title/Abstract] '
            'OR "postoperative recovery"[Title/Abstract])'
        ),
        limit=60,
        tags=["术后", "康复", "ERAS"],
    ),
    CollectQuery(
        name="术后并发症与处理",
        phase="postop",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("Postoperative Complications"[MeSH Terms] OR "surgical site infection"[Title/Abstract] '
            'OR "postoperative ileus"[Title/Abstract] OR "postoperative management"[Title/Abstract])'
        ),
        limit=60,
        tags=["术后", "并发症", "管理"],
    ),
    # ---------------- 跨期 ----------------
    CollectQuery(
        name="围术期营养支持",
        phase="general",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("Nutritional Support"[MeSH Terms] OR "perioperative nutrition"[Title/Abstract] '
            'OR "parenteral nutrition"[Title/Abstract] OR "enteral nutrition"[Title/Abstract] '
            'OR "malnutrition"[MeSH Terms])'
        ),
        limit=40,
        tags=["营养", "围术期"],
    ),
    CollectQuery(
        name="指南与共识",
        phase="general",
        query=(
            f'{COLORECTAL_CORE} AND '
            '("Practice Guideline"[Publication Type] OR "Guideline"[Publication Type] '
            'OR "Consensus Development Conference"[Publication Type] '
            'OR "Systematic Review"[Publication Type] OR "Meta-Analysis"[Publication Type])'
        ),
        limit=60,
        tags=["指南", "共识", "循证"],
    ),
]


def total_limit() -> int:
    """所有检索式的采集上限合计。"""
    return sum(q.limit for q in COLLECT_QUERIES)


def summary() -> dict:
    """采集计划摘要。"""
    by_phase: dict[str, int] = {}
    for q in COLLECT_QUERIES:
        by_phase[q.phase] = by_phase.get(q.phase, 0) + q.limit
    return {
        "queries": len(COLLECT_QUERIES),
        "total_limit": total_limit(),
        "by_phase": by_phase,
    }
