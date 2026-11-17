"""评测集生成器：从知识库文本块反向生成临床问题。

为什么用「反向生成」而非人工编写
--------------------------------
人工编写 200 道有标准来源的临床问题需要数天，且难以客观标注
「正确答案在哪一段」。反向生成从文本块出题，gold chunk 天然确定，
检索指标因此完全客观可复现。

关键质量控制：避免词汇泄漏
--------------------------
若直接让 LLM「根据这段文字提一个问题」，它往往逐字摘抄原文术语，
导致问题与原文词汇高度重叠 —— 关键词检索会虚高，评测失去意义。

因此提示词强制要求：
  1. 用临床医师的**自然提问口吻**（"怎么处理""要不要做""有什么区别"）
  2. **不得照抄原文的完整短语**，需换用同义表达
  3. 问题必须能**仅凭该片段**回答
  4. 覆盖多种问题类型（定义/处理/诊断/比较/证据）

并在提示词中要求标注问题类型与所属围术期分期。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.eval.dataset import (
    QUESTION_TYPES,
    EvalDataset,
    EvalItem,
)
from app.services.llm_client import LLMClient, LLMError

logger = get_logger("eval.generate")


# ----------------------------------------------------------------------
#  提示词
# ----------------------------------------------------------------------
SYSTEM_PROMPT = """你是医学教育测评专家，负责为结直肠手术围术期问答系统构建评测集。

你的任务：依据给定的文献片段，生成一道**临床医师会真实提出的问题**。

## 严格要求

1. **口语化提问**：用临床医师的自然口吻，例如
   - "吻合口漏一般怎么处理？"
   - "术前要不要做机械性肠道准备？"
   - "这两个术式在并发症上有什么区别？"
   **不要**写成教科书式的填空题或名词解释题。

2. **禁止词汇泄漏（最重要）**：不得照抄原文的完整短语或连续术语组合。
   必须换用同义表达或更口语的说法。
   反例（照抄原文，不合格）："ERAS 是多学科、多模式、以患者为中心的照护模式吗？"
   正例（改写，合格）："加速康复外科在组织架构上有什么特点？"

3. **可回答性**：问题必须能**仅凭给定片段**回答。不要问片段中没有的内容。

4. **问题类型**：从以下五种中选最贴切的一种
   - definition   概念与定义
   - management   处理与治疗
   - diagnosis    诊断与评估
   - comparison   比较与选择
   - evidence     证据、效果与预后

5. **围术期分期**：标注问题主要涉及哪个阶段
   - preop 术前 / intraop 术中 / postop 术后 / general 通用

## 输出格式

只输出如下 JSON 对象，不要任何其他文字：

{
  "items": [
    {
      "chunk_index": 1,
      "question": "改写后的自然提问",
      "question_type": "management",
      "phase": "postop",
      "reference_points": ["参考要点1", "参考要点2", "参考要点3"],
      "answerable": true
    }
  ]
}

说明：
- `chunk_index` 对应输入的片段编号
- `reference_points` 为该问题的参考要点，3~5 条，用于后续评判答案完整性
- 若某片段内容不足以出一道有临床意义的题，令其 "answerable": false"""


# 英文出题提示词（与中文版严格对照，仅语言不同）
ENGLISH_QUESTION_RULE = """

## 语言要求（本次覆盖所有其他语言设定）

**必须用英文提问**，且必须是临床医师会真实问出的自然英文，
而非照抄论文标题或摘要句式。例如：
  - "How is anastomotic leak usually managed?"
  - "Is mechanical bowel preparation necessary before colorectal surgery?"
  - "What are the differences in complications between these two approaches?"

`reference_points` 同样用英文书写。`question_type` 与 `phase` 的取值保持不变
（仍为 definition/management/diagnosis/comparison/evidence 与
preop/intraop/postop/general）。"""


def build_user_prompt(chunks: list[dict]) -> str:
    """构造批量出题的用户提示词。"""
    blocks = []
    for i, c in enumerate(chunks, start=1):
        level = c.get("evidence_level", "other")
        title = c.get("doc_title", "")
        journal = c.get("doc_journal") or ""
        year = c.get("doc_year") or ""
        meta = f"{journal} {year}".strip()
        blocks.append(
            f"[片段 {i}] 来源：{title}（{meta}）| 证据等级：{level}\n{c['content']}"
        )
    joined = "\n\n---\n\n".join(blocks)
    return (
        f"请依据以下 {len(chunks)} 个文献片段，各生成一道临床问题。\n\n"
        f"{joined}\n\n"
        f"请输出 JSON 数组，共 {len(chunks)} 个元素（或标记 answerable=false）。"
    )


# ----------------------------------------------------------------------
#  JSON 解析（容忍模型输出包裹在代码块中）
# ----------------------------------------------------------------------
def parse_json_array(text: str) -> list[dict]:
    """从模型输出中提取题目列表。

    兼容三种常见返回形式：
      1. {"items": [...]}          —— 提示词要求的形式（json_object 模式）
      2. [...]                      —— 裸数组
      3. ```json ... ``` 代码块包裹
    """
    if not text:
        return []
    cleaned = text.strip()

    # 去掉可能的 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()

    # 先尝试整体解析（对象或数组）
    for candidate in (cleaned, _slice_json(cleaned, "{", "}"), _slice_json(cleaned, "[", "]")):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 兼容 {"items": [...]} 与 {"questions": [...]} 等键名
            for key in ("items", "questions", "data", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
            # 单个对象也接受
            if "question" in data:
                return [data]

    logger.warning("JSON 解析失败，输出前 200 字符: %s", cleaned[:200])
    return []


def _slice_json(text: str, open_ch: str, close_ch: str) -> str | None:
    """截取首个 open_ch 到末个 close_ch 之间的内容。"""
    start = text.find(open_ch)
    end = text.rfind(close_ch)
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


# ----------------------------------------------------------------------
#  候选文本块选择
# ----------------------------------------------------------------------
@dataclass
class CandidateChunk:
    """候选标注源。"""

    chunk_id: str
    document_id: str
    content: str
    phase: str
    evidence_level: str
    doc_title: str
    doc_journal: str | None
    doc_year: int | None
    page_start: int | None
    char_count: int


def select_candidates(
    db: Session,
    limit: int,
    min_chars: int = 300,
    max_chars: int = 1500,
    seed: int = 42,
) -> list[CandidateChunk]:
    """挑选适合出题的文本块。

    条件：
      - 长度适中（太短信息不足，太长出题过于宽泛）
      - 优先正文段落而非标题（标题块内容为文献题目，不适合出临床问题）
      - 排版章节多样性，避免集中在少数文献
    """
    from app.models.knowledge import Chunk, Document

    stmt = (
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .where(
            Chunk.char_count >= min_chars,
            Chunk.char_count <= max_chars,
            Document.is_active.is_(True),
        )
    )
    rows = db.execute(stmt).all()

    # 过滤掉标题类文本块（section 为 Title，或内容与文献标题高度重合）
    candidates: list[CandidateChunk] = []
    for chunk, doc in rows:
        section = (chunk.section or "").strip().lower()
        if section in ("title", ""):
            # section 为空时，用内容与标题的重合度判断
            if section == "title":
                continue
            if doc.title and chunk.content.strip()[:40] == doc.title.strip()[:40]:
                continue

        candidates.append(
            CandidateChunk(
                chunk_id=str(chunk.id),
                document_id=str(doc.id),
                content=chunk.content,
                phase=chunk.phase,
                evidence_level=chunk.evidence_level,
                doc_title=doc.title or "",
                doc_journal=doc.journal,
                doc_year=doc.year,
                page_start=chunk.page_start,
                char_count=chunk.char_count or len(chunk.content),
            )
        )

    # 打散后按文献去重（每篇文献最多贡献 2 个块，避免样本集中）
    random.Random(seed).shuffle(candidates)
    per_doc: dict[str, int] = {}
    selected: list[CandidateChunk] = []
    for c in candidates:
        if per_doc.get(c.document_id, 0) >= 2:
            continue
        per_doc[c.document_id] = per_doc.get(c.document_id, 0) + 1
        selected.append(c)
        if len(selected) >= limit:
            break

    logger.info(
        "候选文本块: %d 个（来自 %d 篇文献）", len(selected), len(per_doc)
    )
    return selected


# ----------------------------------------------------------------------
#  生成主流程
# ----------------------------------------------------------------------
def generate_dataset(
    db: Session,
    target: int = 200,
    batch_size: int = 8,
    seed: int = 42,
    out_path: str | None = None,
    language: str = "zh",
) -> EvalDataset:
    """生成评测集。

    流程：挑选候选块 → 分批让 LLM 出题 → 组装评测集。

    Args:
        target: 目标题目数
        batch_size: 每次请求送多少个片段（越大越省调用次数，但可能超长）
        language: 出题语言，zh（默认）/ en

            **为什么需要英文问题集**：本课题语料 99% 为英文文献，
            而中文口语化提问与英文文本的 bigram 词元**完全不可能重叠**
            （实测重叠中位数 0.000），导致关键词检索在跨语言场景下
            必然失效（实测 R@5 = 0.000）。因此需另建英文问题集，
            才能公平评估关键词检索的实际贡献，并揭示「跨语言检索」
            这一真实工程约束。
    """
    from app.repositories import search_repo

    candidates = select_candidates(db, limit=target, seed=seed)
    if not candidates:
        raise RuntimeError("没有可用的候选文本块，请确认知识库已入库")

    lang_zh = language == "zh"
    system_prompt = SYSTEM_PROMPT if lang_zh else SYSTEM_PROMPT + ENGLISH_QUESTION_RULE

    # 语料统计（论文需报告）
    n_doc = search_repo.count_active_documents(db)
    n_chunk = search_repo.count_chunks(db)

    dataset = EvalDataset(
        name=(
            "结直肠手术围术期问答评测集"
            if lang_zh
            else "Colorectal Surgery Perioperative QA Benchmark"
        ),
        version="1.0",
        description=(
            f"从知识库文本块反向生成的临床问题（语言={language}），"
            "gold chunk 作为检索评测依据。提示词强制要求改写以避免词汇泄漏。"
        ),
        corpus_documents=n_doc,
        corpus_chunks=n_chunk,
    )

    client = LLMClient()
    used_chunks: set[str] = set()
    qid_seq = 0

    def consume(batch: list[CandidateChunk], items: list[dict]) -> int:
        """把 LLM 输出转成评测样本，返回新增题数。"""
        nonlocal qid_seq
        added = 0
        for obj in items:
            if not isinstance(obj, dict):
                continue
            if obj.get("answerable") is False:
                continue

            idx = obj.get("chunk_index")
            if not isinstance(idx, int) or not (1 <= idx <= len(batch)):
                continue
            chunk = batch[idx - 1]
            question = (obj.get("question") or "").strip()
            if len(question) < 8:
                continue

            qtype = obj.get("question_type")
            if qtype not in QUESTION_TYPES:
                qtype = "other"
            phase = obj.get("phase")
            if phase not in ("preop", "intraop", "postop", "general"):
                phase = chunk.phase

            qid_seq += 1
            used_chunks.add(chunk.chunk_id)
            dataset.items.append(
                EvalItem(
                    qid=f"q{qid_seq:04d}",
                    question=question,
                    gold_chunk_id=chunk.chunk_id,
                    gold_document_id=chunk.document_id,
                    gold_doc_title=chunk.doc_title,
                    gold_page=chunk.page_start,
                    gold_evidence_level=chunk.evidence_level,
                    phase=phase,
                    question_type=qtype,
                    gold_span=chunk.content[:1200],
                    reference_answer=_as_text(obj.get("reference_points")),
                    generator="llm",
                )
            )
            added += 1
        return added

    try:
        total_batches = (len(candidates) + batch_size - 1) // batch_size
        for b in range(total_batches):
            if len(dataset.items) >= target:
                break

            batch = candidates[b * batch_size : (b + 1) * batch_size]
            # 与其他批次共用的块不重复出题（避免同一块多题导致指标虚高）
            batch = [c for c in batch if c.chunk_id not in used_chunks]
            if not batch:
                continue

            logger.info(
                "出题批次 %d/%d（%d 个片段）", b + 1, total_batches, len(batch)
            )
            added = _generate_batch(
                client, batch, dataset, consume, system_prompt=system_prompt
            )
            logger.info("  累计生成 %d 题（本批 +%d）", len(dataset.items), added)
    finally:
        client.close()

    if out_path:
        dataset.save(out_path)
        logger.info("评测集已保存: %s", out_path)

    dist = dataset.distribution()
    logger.info(
        "生成完成: %d 题 | 分期 %s | 类型 %s",
        dist["total"], dist["by_phase"], dist["by_question_type"],
    )
    return dataset


def _as_text(value) -> str:
    """把 LLM 返回的参考要点统一转成字符串。

    模型有时返回字符串，有时返回字符串数组（"reference_points": [...]），
    必须同时兼容，否则会抛 AttributeError 中断整个生成流程。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for v in value:
            if isinstance(v, str):
                parts.append(v.strip())
            elif isinstance(v, dict):
                parts.append(json.dumps(v, ensure_ascii=False))
            else:
                parts.append(str(v))
        return "；".join(p for p in parts if p)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _generate_batch(
    client: LLMClient,
    batch: list[CandidateChunk],
    dataset: EvalDataset,
    consume,
    depth: int = 0,
    system_prompt: str = SYSTEM_PROMPT,
) -> int:
    """为一批片段出题；失败时自动拆半重试。

    为什么要拆半重试：实测一次送 8 个片段时，
    推理模型（deepseek-flash）的思考过程会达到 15000+ 字符，
    在 max_tokens=4096 下推理阶段即耗尽额度，返回空内容
    （finish_reason='length'）。拆成更小的批次可显著降低失败率。

    Args:
        system_prompt: 系统提示词（中文或英文出题规则）
    """
    if not batch:
        return 0

    payload = [
        {
            "content": c.content,
            "doc_title": c.doc_title,
            "doc_journal": c.doc_journal,
            "doc_year": c.doc_year,
            "evidence_level": c.evidence_level,
        }
        for c in batch
    ]

    try:
        result = client.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": build_user_prompt(payload)},
            ],
            temperature=0.4,
            max_tokens=8192,
            response_format={"type": "json_object"},
        )
    except LLMError as exc:
        logger.warning("批次生成失败: %s", str(exc)[:160])
        return _retry_smaller(client, batch, dataset, consume, depth, system_prompt)

    items = parse_json_array(result.content)
    if not items:
        logger.warning("未解析出题目（批次大小 %d）", len(batch))
        return _retry_smaller(client, batch, dataset, consume, depth, system_prompt)

    return consume(batch, items)


def _retry_smaller(
    client: LLMClient,
    batch: list[CandidateChunk],
    dataset: EvalDataset,
    consume,
    depth: int,
    system_prompt: str = SYSTEM_PROMPT,
    max_depth: int = 2,
) -> int:
    """把失败的批次拆半重试；已到最小粒度（1 个片段）则放弃。"""
    if depth >= max_depth or len(batch) <= 1:
        logger.warning("放弃 %d 个片段（已到最小粒度）", len(batch))
        return 0

    mid = len(batch) // 2
    logger.info("  拆分为 %d + %d 重试", mid, len(batch) - mid)
    total = _generate_batch(
        client, batch[:mid], dataset, consume, depth + 1, system_prompt
    )
    total += _generate_batch(
        client, batch[mid:], dataset, consume, depth + 1, system_prompt
    )
    return total
