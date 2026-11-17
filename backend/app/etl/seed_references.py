"""报审表 15 篇参考文献清单。

用途：
  1. 作为知识库的首批权威语料（开题阶段即可跑通链路）
  2. 中文指南/共识无法从 PubMed 获取，直接按题录入库并人工提供摘要

说明：
  - 英文文献通过 PubMed 按标题检索，自动补全 PMID、摘要、MeSH 等字段
  - 中文文献 PubMed 未收录，使用 `manual` 来源入库，摘要需人工补充或留空
  - 每条记录标注预期分期与证据等级，入库后与自动标注结果对比，
    用于评估自动标注的准确率（论文实验素材）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SeedReference:
    """种子文献。"""

    index: int
    title: str
    authors: str | None = None
    journal: str | None = None
    year: int | None = None
    language: str = "en"
    source: str = "pubmed"          # pubmed / manual
    expected_phase: str = "general"  # 预期分期，用于评估自动标注
    expected_evidence: str = "other"  # 预期证据等级
    abstract: str | None = None      # 中文文献需人工提供摘要


SEED_REFERENCES: list[SeedReference] = [
    SeedReference(
        index=1,
        title="Systematic review of enhanced recovery programmes in colonic surgery",
        authors="Wind J, Polle SW, Fung Kon Jin PHP, et al.",
        journal="British Journal of Surgery",
        year=2006,
        expected_phase="postop",
        expected_evidence="systematic",
    ),
    SeedReference(
        index=2,
        title="中国结直肠癌诊疗规范(2023版)",
        authors="中华人民共和国国家卫生健康委员会",
        journal="中华消化外科杂志",
        year=2023,
        language="zh",
        source="manual",
        expected_phase="general",
        expected_evidence="guideline",
        # 中文指南：此处录入关键条目作为知识素材（公开规范内容）
        abstract=(
            "结直肠癌诊疗规范涵盖筛查、诊断、分期、治疗与随访全流程。"
            "术前应完成结肠镜与病理活检确诊，行胸腹盆增强CT评估分期，"
            "直肠癌推荐盆腔高分辨率MRI评估环周切缘。"
            "术后病理分期是辅助治疗决策的核心依据。"
            "对于可切除的结直肠癌，推荐行根治性手术切除并清扫区域淋巴结，"
            "淋巴结检出数应不少于12枚。"
            "术后随访建议：术后2年内每3个月复查一次，"
            "第3~5年每6个月复查一次，包括肿瘤标志物与影像学检查。"
        ),
    ),
    SeedReference(
        index=3,
        title="Attention is all you need",
        authors="Vaswani A, Shazeer N, Parmar N, et al.",
        journal="Advances in Neural Information Processing Systems",
        year=2017,
        source="manual",
        expected_phase="general",
        expected_evidence="other",
        abstract=(
            "The dominant sequence transduction models are based on complex recurrent "
            "or convolutional neural networks that include an encoder and a decoder. "
            "We propose a new simple network architecture, the Transformer, based solely "
            "on attention mechanisms, dispensing with recurrence and convolutions entirely."
        ),
    ),
    SeedReference(
        index=4,
        title="Retrieval-augmented generation for knowledge-intensive NLP tasks",
        authors="Lewis P, Perez E, Piktus A, et al.",
        journal="Advances in Neural Information Processing Systems",
        year=2020,
        source="manual",
        expected_phase="general",
        expected_evidence="other",
        abstract=(
            "Large pre-trained language models have been shown to store factual knowledge "
            "in their parameters, and achieve state-of-the-art results when fine-tuned on "
            "downstream NLP tasks. However, their ability to access and precisely manipulate "
            "knowledge is still limited. We explore a general-purpose fine-tuning recipe for "
            "retrieval-augmented generation (RAG) -- models which combine pre-trained "
            "parametric and non-parametric memory for language generation."
        ),
    ),
    SeedReference(
        index=5,
        title="从加速康复外科到预康复——理念更新与临床实践模式转变",
        authors="李幼生",
        journal="中国实用外科杂志",
        year=2024,
        language="zh",
        source="manual",
        expected_phase="preop",
        expected_evidence="review",
        abstract=(
            "加速康复外科（ERAS）通过优化围术期处理措施减少手术应激，"
            "已显著改善结直肠手术患者的预后。预康复是在ERAS基础上进一步前移干预窗口，"
            "在术前对患者进行体能、营养与心理三方面的优化。"
            "术前预康复包括有氧运动训练、呼吸功能锻炼、营养支持与焦虑干预，"
            "可提高患者的功能储备，降低术后并发症发生率与住院时间。"
        ),
    ),
    SeedReference(
        index=6,
        title="Retrieval-augmented generation for large language models: a survey",
        authors="Gao Y, Xiong Y, Gao X, et al.",
        journal="arXiv:2312.10997",
        year=2023,
        language="en",
        source="manual",
        expected_phase="general",
        expected_evidence="review",
        abstract=(
            "Retrieval-Augmented Generation (RAG) has emerged as a promising technique "
            "that enhances large language models by incorporating external knowledge. "
            "This survey reviews the development of RAG paradigms, including Naive RAG, "
            "Advanced RAG, and Modular RAG, and summarizes the three fundamental components: "
            "retrieval, generation, and augmentation."
        ),
    ),
    SeedReference(
        index=7,
        title="结直肠手术后并发症的卫生经济学分析",
        authors="魏天笑, 吴舟桥, 季加孚",
        journal="中华消化外科杂志",
        year=2023,
        language="zh",
        source="manual",
        expected_phase="postop",
        expected_evidence="cohort",
        abstract=(
            "结直肠手术后并发症显著增加医疗费用与住院时间。"
            "吻合口漏是最昂贵的并发症之一，可使住院费用增加数倍。"
            "术后感染性并发症、肠梗阻与肺部感染同样带来显著经济负担。"
            "通过加速康复外科路径与规范化并发症防治，可有效降低总体医疗成本。"
        ),
    ),
    SeedReference(
        index=8,
        title="A review on knowledge graphs for healthcare: resources, applications, and promises",
        authors="Cui H, Lu J, Xu R, et al.",
        journal="Journal of Biomedical Informatics",
        year=2025,
        language="en",
        source="manual",
        expected_phase="general",
        expected_evidence="review",
        abstract=(
            "Knowledge graphs (KGs) have become a powerful tool in healthcare for "
            "organizing heterogeneous medical data and supporting clinical decision-making. "
            "This review surveys healthcare KG resources, construction methodologies, and "
            "applications including drug repurposing, clinical decision support, and "
            "question answering."
        ),
    ),
    SeedReference(
        index=9,
        title="成人肠外营养脂肪乳注射液临床应用指南(2023版)",
        authors="中华医学会肠外肠内营养学分会",
        journal="中华消化外科杂志",
        year=2023,
        language="zh",
        source="manual",
        expected_phase="postop",
        expected_evidence="guideline",
        abstract=(
            "肠外营养是围术期营养支持的重要手段。"
            "对于无法经口或肠内途径满足营养需求的患者，应给予肠外营养支持。"
            "脂肪乳注射液是肠外营养的重要组成部分，提供必需脂肪酸与能量。"
            "围术期应评估患者的营养风险，对存在营养不良的患者给予营养支持，"
            "推荐优先选择肠内营养，肠内营养不足时联合肠外营养。"
        ),
    ),
    SeedReference(
        index=10,
        title=(
            "Improving large language model applications in biomedicine with "
            "retrieval-augmented generation: a systematic review, meta-analysis, "
            "and clinical development guidelines"
        ),
        authors="Liu S, McCoy AB, Wright A",
        journal="Journal of the American Medical Informatics Association",
        year=2025,
        language="en",
        source="manual",
        expected_phase="general",
        expected_evidence="systematic",
        abstract=(
            "Retrieval-augmented generation (RAG) is increasingly applied to biomedical "
            "question answering. This systematic review and meta-analysis evaluates the "
            "effectiveness of RAG in biomedical applications and proposes clinical "
            "development guidelines, emphasizing the importance of source attribution, "
            "hallucination mitigation, and clinical validation before deployment."
        ),
    ),
    SeedReference(
        index=11,
        title=(
            "Mechanical bowel preparation and antibiotics in elective colorectal "
            "surgery: network meta-analysis"
        ),
        authors="Tan J, Ryan EJ, Davey MG, et al.",
        journal="BJS Open",
        year=2023,
        language="en",
        source="manual",
        expected_phase="preop",
        expected_evidence="systematic",
        abstract=(
            "The role of mechanical bowel preparation (MBP) combined with oral antibiotics "
            "in elective colorectal surgery remains debated. This network meta-analysis "
            "compared MBP alone, oral antibiotics alone, and combined regimens. "
            "Combined mechanical and oral antibiotic bowel preparation was associated with "
            "reduced rates of surgical site infection and anastomotic leak compared with "
            "mechanical bowel preparation alone."
        ),
    ),
    SeedReference(
        index=12,
        title=(
            "Large language model uncertainty proxies: discrimination and calibration "
            "for medical diagnosis and treatment"
        ),
        authors="Savage T, Wang J, Gallo R, et al.",
        journal="Journal of the American Medical Informatics Association",
        year=2025,
        language="en",
        source="manual",
        expected_phase="general",
        expected_evidence="cohort",
        abstract=(
            "Large language models (LLMs) are increasingly used for medical question "
            "answering, but their confidence estimates are poorly calibrated. This study "
            "evaluates uncertainty proxies for LLM outputs in diagnosis and treatment "
            "recommendations, finding that verbalized confidence and token-level "
            "probabilities discriminate correct from incorrect answers only modestly, "
            "highlighting the need for external verification mechanisms."
        ),
    ),
    SeedReference(
        index=13,
        title="Benchmarking retrieval-augmented generation for medicine",
        authors="Xiong G, Jin Q, Lu Z, et al.",
        journal="arXiv:2402.13178",
        year=2024,
        language="en",
        source="manual",
        expected_phase="general",
        expected_evidence="review",
        abstract=(
            "We present MIRAGE, a benchmark for retrieval-augmented generation in medicine "
            "comprising 7,663 questions from five medical QA datasets. Our evaluation of "
            "41 retrieval-augmented LLMs shows that retrieval augmentation improves "
            "accuracy, with BM25 and MedCPT retrievers performing well, and reveals "
            "substantial room for improvement in medical RAG systems."
        ),
    ),
    SeedReference(
        index=14,
        title="经自然腔道取标本机器人手术系统直肠癌手术的质量控制",
        authors="刘东宁, 余宏鑫, 李太原",
        journal="中华消化外科杂志",
        year=2023,
        language="zh",
        source="manual",
        expected_phase="intraop",
        expected_evidence="review",
        abstract=(
            "经自然腔道取标本手术（NOSES）联合机器人手术系统用于直肠癌根治，"
            "具有创伤小、术后疼痛轻、恢复快的优势。"
            "质量控制要点包括：严格掌握手术适应证、规范淋巴结清扫范围、"
            "保证标本的完整性与环周切缘阴性、无瘤操作技术、"
            "以及术中自主神经的保护。"
        ),
    ),
    SeedReference(
        index=15,
        title=(
            "AUGUR-AI: a real-time artificial intelligence interpreter for "
            "indocyanine green fluorescence angiography in colorectal surgery"
        ),
        authors="McEntee PD, Delaney C, Guan S, et al.",
        journal="Surgical Endoscopy",
        year=2025,
        language="en",
        source="manual",
        expected_phase="intraop",
        expected_evidence="cohort",
        abstract=(
            "Indocyanine green (ICG) fluorescence angiography is used intraoperatively to "
            "assess bowel perfusion and reduce anastomotic leak rates in colorectal surgery. "
            "AUGUR-AI is a real-time artificial intelligence system that interprets ICG "
            "fluorescence angiography videos, providing objective perfusion assessment "
            "to support intraoperative decision-making regarding anastomotic site selection."
        ),
    ),
]


def stats() -> dict:
    """返回种子清单的统计信息。"""
    total = len(SEED_REFERENCES)
    by_source: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    by_evidence: dict[str, int] = {}
    for ref in SEED_REFERENCES:
        by_source[ref.source] = by_source.get(ref.source, 0) + 1
        by_phase[ref.expected_phase] = by_phase.get(ref.expected_phase, 0) + 1
        by_evidence[ref.expected_evidence] = by_evidence.get(ref.expected_evidence, 0) + 1
    return {
        "total": total,
        "by_source": by_source,
        "by_phase": by_phase,
        "by_evidence": by_evidence,
    }
