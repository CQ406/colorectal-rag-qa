"""影像辅助解读服务。

三段式链路（报审表「（三）4 影像辅助解读模块」）
------------------------------------------------
用户上传术中影像
   ↓ ① VLM 客观描述（只描述看到的，不做诊断）
   ↓ ② 从描述中提取检索查询 → 检索知识库获取证据
   ↓ ③ LLM 综合：把影像所见与文献证据对照，给出来源可查的辅助解读

**为什么必须分三段而不是直接让 VLM 出结论**
  云端视觉模型对手术影像的诊断能力不可靠，且其输出**无法溯源**。
  若直接展示 VLM 的"诊断意见"，就违背了本系统「可溯源」的核心设计，
  也带来医学合规风险。

  因此本模块的定位是**辅助解读**而非诊断：
    - VLM 只负责客观描述（解剖部位、可见结构、器械、染色范围等）
    - 结论部分必须由**检索到的文献证据**支撑，并带引用编号
    - 输出强制附带合规声明

  这与文本问答的抗幻觉设计一致：先取证，再作答。

**关于影像相似度检索**
  影像上传时会生成 512 维 RGB 颜色直方图基线特征，
  通过 pgvector 提供相似样例检索；后续可替换为 CLIP 类医学视觉编码器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.services import citation as citation_mod
from app.services import hybrid_search, prompts
from app.services.vision_client import VisionClient, VisionError

logger = get_logger("services.image_analysis")


# ======================================================================
#  ① VLM 客观描述提示词
# ======================================================================
VLM_DESCRIBE_PROMPT = """你是手术影像描述助手。请**客观描述**这张结直肠手术影像，不要做诊断结论。

请按以下结构输出（用中文，纯文本，不要 Markdown 表格）：

【影像类型】判断属于哪类影像（术中照片 / 腹腔镜视野 / 内镜 / 影像学检查 / 大体标本 / 其他）

【解剖部位】可见的解剖结构或手术区域

【可见所见】客观描述能看到的组织、颜色、形态、血管、系膜等特征

【手术器械与材料】可见的器械、缝线、补片、吻合器、能量设备等

【特殊染色或标记】如荧光显影（ICG）、亚甲蓝、钛夹等；如有荧光请描述显影范围与强度

【图像质量】清晰度、视野完整性、有无遮挡或反光

严格约束：
- 只描述**确实看到**的内容。看不清的内容写"无法判读"，不要猜测。
- 不要给出疾病诊断、不要判断良恶性。
- 不要编造影像中不存在的结构。"""


# ======================================================================
#  ③ LLM 综合解读提示词
# ======================================================================
SYNTHESIS_SYSTEM_PROMPT = """你是结直肠手术围术期专业助手，正在为临床医师提供**影像辅助解读**。

你会收到两部分输入：
  A. 对一张术中影像的**客观描述**（由视觉模型生成，可能有误）
  B. 从医学文献知识库检索到的**证据片段**

你的任务：结合影像描述与文献证据，给出辅助解读。

## 严格规则

1. **文献证据是唯一的知识来源**。不要使用你自己的医学知识补充。
   证据中没有的内容必须写"提供的资料中未包含相关信息"。

2. **必须区分事实与推测**：
   - 「影像所见」部分只能转述描述 A 中确实存在的内容
   - 「文献依据」部分必须来自证据 B，并标注 [n] 引用编号
   - 描述 A 与证据 B 不一致时，明确指出"影像描述与文献资料未直接对应"

3. **不做诊断**。可以说明「该征象在文献中的意义」，
   但不得输出「诊断为 XX」这类结论。用"提示""需结合""建议评估"等表述。

4. **禁止编造数值**。任何数字必须能在证据 B 中逐字找到。

5. **必须指出局限性**：视觉模型描述可能存在误判，最终判断需由医师结合
   患者完整资料完成。

## 输出结构

### 一、影像所见
（转述描述 A 的客观内容，分点）

### 二、文献依据
（对照影像征象与证据 B，每条结论标注 [n]）

### 三、辅助解读
（综合说明，明确标注哪些是推测）

### 四、局限性说明
（影像描述可能的误判、证据覆盖不足之处）"""


@dataclass
class ImageAnalysis:
    """一次影像解读的完整结果。"""

    description: str                      # VLM 客观描述
    interpretation: str = ""              # 综合解读（含引用标记）
    citations: list = field(default_factory=list)
    invalid_citations: list[int] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    numeric_warning: str = ""

    # 检索过程信息（便于论文中分析）
    retrieval_query: str = ""
    evidence_count: int = 0
    evidences: list[dict] = field(default_factory=list)
    related_cases: list[dict] = field(default_factory=list)

    # 元信息
    vlm_model: str = ""
    llm_model: str = ""
    timings: dict = field(default_factory=dict)
    vision_configured: bool = True
    disclaimer: str = prompts.DISCLAIMER
    # 综合解读失败时的原因（None 表示成功）。
    #
    # 为什么需要：解读失败时 `interpretation` 里写的是一段"生成失败"的提示文案，
    # 调用方如果照常缓存，会把这段失败文案当成结果落库，
    # 甚至覆盖掉此前成功解读的快照。加上这个标记后，调用方据此决定是否落库。
    error: str | None = None


def describe_image(image: bytes, mime: str = "image/jpeg") -> tuple[str, str, float]:
    """① 调用视觉模型做客观描述。

    Returns:
        (描述文本, 使用的模型, 耗时毫秒)
    """
    with VisionClient() as client:
        result = client.analyze(image, VLM_DESCRIBE_PROMPT, mime=mime)
    return result.content, result.model, result.elapsed_ms


# ======================================================================
#  ② 从影像描述中构造检索查询
# ======================================================================
# 检索查询不应直接用整段描述：描述含大量泛化表述与**推测性措辞**
# （"可能是…""或…""无法判读"），会稀释检索信号的语义聚焦度。
_SECTION_PATTERN = re.compile(r"【([^】]+)】([^【]*)")
# 参与构造查询的字段（按信息量排序）
_QUERY_SECTIONS = ("解剖部位", "可见所见", "特殊染色或标记", "手术器械与材料")

# 需剔除的泛化/推测表述。
#
# 为什么必须剔除推测词：VLM 的描述天然带不确定性（"可能是结肠段"）。
# 若把"可能是"这类词带入检索查询，会与文献中确定的陈述语句
# （"colon cancer""anastomotic leak"）语义不匹配，降低检索精度。
# 只保留**解剖结构与操作对象**这类实体性词汇。
_NOISE_PATTERNS = [
    # 推测与不确定表述
    r"可能是", r"可能为", r"可能有", r"疑似", r"考虑为", r"或为", r"或是",
    r"无法判读", r"无法确定", r"不清楚", r"难以判断",
    # 图像质量相关（与医学内容无关）
    r"清晰度[^，。；]*", r"略显模糊", r"视野较为完整", r"无明显的遮挡或反光",
    r"图像质量[^，。；]*",
    # 通用程度描述
    r"良好的", r"一般的", r"尚可",
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS))

# 提取实体性词汇：连续的中文/英文/数字片段
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9\-]{2,}")


def build_retrieval_query(description: str, max_len: int = 200) -> str:
    """从影像描述中提取关键实体作为检索查询。

    策略（三步）：
      1. 按结构化字段切分，只取有医学信息的字段
         （跳过「影像类型」「图像质量」这类元信息）
      2. **剔除推测性措辞与图像质量描述**，只保留实体性词汇
      3. 去重后拼接为长度受限的查询串

    为什么不用整段描述检索：实测整段描述会把"可能是""清晰度一般"
    这类词带入查询，与文献中的确定性陈述语句语义不匹配，
    降低检索聚焦度。构造后查询词应为纯实体序列。
    """
    if not description:
        return "结直肠手术 术中影像"

    sections = {name.strip(): body.strip()
                for name, body in _SECTION_PATTERN.findall(description)}

    tokens: list[str] = []
    seen: set[str] = set()
    for key in _QUERY_SECTIONS:
        body = sections.get(key)
        if not body:
            continue
        # 先剔除推测与质量描述，再抽实体词
        cleaned = _NOISE_RE.sub(" ", body)
        for tok in _TOKEN_RE.findall(cleaned):
            tok = tok.strip()
            if len(tok) < 2 or tok in seen:
                continue
            seen.add(tok)
            tokens.append(tok)
            if sum(len(t) for t in tokens) >= max_len:
                break

    query = " ".join(tokens)[:max_len].strip()
    if not query:
        # 结构化字段解析失败时退回整段描述（截断），保证仍有查询可用
        query = re.sub(r"\s+", " ", description)[:max_len]
    return query


def synthesize(
    db: Session,
    description: str,
    *,
    top_k: int = 5,
    phase: str | None = None,
    image_context: str = "",
) -> ImageAnalysis:
    """③ 检索证据并综合解读。"""
    import time

    from app.services.llm_client import LLMClient, LLMError

    t_start = time.perf_counter()
    timings: dict[str, float] = {}

    analysis = ImageAnalysis(description=description)

    # ---------- 检索 ----------
    query = build_retrieval_query(description)
    analysis.retrieval_query = query
    # 同一影像描述同时作为病例库检索输入，推荐公开脱敏病例供医师对照。
    try:
        from app.repositories import case_repo

        analysis.related_cases = [
            {
                "id": str(case.id),
                "case_code": case.case_code,
                "title": case.title,
                "diagnosis": case.diagnosis,
                "surgery": case.surgery,
                "phase": case.phase,
                "tags": case.tags or [],
                "similarity": round(score, 6),
                "is_public": case.is_public,
            }
            for case, score in case_repo.related_cases(db, query, top_k=3, phase=phase)
        ]
    except Exception as exc:  # noqa: BLE001 - 病例库为空不应阻断影像解读
        logger.warning("相关病例推荐失败: %s", str(exc)[:160])
    t0 = time.perf_counter()
    from app.schemas.common import SearchRequest

    search_resp = hybrid_search.search(
        db,
        SearchRequest(
            query=query,
            top_k=top_k,
            recall_mode="hybrid",
            use_rerank=True,
            use_evidence_boost=True,
            phase=phase,
        ),
    )
    timings["retrieval_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    evidences = citation_mod.evidences_from_search(search_resp.results)
    analysis.evidences = evidences
    analysis.evidence_count = len(evidences)

    if not evidences:
        analysis.interpretation = (
            "### 一、影像所见\n"
            f"{description}\n\n"
            "### 二、文献依据\n"
            "知识库中未检索到与该影像相关的文献证据。\n\n"
            "### 三、辅助解读\n"
            "因缺乏可依据的文献资料，无法给出辅助解读。\n"
            "建议由临床医师结合患者完整资料与最新指南自行判断。\n\n"
            "### 四、局限性说明\n"
            "本次检索未命中相关证据，不代表该影像无临床意义。"
        )
        timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
        analysis.timings = timings
        logger.warning("影像解读：未检索到证据（查询=%s）", query[:80])
        return analysis

    # ---------- 综合解读 ----------
    user_prompt = prompts.build_image_synthesis_prompt(
        description=description,
        evidences=evidences,
        image_context=image_context,
    )

    client = LLMClient()
    try:
        t0 = time.perf_counter()
        try:
            chat = client.chat(
                [
                    {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
            )
        except LLMError as exc:
            logger.error("影像综合解读失败: %s", exc)
            analysis.interpretation = (
                "### 一、影像所见\n"
                f"{description}\n\n"
                "### 二、文献依据\n"
                f"已检索到 {len(evidences)} 条相关证据，但综合解读生成失败：{exc}\n\n"
                "### 三、辅助解读\n请稍后重试，或直接查看右侧引用文献。"
            )
            # 标记失败原因：调用方据此跳过缓存与日志，
            # 避免把这段"生成失败"的提示当成正式解读落库、覆盖旧的成功快照。
            analysis.error = f"{type(exc).__name__}: {exc}"[:300]
            timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
            analysis.timings = timings
            return analysis
        timings["synthesis_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        analysis.llm_model = chat.model
    finally:
        client.close()

    # ---------- 引用解析与数值核验 ----------
    parsed = citation_mod.build_citations(chat.content, evidences)
    analysis.interpretation = chat.content
    analysis.citations = parsed.citations
    analysis.invalid_citations = parsed.invalid_indices
    if parsed.invalid_indices:
        logger.warning("影像解读引用了不存在的编号: %s", parsed.invalid_indices)

    _, unsupported = citation_mod.verify_numeric_claims(
        chat.content, [e["content"] for e in evidences]
    )
    analysis.unsupported_numbers = [c.raw.strip() for c in unsupported]
    analysis.numeric_warning = citation_mod.unsupported_numeric_summary(unsupported)

    timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
    analysis.timings = timings

    logger.info(
        "影像解读完成: 证据 %d 条, 引用 %d 条, 耗时 %.1fs",
        len(evidences), len(parsed.citations), timings["total_ms"] / 1000,
    )
    return analysis
