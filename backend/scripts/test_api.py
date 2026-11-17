"""API 接口测试脚本（阶段一验收）。

用 Python 而非 PowerShell 发送请求，避免中文编码在控制台被破坏。

用法（需先启动服务）:
    python -m scripts.test_api
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

BASE = "http://127.0.0.1:8000"
API = f"{BASE}/api"


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def test_root(client: httpx.Client) -> bool:
    hr("1. 服务信息 GET /")
    r = client.get(f"{BASE}/")
    r.raise_for_status()
    data = r.json()
    for k, v in data.items():
        print(f"  {k:<14}: {v}")
    return data["version"] == "0.1.0"


def test_health(client: httpx.Client) -> bool:
    hr("2. 健康检查 GET /api/health")
    r = client.get(f"{API}/health", timeout=120)
    r.raise_for_status()
    data = r.json()
    print(f"  总体状态: {data['status']}")
    print("\n  组件:")
    for c in data["components"]:
        mark = "OK" if c["ok"] else "--"
        print(f"    [{mark}] {c['name']:<24} {c['detail']}")

    models = data["models"]
    print(f"\n  设备: {models['device']}")
    print(f"  BGE-M3 目录存在: {models['embedding']['path_exists']} | 已加载: {models['embedding']['loaded']}")
    print(f"  reranker 目录存在: {models['reranker']['path_exists']} | 已加载: {models['reranker']['loaded']}")
    vram = models["vram"]
    if vram.get("available"):
        print(f"  显存: {vram['used_gb']} / {vram['total_gb']} GB 已用, 空闲 {vram['free_gb']} GB")

    # 基础设施必须全部可用；LLM 密钥未配置属预期（阶段三/四才需要）
    infra = {"PostgreSQL+pgvector", "Neo4j", "Redis", "文件存储"}
    infra_ok = all(c["ok"] for c in data["components"] if c["name"] in infra)
    print(f"\n  基础设施全部可用: {infra_ok}")
    return infra_ok


def test_phases(client: httpx.Client) -> bool:
    hr("3. 分期与证据等级枚举 GET /api/phases")
    r = client.get(f"{API}/phases")
    r.raise_for_status()
    data = r.json()
    print("  围术期分期:")
    for p in data["phases"]:
        print(f"    {p['value']:<10} {p['label']}")
    print("  证据等级:")
    for e in data["evidence_levels"]:
        print(f"    {e['value']:<12} {e['label']:<16} 权重={e['weight']}")
    return len(data["phases"]) == 4 and len(data["evidence_levels"]) == 8


def test_stats(client: httpx.Client) -> bool:
    hr("4. 知识库统计 GET /api/stats")
    r = client.get(f"{API}/stats")
    r.raise_for_status()
    data = r.json()
    print(f"  文献数: {data['documents']}")
    print(f"  文本块: {data['total_chunks']} (已向量化 {data['embedded_chunks']})")
    print(f"  分期分布: {json.dumps(data['phase_distribution'], ensure_ascii=False)}")
    print(f"  证据等级分布: {json.dumps(data['evidence_distribution'], ensure_ascii=False)}")
    return data["total_chunks"] > 0


def test_embed(client: httpx.Client) -> bool:
    hr("5. 向量化自检 POST /api/models/embed")
    payload = {
        "texts": [
            "结直肠癌术后吻合口漏的处理原则包括充分引流与抗生素治疗。",
            "加速康复外科可缩短结直肠手术患者的住院时间。",
        ],
        "batch_size": 2,
    }
    t0 = time.perf_counter()
    r = client.post(f"{API}/models/embed", json=payload, timeout=300)
    wall = time.perf_counter() - t0
    r.raise_for_status()
    data = r.json()
    print(f"  条数: {data['count']}  维度: {data['dim']}")
    print(f"  服务端耗时: {data['elapsed_ms']} ms  吞吐: {data['per_second']} 条/秒")
    print(f"  端到端耗时(含首次模型加载): {wall:.1f} 秒")
    print(f"  向量预览(前8维): {data['preview'][0]}")
    return data["dim"] == 1024


def test_search(client: httpx.Client, label: str, query: str, warm: bool = False) -> dict:
    """执行一次检索并打印结果。"""
    payload = {"query": query, "top_k": 3, "use_rerank": True, "recall_limit": 10}
    t0 = time.perf_counter()
    r = client.post(f"{API}/search", json=payload, timeout=300)
    wall = time.perf_counter() - t0
    r.raise_for_status()
    data = r.json()

    print(f"\n  查询: {query}")
    print(f"  候选 {data['total_candidates']} 条 -> 返回 {data['returned']} 条 | 重排={data['reranked']}")
    print(f"  服务端总耗时: {data['elapsed_ms']} ms (端到端 {wall:.2f} s)")
    print(f"  阶段耗时: {json.dumps(data['timings'], ensure_ascii=False)}")
    print("  结果:")
    for item in data["results"]:
        print(f"    #{item['rank']} 重排={item['rerank_score']} 向量={item['vector_score']}")
        print(f"       来源: {item['doc_title']} ({item['doc_journal']}, {item['doc_year']})")
        print(f"       元数据: 分期={item['phase']} 证据={item['evidence_level']} "
              f"页码=p.{item['page_start']} 章节={item['section']}")
        preview = item["content"][:60]
        print(f"       内容: {preview}...")
    return data


def test_search_cold_warm(client: httpx.Client) -> bool:
    hr("6. 检索接口 POST /api/search（冷启动 vs 热启动）")
    query = "结直肠癌术后吻合口漏应该怎么处理？"

    print("--- 首次调用（含模型加载，属预期的一次性开销）---")
    cold = test_search(client, "cold", query)

    print("\n--- 再次调用（模型已驻留显存，反映真实检索性能）---")
    warm = test_search(client, "warm", query)

    cold_ms = cold["elapsed_ms"]
    warm_ms = warm["elapsed_ms"]
    print(f"\n  冷启动 {cold_ms} ms -> 热启动 {warm_ms} ms "
          f"(模型加载约 {cold_ms - warm_ms:.0f} ms)")

    # 验证结果正确性：第 1 名应为诊疗规范，且是吻合口漏相关内容
    top = warm["results"][0] if warm["results"] else None
    correct = top is not None and "吻合口漏" in top["content"]
    print(f"  结果正确性: 第1名包含'吻合口漏' = {correct}")
    print(f"  热启动是否在 5 秒内: {warm_ms < 5000}")
    return correct and warm_ms < 5000


def test_search_phase_filter(client: httpx.Client) -> bool:
    hr("7. 分期过滤检索（验证域路由能力）")
    for phase, label in [("preop", "术前"), ("postop", "术后")]:
        payload = {
            "query": "营养支持与饮食管理",
            "top_k": 2,
            "use_rerank": False,
            "recall_limit": 10,
            "phase": phase,
        }
        r = client.post(f"{API}/search", json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        phases = [x["phase"] for x in data["results"]]
        print(f"  过滤 {label}({phase}): 返回 {data['returned']} 条, 实际分期={phases}")
    return True


def main() -> int:
    print("API 接口测试（阶段一验收）")
    results: dict[str, bool] = {}

    with httpx.Client() as client:
        try:
            results["1.服务信息"] = test_root(client)
            results["2.健康检查"] = test_health(client)
            results["3.枚举接口"] = test_phases(client)
            results["4.知识库统计"] = test_stats(client)
            results["5.向量化自检"] = test_embed(client)
            results["6.检索接口"] = test_search_cold_warm(client)
            results["7.分期过滤"] = test_search_phase_filter(client)
        except httpx.ConnectError:
            print("\n无法连接服务，请先启动: python -m uvicorn app.main:app --port 8000")
            return 2

    hr("阶段一 API 验收汇总")
    for name, ok in results.items():
        print(f"  [{'通过' if ok else '未通过'}] {name}")
    passed = sum(results.values())
    print(f"\n  结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
