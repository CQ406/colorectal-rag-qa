"""生成演示用问答日志：用真实问题跑通真实问答链路。

目的
----
`sys_qa_log` 只有 20 条左右，质量监控页面几乎是空的，答辩演示时看不出效果。
本脚本按围术期主题批量提问，每条都会真实调用检索 + 生成并落库 ——
不是伪造数据，是真的把系统用一遍。

题目覆盖三类：
  1. 知识型问题（应有证据、有引用）
  2. 决策/比较型问题（考察多证据综合）
  3. 库外问题（应触发「证据不足」拒答，用于展示防线）

用法：
    python -m scripts.generate_demo_qa --limit 40
    python -m scripts.generate_demo_qa --only nosology   # 只跑某类
"""
from __future__ import annotations

import argparse
import json
import time
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8000/api"

# 围术期主题问题（问题本身由我们提出，答案全部来自知识库检索）
QUESTIONS: dict[str, list[str]] = {
    "preop": [
        "直肠癌术前分期需要做哪些检查？",
        "低位直肠癌保肛手术的适应证是什么？",
        "直肠癌新辅助放化疗的适应证有哪些？",
        "结直肠癌术前肠道准备需要做哪些？",
        "老年患者术前营养风险评估怎么做？",
        "术前是否需要常规停用抗血小板药物？",
        "结直肠癌患者术前需要评估哪些合并症？",
        "新辅助治疗后完全缓解可以观察等待吗？",
        "直肠癌MRI在术前评估中的作用是什么？",
        "术前预康复包括哪些内容？",
    ],
    "intraop": [
        "术中如何评估肠道吻合口的血供？",
        "吲哚菁绿荧光显像如何判断吻合口灌注？",
        "腹腔镜直肠癌根治术的淋巴结清扫范围是什么？",
        "全直肠系膜切除术的关键技术要点有哪些？",
        "术中如何保护盆腔自主神经？",
        "直肠癌侧方淋巴结清扫的适应证是什么？",
        "腹腔镜手术中转开腹的指征有哪些？",
        "术中低体温如何预防？",
        "吻合口张力过大时如何处理？",
        "术中出血如何处理？",
    ],
    "postop": [
        "吻合口漏的临床表现有哪些？",
        "吻合口漏如何预防？",
        "加速康复外科的核心措施有哪些？",
        "术后早期进食是否安全？",
        "术后多久可以下床活动？",
        "术后疼痛如何管理？",
        "术后肠梗阻怎么处理？",
        "直肠癌术后排尿功能障碍如何预防？",
        "术后吻合口出血如何处理？",
        "造口患者术后如何护理？",
        "术后多久开始辅助化疗？",
        "术后随访需要做哪些检查？",
    ],
    "compare": [
        "腹腔镜与开腹手术在短期结局上有何差异？",
        "机器人手术相比腹腔镜有哪些优势？",
        "机械性肠道准备是否必要？",
        "吻合口漏的保守治疗与手术治疗如何选择？",
        "预防性造口能否降低吻合口漏风险？",
        "经肛全直肠系膜切除与传统腹腔镜有何区别？",
        "早期直肠癌局部切除与根治性切除如何选择？",
        "加速康复外科与传统围术期管理有何差异？",
    ],
    "nosology": [
        # 库外问题：用于展示「证据不足即拒答」
        "胃癌D2根治术的清扫范围是什么？",
        "急性胰腺炎的液体复苏策略是什么？",
        "甲状腺结节的手术指征是什么？",
        "胆囊结石的腹腔镜手术时机如何选择？",
        "肝移植术后免疫抑制剂如何调整？",
    ],
}


def ask(question: str, timeout: int = 300) -> dict:
    req = Request(
        BASE + "/qa",
        data=json.dumps({"question": question, "top_k": 5}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑某一类（preop/intraop/postop/compare/nosology）")
    ap.add_argument("--limit", type=int, default=0, help="最多提问数（0 表示全部）")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    groups = [args.only] if args.only else list(QUESTIONS)
    tasks: list[tuple[str, str]] = [(g, q) for g in groups for q in QUESTIONS[g]]
    if args.limit:
        tasks = tasks[: args.limit]

    ok = no_evidence = failed = 0
    t0 = time.perf_counter()
    for i, (group, q) in enumerate(tasks, 1):
        try:
            d = ask(q)
            cites = len(d.get("citations") or [])
            ans = len(d.get("answer") or "")
            inval = d.get("invalid_citations") or []
            if cites == 0:
                no_evidence += 1
                tag = "无证据"
            else:
                ok += 1
                tag = "有引用"
            print(f"  [{i:>2}/{len(tasks)}] {tag} 引用{cites} 答案{ans}字 无效引用{len(inval)} | {q[:34]}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [{i:>2}/{len(tasks)}] 失败: {str(exc)[:70]} | {q[:30]}", flush=True)
        time.sleep(args.sleep)

    elapsed = time.perf_counter() - t0
    print()
    print(f"完成：有引用 {ok} | 无证据 {no_evidence} | 失败 {failed} | 耗时 {elapsed/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
