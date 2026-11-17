"""验证运行时配置是否真的生效（配置表 → 检索链路）。

做法：用管理员接口把 `retrieval.graph_weight` 改掉，再看 /api/search 返回的
`recall_paths[].weight` 是否跟着变。修之前这里永远是 settings 里的 0.2。

跑完会把配置恢复成原值。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api"


def call(method: str, path: str, body=None, token: str | None = None, timeout=300):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"{}")
    except HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            payload = {}
        return e.code, payload


def graph_weight_in_search(token: str) -> float | None:
    st, d = call("POST", "/search",
                 {"query": "吻合口漏怎么预防？", "top_k": 5,
                  "recall_mode": "hybrid", "use_graph": True},
                 token=token, timeout=600)
    if st != 200:
        print(f"  /search 返回 {st}: {d}")
        return None
    for p in d.get("recall_paths") or []:
        if p.get("name") == "graph":
            return p.get("weight")
    return None


def main() -> int:
    from app.db.session import session_scope
    from app.models.system import User
    from app.core.security import create_access_token
    from sqlalchemy import select

    with session_scope() as db:
        u = db.execute(select(User).where(User.username == "admin")).scalar_one()
        role = u.role.value if hasattr(u.role, "value") else str(u.role)
        token, _ = create_access_token(user_id=str(u.id), username=u.username, role=role)

    st, cfg = call("GET", "/admin/config?category=retrieval", token=token)
    if st != 200:
        print(f"  取配置失败 {st}: {cfg}")
        return 1
    row = next((x for x in cfg["items"] if x["key"] == "retrieval.graph_weight"), None)
    if not row:
        print("  没找到 retrieval.graph_weight")
        return 1
    original = row["parsed_value"]
    print(f"  当前配置值: {original}")

    before = graph_weight_in_search(token)
    print(f"  检索返回的 graph 权重（改前）: {before}")

    try:
        for target in (0.9, 0.05):
            st, d = call("PUT", "/admin/config",
                         {"key": "retrieval.graph_weight", "value": target}, token=token)
            print(f"  PUT graph_weight={target} → {st}")
            after = graph_weight_in_search(token)
            ok = after is not None and abs(after - target) < 1e-6
            print(f"  检索返回权重: {after} → {'✓ 已生效' if ok else '✗ 未生效'}")
    finally:
        st, _ = call("PUT", "/admin/config",
                     {"key": "retrieval.graph_weight", "value": original}, token=token)
        restored = graph_weight_in_search(token)
        print(f"  已恢复为 {original}（检索返回 {restored}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
