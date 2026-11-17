"""测量流式问答期间是否还占着数据库连接。

做法：后台发起一个流式问答，等它进入"生成中"阶段后，
从另一个连接查 pg_stat_activity，看有多少会话处于 `idle in transaction`
（即占着连接却不在干活）。修复前应该有 1 个，修复后应为 0。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

import psycopg

BASE = "http://127.0.0.1:8000/api"
# 连接串从配置读取，避免把口令写死在脚本里
from app.core.config import settings as _settings
DSN = (
    f"host={_settings.postgres.host} port={_settings.postgres.port} "
    f"dbname={_settings.postgres.db} user={_settings.postgres.user} "
    f"password={_settings.postgres.password}"
)


def count_idle_in_tx() -> int:
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = 'colorectal_rag' AND state = 'idle in transaction'"
        ).fetchone()
    return int(row[0])


def stream_once(question: str, holder: dict) -> None:
    body = json.dumps({"question": question, "top_k": 5}).encode()
    req = urllib.request.Request(
        BASE + "/qa/stream", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=600) as resp:
            for _line in resp:
                holder["bytes"] = holder.get("bytes", 0) + len(_line)
                if holder.get("first_delta") is None and b"delta" in _line:
                    holder["first_delta"] = time.time()
    except Exception as exc:  # noqa: BLE001
        holder["error"] = str(exc)[:120]


def main() -> int:
    holder: dict = {}
    t0 = time.time()
    th = threading.Thread(target=stream_once, args=("吻合口漏的诊断标准和处理原则是什么？", holder))
    th.start()

    # 等流式进入生成阶段（收到第一个 delta），再采样
    for _ in range(120):
        if holder.get("first_delta"):
            break
        time.sleep(0.5)

    samples = []
    for _ in range(6):
        samples.append(count_idle_in_tx())
        time.sleep(0.8)

    th.join(timeout=300)
    elapsed = time.time() - t0
    print(f"  流式耗时 {elapsed:.1f}s，收到 {holder.get('bytes', 0)} 字节")
    print(f"  生成期间 idle-in-transaction 采样: {samples}")
    print(f"  峰值: {max(samples)} 个（修复前应为 1，修复后应为 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
