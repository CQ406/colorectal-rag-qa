"""提示词模板。

核心设计：**引用内联标注 + 流式输出**

问题：结构化 JSON 输出（含 citations 数组）虽然便于解析，但无法流式显示——
用户必须等完整 JSON 生成完才能看到答案，体验差。

方案：要求 LLM 在答案正文中内联标注 `[1][2]`，后端用正则解析这些标记，
映射回真实的 chunk_id。这样：
  - 答案可以逐字流式输出（前端先看到文字）
  - 引用关系可事后解析（标记与证据编号一一对应）
  - 用户能直观看到「这句话的依据是第几篇文献」

这是本系统「可溯源」的核心实现方式，也是论文中需要说明的设计取舍。

医学场景的提示词约束（重要）
--------------------------
1. **仅依据给定证据作答**，不依赖模型内部知识——否则无法溯源，且易产生幻觉
2. **证据不足时必须明确说明**，不得编造——医学场景的硬性要求
3. **标注证据等级**，让临床医师判断结论可信度
4. **强制合规声明**：输出仅供教学科研参考，不构成诊疗建议
"""

from __future__ import annotations

from app.core.constants import EvidenceLevel, PerioperativePhase

# ======================================================================
#  系统提示词
# ======================================================================
SYSTEM_PROMPT = """你是结直肠手术围术期专业知识助手，服务于临床医师的教学与科研需求。

## 回答规则（必须严格遵守）

1. **只依据提供的「证据片段」作答**。不要使用你自己的医学知识补充或推测。
   如果证据片段中没有相关信息，必须明确回答"提供的资料中未包含相关信息"。

2. **每个结论都要标注来源编号**。在陈述后面用方括号标注依据的证据编号，例如：
   - 吻合口漏的发生率约为 3%~15%[1]。
   - 处理原则包括充分引流与广谱抗生素治疗[1][3]。

3. **区分证据强度**。当证据来自临床指南或系统评价时更可信；
   来自病例报告时需谨慎表述。必要时在回答中提示证据等级。

4. **禁止编造**。不要虚构文献、数据、剂量或结论。
   不确定的内容必须说明"资料中未明确"。

5. **【最重要】数值与专有名称必须逐字来自证据片段。**
   实测发现最危险的幻觉并非整段编造，而是**编造看起来专业的精确细节**，
   例如把「中转率 7.1% vs 3.4%」「病理完全缓解率最高 30%」
   「NORAD / PROSPECT 试验」写成有据可依的样子。
   临床医师看到具体百分比会本能信任，因此这类错误危害最大。

   具体约束：
   - **任何数字、百分比、比值、样本量、P 值、置信区间**，
     必须能在证据片段中逐字找到，否则**不得写出**。
     无法确认时改用定性表述（如"有所降低""差异不明显"）。
   - **任何试验名称、研究缩写、评分量表、药物剂量、作者名**，
     同样必须逐字来自证据片段。
   - **不得把 A 文献的结论归因给 B 文献**。写 [n] 前请确认该编号的
     片段确实包含这句话的内容；若某结论来自片段 [3]，
     而 [3] 讲的是另一主题（如把 taTME 的缺陷归给一篇机器人 TME 综述），
     即为错误归因，必须删除该结论或改为"资料未包含"。
   - **不得外推**。片段未提及的适用人群、亚组、长期结局不得写入。

6. **结构清晰**。可用分点、小标题组织回答，便于临床快速阅读。
   涉及具体数值、药物、操作步骤时尤其要准确引用来源。

7. **语言**：使用中文回答（专业术语可保留英文原文）。

## 输出格式

直接输出回答正文，不要输出 JSON、不要输出"根据提供的资料"这类前言。
引用编号紧跟在对应结论后面，格式为 [数字]，多个引用连写如 [1][2]。"""


# ======================================================================
#  用户提示词：证据 + 问题
# ======================================================================
def build_evidence_block(evidences: list[dict]) -> str:
    """把检索结果格式化为证据片段块。

    Args:
        evidences: 每项含 index / content / doc_title / journal / year /
                   page_start / evidence_level / phase

    Returns:
        形如：
            [证据 1] 标题（期刊, 2023）第 850 页 | 证据等级：临床指南
            正文内容...
    """
    if not evidences:
        return "（未检索到相关证据）"

    lines: list[str] = []
    for ev in evidences:
        idx = ev.get("index", 0)
        title = ev.get("doc_title") or "未标题文献"
        # 证据字典的键是 doc_journal / doc_year（见 citation.evidences_from_search）。
        # 此前读 ev["journal"] / ev["year"]，两处永远是 None —— 提示词里写了
        # 「（期刊, 年份）」却一直是空串，模型拿不到时效性与来源强度线索。
        journal = ev.get("doc_journal") or ev.get("journal") or ""
        year = ev.get("doc_year") or ev.get("year") or ""
        page = ev.get("page_start")

        # 证据等级的中文名，便于 LLM 判断强度
        level_raw = ev.get("evidence_level", "other")
        try:
            level_zh = EvidenceLevel(level_raw).zh
        except ValueError:
            level_zh = "其他"

        phase_raw = ev.get("phase", "general")
        try:
            phase_zh = PerioperativePhase(phase_raw).zh
        except ValueError:
            phase_zh = "通用"

        meta_parts = [p for p in [journal, str(year) if year else ""] if p]
        meta = f"（{', '.join(meta_parts)}）" if meta_parts else ""
        page_str = f" 第{page}页" if page else ""

        lines.append(
            f"[证据 {idx}] {title}{meta}{page_str} | 证据等级：{level_zh} | 分期：{phase_zh}"
        )
        lines.append(ev.get("content", "").strip())
        lines.append("")   # 空行分隔

    return "\n".join(lines).strip()


def build_qa_prompt(question: str, evidences: list[dict], phase: str | None = None) -> str:
    """构造问答提示词。

    Args:
        question: 用户问题
        evidences: 检索到的证据片段
        phase: 限定的围术期分期（术前/术中/术后）
    """
    phase_hint = ""
    if phase:
        try:
            phase_hint = f"\n（本次提问聚焦于**{PerioperativePhase(phase).zh}**阶段）\n"
        except ValueError:
            phase_hint = ""

    scene_guidance = {
        "preop": "优先组织术前评估目标、适应证、检查/准备、风险因素和资料缺口。",
        "intraop": "优先组织术中解剖或问题、文献支持的处理原则、风险和局限；不要写成实时医嘱。",
        "postop": "优先组织术后并发症表现、评估与管理要点、康复和随访；药物剂量只在证据明确时引用。",
    }.get(phase or "", "")
    if scene_guidance:
        scene_guidance = f"\n## 场景组织要求\n{scene_guidance}\n"

    evidence_block = build_evidence_block(evidences)

    return f"""请依据以下证据片段回答临床问题。{phase_hint}{scene_guidance}
## 证据片段

{evidence_block}

## 问题

{question}

## 要求

请依据上述证据片段回答。每个结论后标注来源编号如 [1]。
若证据不足以回答，明确说明缺少哪方面的资料。"""


# ======================================================================
#  影像辅助解读（阶段六）
# ======================================================================
def build_image_synthesis_prompt(
    description: str,
    evidences: list[dict],
    image_context: str = "",
) -> str:
    """构造影像综合解读提示词。

    与文本问答的关键差异：输入含**两个可能不吻合**的信息源 ——
    视觉模型的描述与文献证据。提示词必须要求模型明确区分二者，
    并在二者不对应时如实指出，而不是强行缝合出一个看似合理的结论。

    Args:
        description: 视觉模型生成的客观描述
        evidences: 检索到的文献证据（由描述中的关键词检索而来）
        image_context: 用户填写的人工标注（如术式、解剖部位），可为空
    """
    evidence_block = build_evidence_block(evidences)

    context_block = ""
    if image_context.strip():
        context_block = f"""
## 用户提供的影像背景

{image_context.strip()}
（此为人工提供的信息，可辅助理解，但仍需以证据片段为知识依据）
"""

    return f"""请对以下术中影像做辅助解读。

## 影像的客观描述

{description}
{context_block}
## 检索到的文献证据

{evidence_block}

## 要求

1. 「影像所见」只转述上面的客观描述，不要添加描述中不存在的内容。
2. 「文献依据」必须来自上述证据片段，每条结论标注来源编号如 [1]。
3. **如果影像描述与检索到的证据并不对应**（例如描述的是腹腔镜视野，
   而证据讨论的是病理分期），必须明确指出二者不直接对应，
   不要强行建立关联。
4. 不要输出诊断结论。使用"提示""需结合""建议评估"等表述。
5. 若证据不足以支撑解读，明确说明缺少哪方面的资料。"""


# ======================================================================
#  意图识别与分期路由（阶段七使用，此处先提供）
# ======================================================================
INTENT_SYSTEM_PROMPT = """你是医学问答系统的意图分类器。请判断用户问题的类型。

## 分类标签

分期（phase）：
- preop   术前：诊断分期、术前评估、肠道准备、新辅助治疗、术前营养
- intraop 术中：术式选择、手术技术、解剖要点、术中并发症、灌注评估
- postop  术后：术后并发症、吻合口漏、康复、营养支持、随访
- general 通用：基础知识、流行病学、不特定于某一阶段

问题类型（intent）：
- knowledge  知识性提问（"OR" 是什么、"吻合口漏发生率多少"）
- advice     决策建议（"该怎么处理"、"推荐什么方案"）
- compare    比较类（"腹腔镜与开腹哪个好"）
- summary    总结类（"围术期管理要点有哪些"）

## 输出格式

只输出 JSON，不要任何其他文字：
{"phase": "postop", "intent": "advice", "confidence": 0.9}"""


def build_intent_prompt(question: str) -> str:
    """构造意图识别提示词。"""
    return f"请分类以下问题：\n\n{question}"


# ======================================================================
#  多轮对话：查询改写（阶段七使用）
# ======================================================================
REWRITE_SYSTEM_PROMPT = """你是检索查询改写助手。用户在多轮对话中会使用指代和省略，
需要你结合对话历史把最新问题改写为**独立完整**的检索查询。

## 要求

1. 解析指代：把"它""这个并发症""该术式"等替换为具体实体
2. 补全省略：结合上文补全缺失的主语或限定条件
3. 保留专业术语原样，不要改写医学名词
4. 只输出改写后的查询，不要任何解释、不要引号

## 示例

历史：用户问"结直肠癌术后吻合口漏如何处理"
最新：那它的发生率是多少？
输出：结直肠癌术后吻合口漏的发生率是多少

历史：用户问"ERAS 的核心措施"
最新：术后多久可以进食？
输出：ERAS 方案中术后早期进食的时间要求"""


def build_rewrite_prompt(question: str, history: list[dict[str, str]]) -> str:
    """构造查询改写提示词。

    Args:
        question: 用户最新提问
        history: 对话历史，每项含 role / content
    """
    if not history:
        return question

    recent = history[-6:]   # 只取最近 3 轮，控制 token 消耗
    lines = []
    for msg in recent:
        role = "用户" if msg.get("role") == "user" else "助手"
        content = (msg.get("content") or "")[:300]   # 截断长回答
        lines.append(f"{role}：{content}")

    history_text = "\n".join(lines)

    return f"""## 对话历史

{history_text}

## 最新提问

{question}

请输出改写后的独立检索查询（只输出查询本身）："""


# ======================================================================
#  合规声明
# ======================================================================
DISCLAIMER = (
    "本回答由 AI 依据知识库文献自动生成，仅供教学与科研参考，"
    "不构成任何诊疗建议。临床决策请以最新指南与患者具体情况为准。"
)
