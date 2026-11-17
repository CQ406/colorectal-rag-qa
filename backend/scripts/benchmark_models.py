"""模型延迟特性的稳健测量（多次采样）。

背景：单次测量结果自相矛盾 —— flash(max_tokens=4096) 比 flash(2048) 快 3 倍，
而 v4-pro 在此测得 4.5s、在阶段四实测 15~32s。说明 API 侧存在显著波动，
必须多次采样取中位数与分位数，否则架构决策建立在噪声上。

测量设计：
  - 每个配置采样 N 次（默认 3）
  - 报告 最小值 / 中位数 / 最大值，而非单点
  - 同一轮内交替请求不同配置，避免「先跑的配置遇到冷启动」的系统偏差

用法:
    python -m scripts.benchmark_models            # 每配置 3 次
    python -m scripts.benchmark_models --runs 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.config import settings
from app.core.logging import get_logger, setup_logging

logger = get_logger("scripts.benchmark_models")

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


def one_run(model: str, max_tokens: int) -> dict:
    """单次流式请求，测量时间特性。"""
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
    usage: dict = {}
    finish_reason = ""

    try:
        with httpx.Client(timeout=300.0, trust_env=False) as client:
            with client.stream(
                "POST", url, json=payload,
                headers={"Authorization": f"Bearer {settings.llm.llm_api_key}"},
            ) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "ignore")
                    return {"error": f"HTTP {resp.status_code}: {body[:150]}"}

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
                        reasoning_parts.append(delta["reasoning_content"])
                    if delta.get("content"):
                        if ttft_content is None:
                            ttft_content = time.perf_counter() - t0
                        content_parts.append(delta["content"])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    total = time.perf_counter() - t0
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    return {
        "total_s": total,
        "ttft_s": ttft_content,
        "content_chars": len(content),
        "reasoning_chars": len(reasoning),
        "finish_reason": finish_reason,
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "empty": not content.strip(),
    }


def summarize(label: str, runs: list[dict]) -> dict:
    """汇总多次采样。"""
    valid = [r for r in runs if "error" not in r]
    errors = [r for r in runs if "error" in r]
    if not valid:
        return {"label": label, "error": errors[0]["error"] if errors else "全部失败"}

    totals = [r["total_s"] for r in valid]
    ttfts = [r["ttft_s"] for r in valid if r["ttft_s"] is not None]
    empties = sum(1 for r in valid if r["empty"])
    comps = [r["completion_tokens"] for r in valid if r["completion_tokens"]]
    reason_ratio = statistics.mean(
        r["reasoning_chars"] / (r["content_chars"] + r["reasoning_chars"])
        for r in valid
        if (r["content_chars"] + r["reasoning_chars"]) > 0
    ) if valid else 0

    return {
        "label": label,
        "n": len(valid),
        "total_min": round(min(totals), 2),
        "total_med": round(statistics.median(totals), 2),
        "total_max": round(max(totals), 2),
        "ttft_min": round(min(ttfts), 2) if ttfts else None,
        "ttft_med": round(statistics.median(ttfts), 2) if ttfts else None,
        "ttft_max": round(max(ttfts), 2) if ttfts else None,
        "empty_count": empties,
        "reasoning_ratio": round(reason_ratio * 100),
        "completion_med": int(statistics.median(comps)) if comps else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="模型延迟稳健测量")
    parser.add_argument("--runs", type=int, default=3, help="每个配置采样次数")
    args = parser.parse_args()

    setup_logging()
    logger.info("=" * 96)
    logger.info("模型延迟稳健测量（每配置 %d 次采样）", args.runs)
    logger.info("=" * 96)

    configs = [
        ("deepseek-flash", 1024),
        ("deepseek-flash", 2048),
        ("deepseek-flash", 4096),
        ("deepseek-v4-pro", 2048),
        ("deepseek-v4-pro", 4096),
    ]

    # 交替采样，避免「先跑的配置遇到冷启动」造成系统偏差
    samples: dict[str, list[dict]] = {f"{m}@{t}": [] for m, t in configs}
    for round_idx in range(args.runs):
        logger.info("\n--- 第 %d 轮 ---", round_idx + 1)
        for model, max_tokens in configs:
            key = f"{model}@{max_tokens}"
            r = one_run(model, max_tokens)
            samples[key].append(r)
            if "error" in r:
                logger.info("  %-26s 失败: %s", key, r["error"][:60])
            else:
                logger.info(
                    "  %-26s 总耗时 %5.2fs  首字 %-7s 内容 %4d 字  推理占比 %3.0f%%  %s",
                    key, r["total_s"],
                    f"{r['ttft_s']:.2f}s" if r["ttft_s"] else "无输出",
                    r["content_chars"],
                    (r["reasoning_chars"] / max(1, r["content_chars"] + r["reasoning_chars"])) * 100,
                    "❌空答案" if r["empty"] else "✓",
                )

    # ---------- 汇总 ----------
    logger.info("\n" + "=" * 96)
    logger.info("汇总（多次采样的分布，非单点值）")
    logger.info("=" * 96)
    logger.info(
        "%-26s %-7s %-22s %-22s %-8s %s",
        "配置", "成功", "总耗时 min/med/max", "首字 min/med/max", "推理占比", "空答案",
    )
    logger.info("-" * 96)

    summaries = []
    for model, max_tokens in configs:
        key = f"{model}@{max_tokens}"
        s = summarize(key, samples[key])
        summaries.append(s)
        if "error" in s:
            logger.info("%-26s 失败: %s", key, s["error"][:50])
            continue
        ttft = (
            f"{s['ttft_min']}/{s['ttft_med']}/{s['ttft_max']}"
            if s["ttft_med"] is not None else "无输出"
        )
        logger.info(
            "%-26s %-7s %-22s %-22s %-8s %s",
            key, f"{s['n']}/{args.runs}",
            f"{s['total_min']}/{s['total_med']}/{s['total_max']}",
            ttft,
            f"{s['reasoning_ratio']}%",
            s["empty_count"],
        )

    # ---------- 两段式可行性结论 ----------
    logger.info("\n" + "=" * 96)
    logger.info("两段式方案可行性分析")
    logger.info("=" * 96)

    flash = next((s for s in summaries if s.get("label", "").startswith("deepseek-flash@4096")), None)
    pro = next((s for s in summaries if s.get("label", "").startswith("deepseek-v4-pro@4096")), None)

    if flash and pro and "error" not in flash and "error" not in pro:
        flash_total = flash["total_med"]
        pro_total = pro["total_med"]
        two_stage = flash_total + pro_total

        logger.info("  flash@4096 中位总耗时      : %.2fs", flash_total)
        logger.info("  v4-pro@4096 中位总耗时     : %.2fs  ← 单段方案", pro_total)
        logger.info("  两段式预估总耗时           : %.2fs  ← flash + v4-pro", two_stage)
        logger.info("")
        logger.info("  单段 v4-pro 是否已足够快   : %s", "是" if pro_total <= 8 else "否")
        logger.info("  两段式是否比单段更慢       : %s", "是（慢 %.1fs）" % (two_stage - pro_total))
        logger.info("")
        if two_stage > pro_total * 1.5:
            logger.info("  结论：两段式总耗时显著高于单段 v4-pro，")
            logger.info("        若 v4-pro 单独已能达到可接受的首字延迟，则两段式没有必要。")
        else:
            logger.info("  结论：两段式总耗时与单段接近，且首段可更快给出草稿，值得采用。")
    else:
        logger.info("  数据不足，无法给出结论")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
