"""对比 deepseek-flash 与 deepseek-v4-pro 的延迟特性。

用途：为「两段式生成」（flash 草稿 + v4-pro 精修）方案提供选型依据。

关键问题：两段式要成立，第一段必须真的快。
若 flash 也是「先长时间思考再输出」，则两段式总耗时 = 两段之和，
反而不如单用 v4-pro。

测量项：
  - 首字延迟（TTFT）：从请求到第一个 content 字符
  - 首思考延迟：从请求到第一个 reasoning 字符（推理模型专有）
  - 总耗时、token 用量、reasoning 占 completion 的比例

用法:
    python -m scripts.compare_models
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.config import settings
from app.core.logging import get_logger, setup_logging

logger = get_logger("scripts.compare_models")

# 模拟真实问答场景：系统提示 + 证据 + 问题
SYSTEM = """你是结直肠手术围术期专业知识助手。
1. 只依据提供的证据片段作答，不要使用你自己的医学知识补充。
2. 每个结论标注来源编号如 [1]。
3. 禁止编造文献、数据、剂量或结论。"""

EVIDENCE = """[证据 1] 中国结直肠癌诊疗规范(2023版)（中华消化外科杂志, 2023） 第850页 | 证据等级：临床指南
结直肠癌术后吻合口漏是结直肠手术最常见且最严重的并发症之一，发生率约为3%~15%。
吻合口漏的处理原则包括：充分引流、禁食水、胃肠减压、广谱抗生素治疗，必要时行近端肠造口转流手术。

[证据 2] 加速康复外科在结直肠手术中的应用进展（中国实用外科杂志, 2024） 第146页 | 证据等级：综述
加速康复外科（ERAS）在结直肠手术中的应用可显著缩短住院时间。
推荐术后24小时内开始经口进食，可降低吻合口漏与肺部感染风险。

[证据 3] Anastomotic leakage in colorectal cancer surgery（Surgical Oncology, 2022） 第1页 | 证据等级：系统评价
Management of anastomotic leak depends on the patient's general condition, the size and
location of the defect, and the presence of a proximal stoma. CT and endoscopy are the
most important diagnostic tools. CRP and procalcitonin can serve as early predictors."""

QUESTION = "结直肠癌术后吻合口漏的发生率和处理原则是什么？"


def measure_stream(model: str, max_tokens: int) -> dict:
    """用流式请求测量时间特性。"""
    url = settings.llm.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"## 证据片段\n\n{EVIDENCE}\n\n## 问题\n\n{QUESTION}"},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": True,
    }

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    t0 = time.perf_counter()
    ttft_content: float | None = None
    ttft_reasoning: float | None = None
    usage: dict = {}
    finish_reason = ""

    with httpx.Client(timeout=300.0, trust_env=False) as client:
        with client.stream(
            "POST", url, json=payload,
            headers={"Authorization": f"Bearer {settings.llm.llm_api_key}"},
        ) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "ignore")
                return {"error": f"HTTP {resp.status_code}: {body[:200]}"}

            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]

                delta = choices[0].get("delta", {})
                if delta.get("reasoning_content"):
                    if ttft_reasoning is None:
                        ttft_reasoning = time.perf_counter() - t0
                    reasoning_parts.append(delta["reasoning_content"])
                if delta.get("content"):
                    if ttft_content is None:
                        ttft_content = time.perf_counter() - t0
                    content_parts.append(delta["content"])

    total = time.perf_counter() - t0
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)

    return {
        "model": model,
        "max_tokens": max_tokens,
        "total_s": round(total, 2),
        "ttft_content_s": round(ttft_content, 2) if ttft_content else None,
        "ttft_reasoning_s": round(ttft_reasoning, 2) if ttft_reasoning else None,
        "content_chars": len(content),
        "reasoning_chars": len(reasoning),
        "finish_reason": finish_reason,
        "usage": usage,
        "content_preview": content[:200],
        "empty": not content.strip(),
    }


def main() -> int:
    setup_logging()
    logger.info("=" * 88)
    logger.info("模型延迟特性对比（两段式生成方案选型）")
    logger.info("=" * 88)

    configs = [
        ("deepseek-flash", 512),
        ("deepseek-flash", 2048),
        ("deepseek-flash", 4096),
        ("deepseek-v4-pro", 4096),
    ]

    results: list[dict] = []
    for model, max_tokens in configs:
        logger.info("\n--- %s (max_tokens=%d) ---", model, max_tokens)
        r = measure_stream(model, max_tokens)
        results.append(r)
        if "error" in r:
            logger.error("  失败: %s", r["error"])
            continue

        logger.info(
            "  首字延迟: %s | 首思考延迟: %s | 总耗时: %ss",
            f"{r['ttft_content_s']}s" if r["ttft_content_s"] else "无输出",
            f"{r['ttft_reasoning_s']}s" if r["ttft_reasoning_s"] else "—",
            r["total_s"],
        )
        logger.info(
            "  内容 %d 字符 | 推理 %d 字符 | finish=%s | %s",
            r["content_chars"], r["reasoning_chars"], r["finish_reason"],
            "❌ 空答案" if r["empty"] else "✓",
        )
        u = r.get("usage") or {}
        if u:
            logger.info(
                "  token: prompt=%s completion=%s",
                u.get("prompt_tokens"), u.get("completion_tokens"),
            )
        if r["content_preview"]:
            logger.info("  内容预览: %s", r["content_preview"][:150].replace("\n", " "))

    # ---------- 汇总 ----------
    logger.info("\n" + "=" * 88)
    logger.info("汇总")
    logger.info("=" * 88)
    logger.info("%-20s %-11s %-11s %-9s %-8s %s", "模型", "max_tokens", "首字延迟", "总耗时", "推理占比", "空答案")
    logger.info("-" * 88)
    for r in results:
        if "error" in r:
            logger.info("%-20s %-11s 失败", r.get("model", "?"), r.get("max_tokens", "?"))
            continue
        u = r.get("usage") or {}
        comp = u.get("completion_tokens") or 0
        # 粗略估计推理 token 占比（按字符数比例）
        total_chars = r["content_chars"] + r["reasoning_chars"]
        ratio = (r["reasoning_chars"] / total_chars * 100) if total_chars else 0
        logger.info(
            "%-20s %-11d %-11s %-9s %-8s %s",
            r["model"], r["max_tokens"],
            f"{r['ttft_content_s']}s" if r["ttft_content_s"] else "无输出",
            f"{r['total_s']}s",
            f"{ratio:.0f}%",
            "是" if r["empty"] else "否",
        )

    logger.info("\n两段式可行性判据：")
    logger.info("  若 flash 的首字延迟明显小于 v4-pro 的总耗时，则两段式可改善体感；")
    logger.info("  若 flash 也需长时间思考（首字延迟接近 v4-pro），两段式只会叠加延迟。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
