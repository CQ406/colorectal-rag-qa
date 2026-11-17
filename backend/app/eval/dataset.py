"""评测集的数据结构定义。

为什么需要评测集
----------------
自动评测（用文献标题反查自身）只能反映「已知条目查找」能力，
无法回答论文最关心的问题：**系统对真实临床提问的效果如何？**

因此阶段十构建人工可核验的评测集：

  - **检索指标**（精确客观，无需 LLM）：Recall@K / MRR / NDCG@K
    标注「正确答案所在的文本块」（gold chunk），
    检索指标即衡量该块是否被召回及其排名。

  - **生成指标**（LLM 裁判）：忠实度 / 答案相关性 / 引用准确率
    医学场景最重要的是**忠实度**（答案是否有据可依、无编造）。

标注方式与质量保证
------------------
采用「LLM 从文本块生成问题 + 人工抽查」的半自动方式：

  1. 从知识库挑选内容充实的文本块（长度 > 300 字符）
  2. 让 LLM 依据该块内容，以**临床医师的自然提问口吻**生成问题
  3. 关键约束：提示词明确要求**避免逐字摘抄原文术语**，
     否则问题与原文词汇重叠会虚高检索指标（词汇泄漏）
  4. 生成的块即 gold chunk，作为检索评测的客观依据
  5. 人工抽查若干条，确认问题可从该块回答

覆盖维度（保证评测代表性）：
  - 围术期分期：preop / intraop / postop / general
  - 问题类型：definition（定义）/ management（处理）/ 
              diagnosis（诊断）/ comparison（比较）/ evidence（证据）
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class EvalItem:
    """一条评测样本。"""

    qid: str                          # 唯一编号
    question: str                     # 问题文本
    gold_chunk_id: str                # 正确答案所在文本块（检索评测依据）
    gold_document_id: str
    gold_doc_title: str
    gold_page: int | None = None
    gold_evidence_level: str = "other"

    # 分类维度（用于分组统计与论文中的分层分析）
    phase: str = "general"
    question_type: str = "other"

    # 生成式指标所需的参考信息
    # 注：gold_span 为原文片段，供 LLM 裁判判断答案是否有据可依
    gold_span: str = ""
    # reference_answer 为可选的人工/LLM 参考要点，用于答案相关性判定
    reference_answer: str = ""

    # 生成来源（便于追溯与人工复核）
    generator: str = "llm"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalDataset:
    """评测集。"""

    name: str
    version: str
    description: str
    items: list[EvalItem] = field(default_factory=list)

    # 构建时的语料统计，论文中需报告
    corpus_documents: int = 0
    corpus_chunks: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "corpus_documents": self.corpus_documents,
            "corpus_chunks": self.corpus_chunks,
            "total": len(self.items),
            "items": [i.to_dict() for i in self.items],
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "EvalDataset":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            version=data["version"],
            description=data.get("description", ""),
            corpus_documents=data.get("corpus_documents", 0),
            corpus_chunks=data.get("corpus_chunks", 0),
            items=[EvalItem(**{k: v for k, v in it.items()
                               if k in EvalItem.__dataclass_fields__})
                   for it in data["items"]],
        )

    def distribution(self) -> dict:
        """返回分期与问题类型的分布，用于确认评测集代表性。"""
        by_phase: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_evidence: dict[str, int] = {}
        for item in self.items:
            by_phase[item.phase] = by_phase.get(item.phase, 0) + 1
            by_type[item.question_type] = by_type.get(item.question_type, 0) + 1
            by_evidence[item.gold_evidence_level] = (
                by_evidence.get(item.gold_evidence_level, 0) + 1
            )
        return {
            "total": len(self.items),
            "by_phase": by_phase,
            "by_question_type": by_type,
            "by_evidence_level": by_evidence,
        }


# 问题类型枚举（与提示词、统计口径保持一致）
QUESTION_TYPES = [
    "definition",    # 概念与定义（"什么是…"）
    "management",    # 处理与治疗（"如何处理…"）
    "diagnosis",     # 诊断与评估（"如何诊断/评估…"）
    "comparison",    # 比较与选择（"…与…有何区别"）
    "evidence",      # 证据与预后（"证据等级如何"、"预后怎样"）
]

QUESTION_TYPE_ZH = {
    "definition": "概念定义",
    "management": "处理治疗",
    "diagnosis": "诊断评估",
    "comparison": "比较选择",
    "evidence": "证据预后",
    "other": "其他",
}
