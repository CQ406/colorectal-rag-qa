"""医学实体与关系抽取。

报审表要求：「提取关键知识点并进行实体识别与关系抽取」、
「基于医学本体构建结直肠手术知识图谱，实现疾病、术式、并发症、药物等实体的语义关联」

为什么用 LLM 抽取而不是规则/BERT 模型
-------------------------------------
1. **无需标注数据**：本课题没有实体标注语料。训练 BERT 类模型需要数千条
   人工标注，而 LLM 零样本即可达到可用精度。
2. **本体可控**：实体类型与关系类型由提示词定义，调整本体无需重新训练。
3. **中文医学文本**：文献正文为英文，但抽取结果需要中文（与问题语言一致），
   LLM 可同时完成翻译与归一化。

代价是成本与速度：每条文本块一次 LLM 调用。因此需要**筛选待抽取的文本块**
（见 `select_chunks_for_extraction`），而非全量处理。

本体设计（7 类实体）
-------------------
  disease      疾病         结直肠癌、克罗恩病
  procedure    手术与操作    腹腔镜直肠癌根治术、回肠造口术
  complication 并发症        吻合口漏、肠梗阻、吻合口出血
  drug         药物与制剂    抗生素、生长抑素、ICG
  anatomy      解剖部位      直肠、乙状结肠、肠系膜下动脉
  exam         检查与评估     CT、MRI、内镜、荧光血管造影
  metric       指标与结局     中转率、住院时间、并发症发生率

10 类关系覆盖临床推理路径
------------------------
  CAUSES_COMPLICATION   疾病 → 并发症
  TREATS                手术/药物/检查 → 疾病
  PREVENTS              手术/药物 → 并发症
  DIAGNOSES             检查 → 疾病/并发症
  LOCATED_AT            疾病/手术 → 解剖部位
  USES_DRUG             手术 → 药物
  COMPARES_WITH         手术 → 手术
  HAS_RISK_FACTOR       疾病/手术 → 疾病
  MEASURES              检查 → 指标
  APPLIES_TO            手术 → 疾病
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.services.graph_client import GraphEntity, GraphRelation
from app.services.llm_client import LLMClient, LLMError

logger = get_logger("services.extraction")

# 实体类型（与提示词、校验逻辑保持一致）
ENTITY_TYPES = {
    "disease", "procedure", "treatment", "complication", "drug",
    "anatomy", "exam", "metric",
}

ENTITY_TYPE_ZH = {
    "disease": "疾病", "procedure": "手术操作", "treatment": "非手术治疗",
    "complication": "并发症", "drug": "药物", "anatomy": "解剖部位",
    "exam": "检查", "metric": "指标",
}

# 关系类型白名单：只有这些类型会被写入图谱。
# 白名单是必需的 —— 关系类型会被拼进 Cypher 语句，不能信任模型输出。
RELATION_TYPES = {
    "CAUSES_COMPLICATION", "TREATS", "PREVENTS", "DIAGNOSES", "LOCATED_AT",
    "USES_DRUG", "COMPARES_WITH", "HAS_RISK_FACTOR", "MEASURES", "APPLIES_TO",
}


EXTRACTION_SYSTEM_PROMPT = """你是医学知识图谱构建专家。请从给定的文献片段中抽取实体与关系。

## 实体类型（只能使用这 8 种）

- disease       疾病（如 结直肠癌、克罗恩病）
- procedure     **手术**操作（如 腹腔镜直肠癌根治术、回肠造口术、全直肠系膜切除术）
- treatment     **非手术**治疗手段（如 放化疗、化疗、放疗、免疫治疗、靶向治疗、新辅助治疗）
- complication  并发症（如 吻合口漏、肠梗阻）
- drug          药物与制剂（如 抗生素、生长抑素、吲哚菁绿）
- anatomy       解剖部位（如 直肠、乙状结肠、肠系膜下动脉）
- exam          检查与评估手段（如 CT、MRI、内镜、荧光血管造影）
- metric        指标与结局（如 中转率、住院时间、并发症发生率）

⚠️ **procedure 与 treatment 必须严格区分**：procedure 仅指外科手术操作；
放疗、化疗、免疫治疗等非手术手段一律归为 treatment。

## 关系类型（只能使用这 10 种）

- CAUSES_COMPLICATION  疾病 → 并发症
- TREATS               手术/治疗/药物/检查 → 疾病
- PREVENTS             手术/治疗/药物 → 并发症
- DIAGNOSES            检查 → 疾病/并发症
- LOCATED_AT           疾病/手术 → 解剖部位
- USES_DRUG            手术 → 药物
- COMPARES_WITH        手术/治疗 → 手术/治疗
- HAS_RISK_FACTOR      疾病/手术 → 疾病
- MEASURES             检查 → 指标
- APPLIES_TO           手术/治疗 → 疾病

## 抽取要求

1. **实体名称用中文**。文献是英文时翻译为规范中文医学术语
   （如 anastomotic leak → 吻合口漏，total mesorectal excision → 全直肠系膜切除术）。
2. **只抽取片段中确实提到的内容**。不要用你的医学知识补充片段中没有的实体或关系。
3. **名称要归一化**：同一概念用统一表述，不要包含修饰语。
   正例："吻合口漏"   反例："术后的吻合口漏"、"严重的吻合口漏"
4. **关系必须有片段依据**。片段未明确表达的关系不要输出。
5. 每个片段最多抽 12 个实体、12 条关系。优先抽取重要的。

## 输出格式

只输出 JSON 对象，不要其他文字：

{
  "items": [
    {
      "chunk_index": 1,
      "entities": [
        {"name": "结直肠癌", "type": "disease", "aliases": ["colorectal cancer"]},
        {"name": "吻合口漏", "type": "complication", "aliases": ["anastomotic leak"]}
      ],
      "relations": [
        {"source": "结直肠癌", "target": "吻合口漏",
         "type": "CAUSES_COMPLICATION", "source_type": "disease",
         "target_type": "complication"}
      ]
    }
  ]
}

注意：relations 中的 source / target 必须出现在同一片段的 entities 列表中。"""


@dataclass
class ExtractedChunk:
    """一个文本块的抽取结果。"""

    chunk_id: str
    entities: list[GraphEntity] = field(default_factory=list)
    relations: list[GraphRelation] = field(default_factory=list)


def build_extraction_prompt(chunks: list[dict]) -> str:
    """构造批量抽取提示词。"""
    blocks = []
    for i, c in enumerate(chunks, start=1):
        title = c.get("doc_title") or ""
        blocks.append(f"[片段 {i}] 来源：{title}\n{c['content']}")
    joined = "\n\n---\n\n".join(blocks)
    return (
        f"请从以下 {len(chunks)} 个文献片段中各抽取实体与关系。\n\n"
        f"{joined}\n\n"
        f"请输出 JSON 对象，items 数组含 {len(chunks)} 个元素。"
    )


def _parse_json(text: str) -> dict | None:
    """解析模型输出为 dict。"""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("抽取结果 JSON 解析失败: %s", str(exc)[:120])
        return None
    return data if isinstance(data, dict) else None


def _normalize_name(name: str) -> str:
    """归一化实体名称。

    去掉常见修饰前缀与首尾标点，保证同一概念不会因表述差异产生多个节点。

    为什么必须做：LLM 有时输出"术后的吻合口漏"、有时输出"吻合口漏"，
    不归一化会让图谱被近义表述碎片化，多跳查询随即失效。
    """
    s = (name or "").strip()
    # 去掉括号补充说明，如 "吻合口漏（AL）"
    s = re.sub(r"[（(][^）)]*[）)]", "", s).strip()
    # 去掉修饰性前后缀
    for prefix in ("术后的", "术前的", "严重的", "常见的", "急性的", "慢性的",
                   "可能的", "该", "此种", "此种类型的"):
        if s.startswith(prefix) and len(s) > len(prefix) + 1:
            s = s[len(prefix) :]
    for suffix in ("的发生", "的处理", "的治疗", "等并发症"):
        if s.endswith(suffix) and len(s) > len(suffix) + 1:
            s = s[: -len(suffix)]
    return s.strip(" 　.,;:、，。；：-—_")


def extract_chunk(
    chunk: dict,
    client: LLMClient,
    *,
    max_tokens: int = 8192,
    max_retries: int = 3,
) -> ExtractedChunk:
    """抽取单个文本块。

    单独一块抽取（而非批量）的原因：实测批量送多块时，
    推理模型的思考过程会随输入长度显著增长，容易在推理阶段耗尽
    max_tokens 而返回空内容（与阶段十评测集生成时遇到的同一问题）。
    单块抽取虽增加调用次数，但成功率明显更高。
    """
    result = ExtractedChunk(chunk_id=chunk["chunk_id"])
    try:
        chat = client.chat(
            [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": build_extraction_prompt([chunk])},
            ],
            temperature=0.1,
            max_retries=max_retries,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except LLMError as exc:
        logger.warning("抽取失败 [%s]: %s", chunk["chunk_id"][:8], str(exc)[:120])
        return result

    data = _parse_json(chat.content)
    if not data:
        return result

    items = data.get("items")
    if not isinstance(items, list) or not items:
        return result

    item = items[0]
    if not isinstance(item, dict):
        return result

    # ---- 实体 ----
    valid_names: dict[str, str] = {}   # name -> type
    for raw in item.get("entities") or []:
        if not isinstance(raw, dict):
            continue
        etype = raw.get("type")
        if etype not in ENTITY_TYPES:
            continue
        name = _normalize_name(raw.get("name") or "")
        if not name or len(name) > 60:
            continue
        aliases = [
            a.strip() for a in (raw.get("aliases") or [])
            if isinstance(a, str) and a.strip()
        ][:5]
        valid_names[name] = etype
        result.entities.append(
            GraphEntity(
                name=name,
                type=etype,
                norm=name.lower(),
                aliases=aliases,
                chunk_ids=[chunk["chunk_id"]],
                frequency=1,
            )
        )

    # ---- 关系 ----
    # 只保留两端都出现在本块实体列表中的关系。
    # 这是防止 LLM 编造跨块关联的关键校验：
    # 若 source 或 target 不在 entities 中，该关系就没有本块依据。
    for raw in item.get("relations") or []:
        if not isinstance(raw, dict):
            continue
        rtype = raw.get("type")
        if rtype not in RELATION_TYPES:
            continue
        src = _normalize_name(raw.get("source") or "")
        dst = _normalize_name(raw.get("target") or "")
        if not src or not dst or src == dst:
            continue
        if src not in valid_names or dst not in valid_names:
            logger.debug("丢弃无依据关系 %s -[%s]-> %s", src, rtype, dst)
            continue
        result.relations.append(
            GraphRelation(
                source=src,
                target=dst,
                rel_type=rtype,
                source_type=valid_names[src],
                target_type=valid_names[dst],
                chunk_ids=[chunk["chunk_id"]],
                weight=1,
            )
        )

    return result


# ----------------------------------------------------------------------
#  待抽取文本块筛选
# ----------------------------------------------------------------------
# 优先抽取的文献类型：高证据等级文献的表述更规范，抽取质量更高
PRIORITY_EVIDENCE = ("guideline", "consensus", "systematic", "rct", "review")


def select_chunks_for_extraction(
    db: Session,
    limit: int = 400,
    min_chars: int = 300,
    seed: int = 42,
    evidence_levels: tuple[str, ...] | None = None,
    max_per_document: int = 3,
) -> list[dict]:
    """筛选用于图谱构建的文本块。

    为什么不全量抽取（1661 块）：
      每块一次 LLM 调用，全量约需 1661 次调用（约 2~3 小时、消耗大量额度）。
      而图谱的价值集中在**核心概念与关系**上，这些在正文块中反复出现，
      抽样 400 块已能覆盖主要实体。

    筛选策略：
      1. 长度适中（信息量足够但不至于过长）
      2. **优先高证据等级文献**（指南/共识/系统评价的表述更规范）
      3. 按文献去重（每篇最多 3 块），保证概念来源多样

    参数:
      evidence_levels: 只抽取这些证据等级的块（如 ``("guideline",)``）。
          用于「分阶段扩图」——先覆盖指南/共识验证增益，再决定是否扩全量。
          None 表示不限。
      max_per_document: 每篇文献最多抽多少块。扩图时必须放大：
          30 篇指南 × 3 块只有 90 块，覆盖率依旧极低。
    """
    from app.models.knowledge import Chunk, Document

    conditions = [
        Chunk.char_count >= min_chars,
        Chunk.char_count <= 1500,
        Document.is_active.is_(True),
    ]
    if evidence_levels:
        conditions.append(Chunk.evidence_level.in_(list(evidence_levels)))

    stmt = (
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .where(*conditions)
    )
    rows = db.execute(stmt).all()

    candidates: list[tuple[int, dict]] = []
    for chunk, doc in rows:
        section = (chunk.section or "").strip().lower()
        if section == "title":
            continue
        # 跳过与文献标题高度重合的块
        if doc.title and chunk.content.strip()[:40] == doc.title.strip()[:40]:
            continue

        # 优先级：高证据等级 + 有具体分期标签
        priority = 0
        if chunk.evidence_level in PRIORITY_EVIDENCE:
            priority += 2
        if chunk.evidence_level in ("guideline", "consensus"):
            priority += 2
        if chunk.phase and chunk.phase != "general":
            priority += 1

        candidates.append(
            (
                priority,
                {
                    "chunk_id": str(chunk.id),
                    "document_id": str(doc.id),
                    "content": chunk.content,
                    "doc_title": doc.title or "",
                    "evidence_level": chunk.evidence_level,
                    "phase": chunk.phase,
                },
            )
        )

    # 高优先级优先，同级内打散
    import random

    rng = random.Random(seed)
    candidates.sort(key=lambda x: -x[0])
    # 按优先级分桶后再各自打散，保证高优先级块一定入选
    buckets: dict[int, list[dict]] = {}
    for priority, item in candidates:
        buckets.setdefault(priority, []).append(item)
    ordered: list[dict] = []
    for priority in sorted(buckets, reverse=True):
        bucket = buckets[priority]
        rng.shuffle(bucket)
        ordered.extend(bucket)

    # 按文献去重
    per_doc: dict[str, int] = {}
    selected: list[dict] = []
    for item in ordered:
        did = item["document_id"]
        if per_doc.get(did, 0) >= max_per_document:
            continue
        per_doc[did] = per_doc.get(did, 0) + 1
        selected.append(item)
        if len(selected) >= limit:
            break

    logger.info(
        "选中 %d 个文本块用于图谱抽取（来自 %d 篇文献）",
        len(selected), len(per_doc),
    )
    return selected
