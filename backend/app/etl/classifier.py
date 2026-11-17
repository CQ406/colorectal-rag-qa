"""文献分期与证据等级自动标注。

为什么需要自动标注
------------------
引用溯源要求每个文本块携带「围术期分期」与「证据等级」。人工标注 500 篇
文献不现实，故采用「规则 + 关键词权重」自动标注，并保留人工修正接口。

标注策略
--------
分期（preop/intraop/postop/general）：
  统计文本中三类关键词的加权出现次数，得分最高者为该文献分期；
  若三类得分接近或总体过低，则判为 general（跨期通用）。

证据等级：
  优先用 PubMed 的 PublicationType（权威且结构化）；
  其次用标题/摘要中的中文关键词（针对中文指南与共识）；
  最后按期刊类型兜底。

局限与论文说明
--------------
规则法对边界案例会误判，故：
  1. 保留 score 与 matched_terms 供人工复核
  2. `needs_review` 标记低置信度样本，前端可筛选人工确认
  3. 论文中应报告自动标注的准确率（抽样 50 篇人工核对）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.constants import EvidenceLevel, PerioperativePhase

# ----------------------------------------------------------------------
#  分期关键词（按权重降序，权重越高越能确定分期）
# ----------------------------------------------------------------------
PHASE_KEYWORDS: dict[PerioperativePhase, dict[str, int]] = {
    PerioperativePhase.PREOP: {
        # 术前评估与准备
        "preoperative": 3, "pre-operative": 3, "术前": 4, "preop": 3,
        "neoadjuvant": 3, "新辅助": 4,
        "bowel preparation": 3, "肠道准备": 4, "机械性肠道准备": 4,
        "prehabilitation": 3, "预康复": 4,
        "preoperative risk": 3, "术前评估": 4, "术前准备": 4,
        "staging": 2, "分期评估": 3, "术前营养": 4,
        "preoperative fasting": 3, "术前禁食": 4,
        "anesthesia assessment": 2, "麻醉评估": 3,
    },
    PerioperativePhase.INTRAOP: {
        # 术中操作与决策
        "intraoperative": 3, "intra-operative": 3, "术中": 4,
        "surgical technique": 3, "手术技巧": 3, "术式": 4,
        "anastomosis": 2, "吻合": 3, "吻合口": 3,
        "laparoscopic": 2, "腹腔镜": 3, "robotic": 2, "机器人": 3,
        "conversion": 2, "中转开腹": 3,
        "intraoperative complication": 3, "术中并发症": 4,
        "blood loss": 2, "出血量": 2, "operative time": 2, "手术时间": 2,
        "lymph node dissection": 3, "淋巴结清扫": 3,
        "fluorescence": 3, "荧光": 3, "indocyanine": 3,
        "intraoperative imaging": 4, "术中影像": 4,
    },
    PerioperativePhase.POSTOP: {
        # 术后管理与并发症
        "postoperative": 3, "post-operative": 3, "术后": 4,
        "anastomotic leak": 4, "吻合口漏": 4, "吻合口瘘": 4,
        "complication": 2, "并发症": 3,
        "recovery": 2, "康复": 3, "enhanced recovery": 3, "eras": 3,
        "加速康复": 4, "快速康复": 4,
        "length of stay": 3, "住院时间": 3,
        "mortality": 2, "死亡率": 2, "morbidity": 2, "并发症发生率": 3,
        "follow-up": 2, "随访": 2, "recurrence": 2, "复发": 2,
        "adjuvant": 3, "辅助化疗": 4, "chemotherapy": 2, "化疗": 2,
        "surgical site infection": 3, "切口感染": 3,
        "ileus": 3, "肠梗阻": 3, "discharge": 2, "出院": 2,
        "early oral feeding": 3, "早期进食": 3,
    },
}

# ----------------------------------------------------------------------
#  PubMed PublicationType → 证据等级
# ----------------------------------------------------------------------
PUBLICATION_TYPE_MAP: dict[str, EvidenceLevel] = {
    "Practice Guideline": EvidenceLevel.GUIDELINE,
    "Guideline": EvidenceLevel.GUIDELINE,
    "Consensus Development Conference": EvidenceLevel.CONSENSUS,
    "Consensus Development Conference, NIH": EvidenceLevel.CONSENSUS,
    "Meta-Analysis": EvidenceLevel.SYSTEMATIC,
    "Systematic Review": EvidenceLevel.SYSTEMATIC,
    "Randomized Controlled Trial": EvidenceLevel.RCT,
    "Clinical Trial, Phase III": EvidenceLevel.RCT,
    "Controlled Clinical Trial": EvidenceLevel.RCT,
    "Observational Study": EvidenceLevel.COHORT,
    "Comparative Study": EvidenceLevel.COHORT,
    "Case Reports": EvidenceLevel.CASE,
    "Review": EvidenceLevel.REVIEW,
    "Systematic Review and Meta-Analysis": EvidenceLevel.SYSTEMATIC,
}

# 中文证据等级关键词（针对中文指南与共识）
EVIDENCE_KEYWORD_MAP: dict[str, EvidenceLevel] = {
    "诊疗规范": EvidenceLevel.GUIDELINE,
    "指南": EvidenceLevel.GUIDELINE,
    "专家共识": EvidenceLevel.CONSENSUS,
    "共识": EvidenceLevel.CONSENSUS,
    "系统评价": EvidenceLevel.SYSTEMATIC,
    "meta分析": EvidenceLevel.SYSTEMATIC,
    "荟萃分析": EvidenceLevel.SYSTEMATIC,
    "随机对照": EvidenceLevel.RCT,
    "随机双盲": EvidenceLevel.RCT,
    "队列研究": EvidenceLevel.COHORT,
    "回顾性研究": EvidenceLevel.COHORT,
    "病例报告": EvidenceLevel.CASE,
    "个案报道": EvidenceLevel.CASE,
    "综述": EvidenceLevel.REVIEW,
}


@dataclass
class PhaseResult:
    """分期判定结果。"""

    phase: PerioperativePhase
    scores: dict[str, int] = field(default_factory=dict)
    matched_terms: dict[str, list[str]] = field(default_factory=dict)

    @property
    def confidence(self) -> float:
        """置信度 = 最高分 / 总分。低置信度需人工复核。"""
        total = sum(self.scores.values())
        if total == 0:
            return 0.0
        return max(self.scores.values()) / total

    @property
    def needs_review(self) -> bool:
        """是否需要人工复核：总分过低或分期不明确。"""
        total = sum(self.scores.values())
        return total < 3 or self.confidence < 0.5


# ----------------------------------------------------------------------
#  分期判定
# ----------------------------------------------------------------------
def classify_phase(*texts: str | None) -> PhaseResult:
    """根据标题与摘要判定围术期分期。

    对传入的所有文本做关键词匹配，按权重累计得分。

    Args:
        *texts: 标题、摘要等文本，任意数量
    """
    combined = " ".join(t for t in texts if t).lower()
    if not combined:
        return PhaseResult(phase=PerioperativePhase.GENERAL)

    scores: dict[str, int] = {p.value: 0 for p in PerioperativePhase}
    matched: dict[str, list[str]] = {p.value: [] for p in PerioperativePhase}

    for phase, keywords in PHASE_KEYWORDS.items():
        for keyword, weight in keywords.items():
            # 用正则做词边界保护，避免 "postoperative" 命中 "preoperative"
            count = _count_occurrences(combined, keyword)
            if count:
                scores[phase.value] += weight * min(count, 3)  # 单词最多计 3 次
                matched[phase.value].append(f"{keyword}×{count}")

    # 三类得分全为 0 → general
    max_score = max(scores.values())
    if max_score == 0:
        return PhaseResult(phase=PerioperativePhase.GENERAL, scores=scores, matched_terms=matched)

    # 取最高分；若最高分与其他分接近（差距 < 20%），判为 general（跨期）
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) > 1 and sorted_scores[0] > 0:
        if (sorted_scores[0] - sorted_scores[1]) / sorted_scores[0] < 0.2:
            return PhaseResult(
                phase=PerioperativePhase.GENERAL, scores=scores, matched_terms=matched
            )

    best = max(scores, key=lambda k: scores[k])
    return PhaseResult(
        phase=PerioperativePhase(best), scores=scores, matched_terms=matched
    )


def _count_occurrences(text: str, keyword: str) -> int:
    """统计关键词出现次数。

    英文关键词加词边界，避免子串误匹配（如 "preop" 命中 "preoperative" 时
    两者都应计入，但 "eras" 不应命中 "erasmus"）。
    中文关键词直接子串计数。
    """
    if re.search(r"[\u4e00-\u9fff]", keyword):
        return text.count(keyword)
    return len(re.findall(rf"\b{re.escape(keyword)}\b", text))


# ----------------------------------------------------------------------
#  证据等级判定
# ----------------------------------------------------------------------
def classify_evidence(
    publication_types: list[str] | None = None,
    title: str | None = None,
    abstract: str | None = None,
) -> tuple[EvidenceLevel, str]:
    """判定证据等级。

    优先级：PubMed 出版类型 > 中文关键词 > 兜底。

    Returns:
        (证据等级, 判定依据说明)
    """
    # 1) PubMed 结构化出版类型（最权威）
    if publication_types:
        # 按证据强度排序后取最高
        candidates = [
            PUBLICATION_TYPE_MAP[pt]
            for pt in publication_types
            if pt in PUBLICATION_TYPE_MAP
        ]
        if candidates:
            best = max(candidates, key=lambda e: e.weight)
            return best, f"PubMed出版类型: {', '.join(publication_types)}"

    # 2) 中文关键词（标题优先，其次摘要）
    combined = f"{title or ''} {abstract or ''}".lower()
    for keyword, level in sorted(
        EVIDENCE_KEYWORD_MAP.items(), key=lambda kv: -kv[1].weight
    ):
        if keyword in combined:
            return level, f"关键词命中: {keyword}"

    # 3) 英文标题特征兜底
    title_lower = (title or "").lower()
    if "systematic review" in title_lower or "meta-analysis" in title_lower:
        return EvidenceLevel.SYSTEMATIC, "标题含 systematic review/meta-analysis"
    if "randomized" in title_lower or "randomised" in title_lower:
        return EvidenceLevel.RCT, "标题含 randomized"
    if "review" in title_lower:
        return EvidenceLevel.REVIEW, "标题含 review"

    return EvidenceLevel.OTHER, "无法判定，归为其他"
