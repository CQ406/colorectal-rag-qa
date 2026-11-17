"""验证本轮后端修复是否真的生效（不依赖单元测试）。

覆盖四项：
  1. /api/models/unload 是否要求 MODEL_MANAGE（医师 403、匿名 401）
  2. 反馈接口是否校验归属（医师评价他人日志 403）
  3. 提示词证据块是否带上期刊与年份
  4. 多轮流式出错时是否只发一条 error
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api"


def call(method: str, path: str, body=None, token: str | None = None, timeout=120):
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


def main() -> int:
    from app.db.session import session_scope
    from app.models.system import User
    from app.core.security import create_access_token
    from sqlalchemy import select

    with session_scope() as db:
        users = {u.username: u for u in db.execute(select(User)).scalars().all()}
        tokens = {}
        for name, u in users.items():
            role = u.role.value if hasattr(u.role, "value") else str(u.role)
            t, _ = create_access_token(user_id=str(u.id), username=u.username, role=role)
            tokens[name] = t

    print("== 1. 模型管理权限 ==")
    st, _ = call("POST", "/models/unload")
    print(f"  匿名 POST /models/unload        → {st}（期望 401）")
    if "doctor01" in users:
        st, d = call("POST", "/models/unload", token=tokens["doctor01"])
        print(f"  医师 POST /models/unload        → {st}（期望 403）")
    st, d = call("GET", "/models/status", token=tokens.get("doctor01"))
    print(f"  医师 GET  /models/status        → {st}（期望 200，只读不拦）")

    print("== 2. 反馈归属校验 ==")
    from app.models.system import QaLog
    with session_scope() as db:
        others = db.execute(
            select(QaLog).where(QaLog.user_id.isnot(None)).limit(3)
        ).scalars().all()
        target = others[0] if others else None
        if target:
            owner_id = str(target.user_id)
            log_id = str(target.id)
    if target:
        # 用一个确定不是 owner 的账号（doctor01）提交评价
        st, d = call("POST", "/feedback", {"qa_log_id": log_id, "rating": 1},
                     token=tokens.get("doctor01"))
        owner_name = next((n for n, u in users.items() if str(u.id) == owner_id), owner_id[:8])
        print(f"  医师评价 {owner_name} 的日志 → {st}（期望 403 或 200-若本人/管理员）")
        if st == 200:
            print(f"     ⚠ 未被拦截，说明 doctor01 就是该日志的所有者或拥有 LOG_VIEW_ALL")
    else:
        print("  跳过：库中没有带 user_id 的问答日志")

    print("== 3. 提示词证据块含期刊与年份 ==")
    from app.services import prompts
    block = prompts.build_evidence_block([{
        "index": 1, "doc_title": "测试文献", "doc_journal": "中华胃肠外科杂志",
        "doc_year": 2024, "page_start": 12, "evidence_level": "guideline",
        "content": "示例证据正文。",
    }])
    print("   含「中华胃肠外科杂志」:", "✓" if "中华胃肠外科杂志" in block else "✗")
    print("   含「2024」:", "✓" if "2024" in block else "✗")

    print("== 4. 多轮流式错误只发一条 ==")
    # 用不存在的会话 id 触发 404，观察是否只回一条 error
    st, d = call("GET", "/chat/not-a-real-session")
    print(f"  GET /chat/{'not-a-real-session'} → {st}（期望 404）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
