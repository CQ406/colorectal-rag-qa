"""领域常量定义。

把「围术期分期」「证据等级」「实体类型」等业务概念固化为枚举，
避免在代码各处散落魔法字符串。这些枚举同时用于：
  - 文献/知识块的元数据标注
  - 检索时的域过滤
  - 前端筛选条件
  - 知识图谱的节点标签
"""

from __future__ import annotations

from enum import Enum


class PerioperativePhase(str, Enum):
    """围术期分期 —— 对应报审表「（三）围术期场景应用模块」。

    这是本系统区别于通用医学问答的关键维度：
    同一疾病在术前/术中/术后关注点完全不同，检索时按分期过滤可显著降噪。
    """

    PREOP = "preop"      # 术前：诊断、适应症评估、风险预测、肠道准备、营养
    INTRAOP = "intraop"  # 术中：术式选择、突发情况、并发症处理、解剖要点
    POSTOP = "postop"    # 术后：并发症识别、用药、康复、随访
    GENERAL = "general"  # 跨期通用：基础解剖、流行病学、总论

    @property
    def zh(self) -> str:
        return {
            "preop": "术前",
            "intraop": "术中",
            "postop": "术后",
            "general": "通用",
        }[self.value]


class EvidenceLevel(str, Enum):
    """证据等级 —— 用于引用溯源时标注来源可信度。

    参照循证医学常见分级简化而来，便于在答案中提示证据强度。
    """

    GUIDELINE = "guideline"      # 临床指南 / 诊疗规范
    CONSENSUS = "consensus"      # 专家共识
    SYSTEMATIC = "systematic"    # 系统评价 / Meta 分析
    RCT = "rct"                  # 随机对照试验
    COHORT = "cohort"            # 队列 / 病例对照研究
    CASE = "case"                # 病例报告 / 病例系列
    REVIEW = "review"            # 叙述性综述
    OTHER = "other"              # 其他

    @property
    def zh(self) -> str:
        return {
            "guideline": "临床指南",
            "consensus": "专家共识",
            "systematic": "系统评价/Meta分析",
            "rct": "随机对照试验",
            "cohort": "队列研究",
            "case": "病例报告",
            "review": "综述",
            "other": "其他",
        }[self.value]

    @property
    def weight(self) -> int:
        """证据权重，用于重排时加权（数值越大越可信）。"""
        return {
            "guideline": 100,
            "consensus": 90,
            "systematic": 85,
            "rct": 80,
            "cohort": 60,
            "review": 50,
            "case": 40,
            "other": 30,
        }[self.value]


class DocumentType(str, Enum):
    """文献类型 —— 对应报审表「（一）多模态知识库构建模块」。"""

    LITERATURE = "literature"  # 期刊文献
    GUIDELINE = "guideline"    # 诊疗指南 / 操作规范
    CASE = "case"              # 典型病例
    TEXTBOOK = "textbook"      # 教材 / 专著章节
    IMAGE = "image"            # 术中影像资料


class SourceDatabase(str, Enum):
    """文献来源数据库。"""

    PUBMED = "pubmed"
    CNKI = "cnki"
    MANUAL = "manual"      # 人工录入（如报审表的 15 篇种子文献）
    GUIDELINE_OFFICIAL = "guideline_official"


class EntityType(str, Enum):
    """知识图谱实体类型 —— 对应报审表「疾病、术式、并发症、药物等实体」。"""

    DISEASE = "Disease"                # 疾病
    SURGERY = "Surgery"                # 术式
    COMPLICATION = "Complication"      # 并发症
    DRUG = "Drug"                      # 药物
    SYMPTOM = "Symptom"                # 症状/体征
    EXAM = "Examination"               # 检查/检验
    ANATOMY = "Anatomy"                # 解剖结构
    DEVICE = "Device"                  # 器械/耗材
    NUTRITION = "Nutrition"            # 营养支持


class RelationType(str, Enum):
    """知识图谱关系类型。"""

    HAS_COMPLICATION = "HAS_COMPLICATION"    # 术式 -> 并发症
    TREATS = "TREATS"                        # 药物 -> 疾病/并发症
    INDICATED_FOR = "INDICATED_FOR"          # 术式 -> 疾病（适应症）
    CONTRAINDICATED = "CONTRAINDICATED"      # 禁忌
    DIAGNOSED_BY = "DIAGNOSED_BY"            # 疾病 -> 检查
    PRESENTS_WITH = "PRESENTS_WITH"          # 疾病 -> 症状
    LOCATED_AT = "LOCATED_AT"                # 疾病 -> 解剖部位
    MANAGED_BY = "MANAGED_BY"                # 并发症 -> 处理措施
    REQUIRES = "REQUIRES"                    # 术式 -> 器械/营养


# 检索时默认召回的期别（不指定则全部）
ALL_PHASES: tuple[PerioperativePhase, ...] = (
    PerioperativePhase.PREOP,
    PerioperativePhase.INTRAOP,
    PerioperativePhase.POSTOP,
    PerioperativePhase.GENERAL,
)

# 中文证据等级展示名到枚举的反查表，供 PDF 解析时自动识别
EVIDENCE_KEYWORDS: dict[str, EvidenceLevel] = {
    "指南": EvidenceLevel.GUIDELINE,
    "规范": EvidenceLevel.GUIDELINE,
    "共识": EvidenceLevel.CONSENSUS,
    "系统评价": EvidenceLevel.SYSTEMATIC,
    "meta分析": EvidenceLevel.SYSTEMATIC,
    "meta-analysis": EvidenceLevel.SYSTEMATIC,
    "随机对照": EvidenceLevel.RCT,
    "randomized": EvidenceLevel.RCT,
    "队列": EvidenceLevel.COHORT,
    "病例报告": EvidenceLevel.CASE,
    "综述": EvidenceLevel.REVIEW,
}
