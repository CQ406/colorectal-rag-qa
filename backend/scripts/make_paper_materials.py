"""论文材料生成器：把评测结果转成表格与图表。

输出内容
--------
1. **Markdown 表格**：可直接粘贴进毕业论文的实验章节
2. **CSV 表格**：便于在 Word / Excel 中二次排版
3. **PNG 图表**：检索消融对比、生成质量雷达图（需 matplotlib）

设计说明
--------
表格与图表均由评测 JSON 驱动，评测脚本重跑后本脚本可一键刷新全部材料，
避免「手抄数据到论文」导致的版本不一致 —— 这是实验章节最常见的错误来源。

用法:
    python -m scripts.make_paper_materials
    python -m scripts.make_paper_materials --eval-dir data/eval --out-dir data/paper
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import get_logger, setup_logging

logger = get_logger("scripts.paper_materials")


# ======================================================================
#  表格生成
# ======================================================================
def retrieval_table(data: dict) -> str:
    """检索消融实验表（Markdown）。"""
    runs = data.get("runs", [])
    if not runs:
        return ""

    lines = [
        "| 配置 | R@1 | R@3 | R@5 | R@10 | MRR | NDCG@5 | 延迟(ms) |",
        "|------|-----|-----|-----|------|-----|--------|----------|",
    ]
    for r in runs:
        lines.append(
            f"| {r['label']} | {r['recall@1']:.3f} | {r['recall@3']:.3f} | "
            f"{r['recall@5']:.3f} | {r['recall@10']:.3f} | {r['mrr']:.3f} | "
            f"{r['ndcg@5']:.3f} | {r['avg_latency_ms']:.0f} |"
        )

    # 相对基线的提升
    if len(runs) >= 2:
        base = runs[0]
        lines.append("")
        lines.append(f"**相对「{base['label']}」的提升（百分点）**")
        lines.append("")
        lines.append("| 配置 | ΔR@5 | ΔMRR | ΔNDCG@5 |")
        lines.append("|------|------|------|---------|")
        for r in runs[1:]:
            lines.append(
                f"| {r['label']} | "
                f"{(r['recall@5'] - base['recall@5']) * 100:+.1f} | "
                f"{(r['mrr'] - base['mrr']) * 100:+.1f} | "
                f"{(r['ndcg@5'] - base['ndcg@5']) * 100:+.1f} |"
            )
    return "\n".join(lines)


def generation_table(data: dict) -> str:
    """生成质量表（Markdown）。"""
    m = data.get("metrics")
    if not m:
        return ""
    lines = [
        "| 指标 | 数值 | 说明 |",
        "|------|------|------|",
        f"| 忠实度 | {m['faithfulness']:.3f} | 结论是否有证据支撑（医学场景最关键） |",
        f"| 相关性 | {m['relevance']:.3f} | 答案是否切题 |",
        f"| 引用准确率 | {m['citation_accuracy']:.3f} | 引用是否真实且支撑所述内容 |",
        f"| 幻觉率 | {m['hallucination_rate']:.3f} | 出现无效引用编号的样本比例 |",
        f"| 平均答案长度 | {m['avg_answer_chars']:.0f} 字符 | — |",
        f"| 平均引用数 | {m['avg_citations']:.2f} 条 | — |",
        f"| 平均生成延迟 | {m['avg_latency_ms']:.0f} ms | 含检索与 LLM 生成 |",
        "",
        f"*样本量 n={m['valid']}（评测集共 {data.get('sample', 0)} 题抽样）*",
    ]
    return "\n".join(lines)


def dataset_table(data: dict) -> str:
    """评测集构成表（Markdown）。"""
    items = data.get("items", [])
    if not items:
        return ""

    def dist(key: str) -> dict[str, int]:
        d: dict[str, int] = {}
        for it in items:
            k = it.get(key, "other")
            d[k] = d.get(k, 0) + 1
        return d

    phase_zh = {"preop": "术前", "intraop": "术中", "postop": "术后", "general": "通用"}
    type_zh = {
        "definition": "概念定义", "management": "处理治疗", "diagnosis": "诊断评估",
        "comparison": "比较选择", "evidence": "证据预后", "other": "其他",
    }

    total = len(items)
    lines = [
        f"评测集共 **{total}** 题，语料规模 {data.get('corpus_documents', 0)} 篇文献 / "
        f"{data.get('corpus_chunks', 0)} 个文本块。",
        "",
        "**围术期分期分布**",
        "",
        "| 分期 | 题数 | 占比 |",
        "|------|------|------|",
    ]
    for k, v in sorted(dist("phase").items(), key=lambda kv: -kv[1]):
        lines.append(f"| {phase_zh.get(k, k)} | {v} | {v / total * 100:.1f}% |")

    lines += ["", "**问题类型分布**", "", "| 类型 | 题数 | 占比 |", "|------|------|------|"]
    for k, v in sorted(dist("question_type").items(), key=lambda kv: -kv[1]):
        lines.append(f"| {type_zh.get(k, k)} | {v} | {v / total * 100:.1f}% |")

    lines += [
        "",
        "**答案所在文献的证据等级分布**",
        "",
        "| 证据等级 | 题数 | 占比 |",
        "|----------|------|------|",
    ]
    for k, v in sorted(dist("gold_evidence_level").items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} | {v / total * 100:.1f}% |")

    return "\n".join(lines)


# ======================================================================
#  CSV 输出
# ======================================================================
def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info("  已写入 %s", path)


# ======================================================================
#  图表输出（需 matplotlib）
# ======================================================================
def try_make_charts(eval_dir: Path, out_dir: Path) -> bool:
    """生成图表；matplotlib 不可用时返回 False。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("未安装 matplotlib，跳过图表生成（表格仍可用）")
        return False

    # 中文字体（Windows 自带）
    for font in ("Microsoft YaHei", "SimHei", "DejaVu Sans"):
        try:
            matplotlib.rcParams["font.sans-serif"] = [font]
            matplotlib.rcParams["axes.unicode_minus"] = False
            break
        except Exception:  # noqa: BLE001
            continue

    # ---------- 检索消融柱状图 ----------
    ret_path = eval_dir / "retrieval_ablation_qa.json"
    if ret_path.exists():
        data = json.loads(ret_path.read_text(encoding="utf-8"))
        runs = data.get("runs", [])
        if runs:
            labels = [r["label"] for r in runs]
            metrics = ["recall@5", "mrr", "ndcg@5"]
            titles = ["Recall@5", "MRR", "NDCG@5"]

            fig, axes = plt.subplots(1, 3, figsize=(15, 4.2))
            for ax, metric, title in zip(axes, metrics, titles):
                values = [r[metric] for r in runs]
                bars = ax.bar(range(len(runs)), values, color="#2b7a78")
                ax.set_title(title, fontsize=12, fontweight="bold")
                ax.set_xticks(range(len(runs)))
                ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=9)
                ax.set_ylim(0, 1.05)
                ax.grid(axis="y", alpha=0.3)
                for bar, v in zip(bars, values):
                    ax.text(
                        bar.get_x() + bar.get_width() / 2, v + 0.02,
                        f"{v:.3f}", ha="center", fontsize=8.5,
                    )
            fig.suptitle("检索消融实验（195 题评测集）", fontsize=13, fontweight="bold")
            fig.tight_layout()
            out = out_dir / "fig_retrieval_ablation.png"
            fig.savefig(out, dpi=160)
            plt.close(fig)
            logger.info("  已写入 %s", out)

    # ---------- 生成质量雷达图 ----------
    gen_path = eval_dir / "generation_quality.json"
    if gen_path.exists():
        data = json.loads(gen_path.read_text(encoding="utf-8"))
        m = data.get("metrics", {})
        if m:
            dims = ["忠实度", "相关性", "引用准确率"]
            values = [m["faithfulness"], m["relevance"], m["citation_accuracy"]]
            angles = [i / len(dims) * 2 * 3.1415926 for i in range(len(dims))]
            angles += angles[:1]
            values_plot = values + values[:1]

            fig, ax = plt.subplots(figsize=(5.5, 5.5), subplot_kw={"polar": True})
            ax.plot(angles, values_plot, "o-", linewidth=2, color="#2b7a78")
            ax.fill(angles, values_plot, alpha=0.25, color="#3aafa9")
            ax.set_xticks(angles[:-1])
            ax.set_xticklabels(dims, fontsize=11)
            ax.set_ylim(0, 1)
            ax.set_title(
                f"生成质量（n={m['valid']}）", fontsize=12, fontweight="bold", pad=20
            )
            for angle, value, name in zip(angles[:-1], values, dims):
                ax.text(angle, value + 0.1, f"{value:.3f}", ha="center", fontsize=10)
            out = out_dir / "fig_generation_quality.png"
            fig.savefig(out, dpi=160, bbox_inches="tight")
            plt.close(fig)
            logger.info("  已写入 %s", out)

    return True


# ======================================================================
#  主流程
# ======================================================================
def graph_table(data: dict) -> str:
    """知识图谱规模表（Markdown）。"""
    g = data.get("graph") or {}
    if not g.get("available") or not g.get("nodes"):
        return ""

    type_zh = {
        "disease": "疾病", "procedure": "手术操作", "treatment": "非手术治疗",
        "complication": "并发症", "drug": "药物", "anatomy": "解剖部位",
        "exam": "检查与评估", "metric": "指标与结局",
    }
    rel_zh = {
        "CAUSES_COMPLICATION": "致病→并发症", "TREATS": "治疗疾病",
        "PREVENTS": "预防并发症", "DIAGNOSES": "诊断",
        "LOCATED_AT": "位于部位", "USES_DRUG": "使用药物",
        "COMPARES_WITH": "与其他方案比较", "HAS_RISK_FACTOR": "风险因素",
        "MEASURES": "测量指标", "APPLIES_TO": "适用于疾病",
    }

    lines = [
        f"知识图谱由 **{data.get('chunks_processed', 0)}** 个文本块抽取构建，"
        f"共 **{g['nodes']}** 个实体节点、**{g['relations']}** 条关系，"
        f"抽取耗时 {data.get('elapsed_sec', 0) / 60:.1f} 分钟。",
        "",
        "**实体类型分布**",
        "",
        "| 实体类型 | 数量 |",
        "|----------|------|",
    ]
    for t, n in sorted(g.get("by_type", {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"| {type_zh.get(t, t)} | {n} |")

    lines += ["", "**关系类型分布**", "", "| 关系类型 | 数量 |", "|----------|------|"]
    for t, n in sorted(g.get("by_relation", {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"| {rel_zh.get(t, t)}（{t}） | {n} |")

    return "\n".join(lines)


def try_make_graph_chart(eval_dir: Path, out_dir: Path) -> bool:
    """知识图谱实体类型分布图。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    path = eval_dir / "graph_build_result.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    g = data.get("graph") or {}
    if not g.get("by_type"):
        return False

    for font in ("Microsoft YaHei", "SimHei", "DejaVu Sans"):
        matplotlib.rcParams["font.sans-serif"] = [font]
        matplotlib.rcParams["axes.unicode_minus"] = False
        break

    type_zh = {
        "disease": "疾病", "procedure": "手术操作", "treatment": "非手术治疗",
        "complication": "并发症", "drug": "药物", "anatomy": "解剖部位",
        "exam": "检查与评估", "metric": "指标与结局",
    }
    rel_zh = {
        "CAUSES_COMPLICATION": "致病→并发症", "TREATS": "治疗疾病",
        "PREVENTS": "预防并发症", "DIAGNOSES": "诊断",
        "LOCATED_AT": "位于部位", "USES_DRUG": "使用药物",
        "COMPARES_WITH": "方案比较", "HAS_RISK_FACTOR": "风险因素",
        "MEASURES": "测量指标", "APPLIES_TO": "适用于疾病",
    }

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # 实体类型分布
    items = sorted(g["by_type"].items(), key=lambda kv: kv[1])
    labels = [type_zh.get(k, k) for k, _ in items]
    values = [v for _, v in items]
    axes[0].barh(labels, values, color="#2b7a78")
    axes[0].set_title(
        f"实体类型分布（共 {g['nodes']} 个节点）", fontsize=12, fontweight="bold"
    )
    axes[0].grid(axis="x", alpha=0.3)
    for i, v in enumerate(values):
        axes[0].text(v + max(values) * 0.01, i, str(v), va="center", fontsize=9)

    # 关系类型分布
    items2 = sorted(g["by_relation"].items(), key=lambda kv: kv[1])
    labels2 = [rel_zh.get(k, k) for k, _ in items2]
    values2 = [v for _, v in items2]
    axes[1].barh(labels2, values2, color="#3aafa9")
    axes[1].set_title(
        f"关系类型分布（共 {g['relations']} 条关系）", fontsize=12, fontweight="bold"
    )
    axes[1].grid(axis="x", alpha=0.3)
    for i, v in enumerate(values2):
        axes[1].text(v + max(values2) * 0.01, i, str(v), va="center", fontsize=9)

    fig.suptitle("结直肠手术围术期知识图谱规模", fontsize=13, fontweight="bold")
    fig.tight_layout()
    out = out_dir / "fig_knowledge_graph.png"
    fig.savefig(out, dpi=160)
    plt.close(fig)
    logger.info("  已写入 %s", out)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="生成论文实验材料")
    parser.add_argument("--eval-dir", default=r"D:\AI\RAG_project\data\eval")
    parser.add_argument("--out-dir", default=r"D:\AI\RAG_project\data\paper")
    args = parser.parse_args()

    setup_logging()
    eval_dir = Path(args.eval_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 80)
    logger.info("生成论文实验材料")
    logger.info("=" * 80)
    logger.info("评测结果目录: %s", eval_dir)
    logger.info("输出目录    : %s", out_dir)

    sections: list[str] = []
    sections.append("# 实验材料（自动生成）\n")
    sections.append(
        "> 本文件由 `scripts/make_paper_materials.py` 从评测结果自动生成。\n"
        "> 重跑评测后重新执行即可刷新，请勿手工编辑以免与数据不一致。\n"
    )

    # ---------- 评测集构成 ----------
    ds_path = eval_dir / "qa_dataset.json"
    if ds_path.exists():
        data = json.loads(ds_path.read_text(encoding="utf-8"))
        table = dataset_table(data)
        if table:
            sections += ["\n## 一、评测集构成\n", table, ""]
            # CSV：题目清单
            write_csv(
                out_dir / "table_dataset_items.csv",
                [
                    {
                        "编号": it["qid"], "问题": it["question"],
                        "分期": it.get("phase"), "类型": it.get("question_type"),
                        "证据等级": it.get("gold_evidence_level"),
                        "来源文献": it.get("gold_doc_title"),
                        "页码": it.get("gold_page"),
                    }
                    for it in data.get("items", [])
                ],
            )

    # ---------- 检索消融 ----------
    ret_path = eval_dir / "retrieval_ablation_qa.json"
    if ret_path.exists():
        data = json.loads(ret_path.read_text(encoding="utf-8"))
        table = retrieval_table(data)
        if table:
            sections += [
                "\n## 二、检索消融实验\n",
                f"评测集 {data.get('total', 0)} 题，单答案标注（gold chunk 精确匹配）。\n",
                table, "",
            ]
            write_csv(out_dir / "table_retrieval_ablation.csv", data.get("runs", []))

    # ---------- 生成质量 ----------
    gen_path = eval_dir / "generation_quality.json"
    if gen_path.exists():
        data = json.loads(gen_path.read_text(encoding="utf-8"))
        table = generation_table(data)
        if table:
            sections += ["\n## 三、生成质量\n", table, ""]
            write_csv(out_dir / "table_generation_quality.csv", [data.get("metrics", {})])

    # ---------- 知识图谱 ----------
    graph_path = eval_dir / "graph_build_result.json"
    if graph_path.exists():
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        table = graph_table(data)
        if table:
            sections += ["\n## 四、知识图谱构建\n", table, ""]
            g = data.get("graph") or {}
            write_csv(
                out_dir / "table_graph_types.csv",
                [{"实体类型": k, "数量": v} for k, v in (g.get("by_type") or {}).items()]
                + [{"实体类型": f"关系:{k}", "数量": v}
                   for k, v in (g.get("by_relation") or {}).items()],
            )

    # ---------- 图谱检索消融 ----------
    gab_path = eval_dir / "graph_retrieval_ablation.json"
    if gab_path.exists():
        data = json.loads(gab_path.read_text(encoding="utf-8"))
        runs = data.get("runs", [])
        if runs:
            lines = [
                "| 配置 | R@1 | R@3 | R@5 | R@10 | MRR | NDCG@5 | 延迟(ms) |",
                "|------|-----|-----|-----|------|-----|--------|----------|",
            ]
            for r in runs:
                lines.append(
                    f"| {r['label']} | {r['recall@1']:.3f} | {r['recall@3']:.3f} | "
                    f"{r['recall@5']:.3f} | {r['recall@10']:.3f} | {r['mrr']:.3f} | "
                    f"{r['ndcg@5']:.3f} | {r['avg_latency_ms']:.0f} |"
                )
            sections += [
                "\n## 五、知识图谱检索的边际贡献\n",
                "对比启用/关闭图谱路径的检索指标（同评测集、同其他配置）。\n",
                "\n".join(lines), "",
                "> **注意语言差异**：实体以中文抽取，故中文查询的同语言匹配收益显著"
                "（R@1 提升近 3 倍），而英文查询需跨语言匹配中文实体名，"
                "种子质量下降，呈现「排序精度提升但召回略降」。\n",
            ]
            write_csv(out_dir / "table_graph_ablation.csv", runs)

    # ---------- 写出 Markdown ----------
    md_path = out_dir / "experiment_results.md"
    md_path.write_text("\n".join(sections), encoding="utf-8")
    logger.info("\n已写入 %s", md_path)

    # ---------- 图表 ----------
    logger.info("\n生成图表…")
    try_make_charts(eval_dir, out_dir)
    try_make_graph_chart(eval_dir, out_dir)

    logger.info("\n完成。输出目录: %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
