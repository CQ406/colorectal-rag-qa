"""按人工复核结果发布 kb_case 草稿。

本脚本只执行「已经由人复核过」的发布动作：

- 不判断病例能否用于教学、是否已脱敏；
- 不生成、不补写、不猜测复核意见 / 复核人 / 复核日期；
- 不修改 title、diagnosis、surgery 等任何临床字段，不调用任何模型。

复核结论只能来自复核人填写的 CSV。脚本负责校验、原样搬运结论，并留下审计记录。

用法（在 backend 目录执行）：:

    python -m scripts.publish_reviewed_cases --dry-run      # 只打印，不写库
    python -m scripts.publish_reviewed_cases                # 真正发布
    python -m scripts.publish_reviewed_cases --csv <路径>   # 指定其他 CSV

输入 CSV（UTF-8-SIG，Excel 可直接打开）的列：:

    case_code,approved,deidentified,review_note,reviewed_by,reviewed_at

逐条校验，任一不满足即拒绝该条并打印原因（不写库）：

- ``approved`` 必须为 ``yes``；
- ``deidentified`` 必须为 ``yes``；
- ``review_note`` 去空白后非空且长度 >= 10；
- ``reviewed_by`` 非空；
- ``reviewed_at`` 非空且可解析（``YYYY-MM-DD`` 或 ISO 8601）。
  **空值不会被自动填成今天**——那等于替复核人伪造复核日期。

通过校验的条目：写入 ``review_note`` / ``reviewed_by`` / ``reviewed_at``，
并把 ``is_public`` 置为 true；同时写一条 ``case_publish`` 审计日志。

退出码：0 = 正常执行完（含部分或全部拒绝）；2 = 输入文件 / 表头缺失。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# 允许 `python -m scripts.publish_reviewed_cases` 与直接运行脚本两种方式。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1.cases import _validate_source
from app.core.config import PROJECT_ROOT
from app.db.session import session_scope
from app.models.knowledge import CaseRecord
from app.models.system import AuditLog
from app.repositories import case_repo

REQUIRED_COLUMNS = ("case_code", "approved", "deidentified", "review_note", "reviewed_by", "reviewed_at")
MIN_REVIEW_NOTE = 10
ACTOR = "publish_reviewed_cases"

# Windows 路径用 raw string，避免 \t、\n 等被当成转义字符。
DEFAULT_CSV = PROJECT_ROOT / r"data\cases\case_review.csv"

_LICENSE_RE = re.compile(r"^CC[ -]BY(?:[ -]\d\.\d)?$", re.IGNORECASE)


def _parse_reviewed_at(raw: str) -> tuple[datetime | None, str | None]:
    """解析复核日期；只接受明确写出的日期，绝不默认成今天。"""
    text = raw.strip()
    if not text:
        return None, "reviewed_at 为空：必须填写复核人实际复核日期（YYYY-MM-DD 或 ISO 8601），脚本不会代填"
    try:
        if len(text) == 10:
            # 纯日期按 UTC 零点写入，避免界面按 UTC 显示时日期倒退一天。
            day = date.fromisoformat(text)
            value = datetime.combine(day, time.min, tzinfo=timezone.utc)
        else:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if value.tzinfo is None:
                # 无时区的日期时间按本机时区理解，与复核人填写时看到的日期一致。
                value = value.astimezone()
    except ValueError:
        return None, f"reviewed_at 无法解析：{text!r}（应为 YYYY-MM-DD 或 ISO 8601）"
    value = value.astimezone(timezone.utc)
    if value > datetime.now(timezone.utc) + timedelta(days=1):
        return None, f"reviewed_at 晚于今天：{text}（复核日期不能是未来日期）"
    return value, None


def _license_hint(license_text: str | None) -> str | None:
    """许可字段只是格式提示，不阻断；是否可发布仍由复核人判断。"""
    text = (license_text or "").strip()
    if text and _LICENSE_RE.match(text):
        return None
    return f"来源许可字段为 {text or '<空>'}，不是标准 CC BY 写法；请人工核对原文许可声明"


def _validate_candidate(
    db, row: dict, seen: dict[str, int], line_no: int,
) -> tuple[tuple[CaseRecord, str, str, datetime] | None, list[str]]:
    """按复核结论校验一行；返回（候选元组, 拒绝原因列表）。"""
    reasons: list[str] = []
    code = (row.get("case_code") or "").strip()
    if not code:
        return None, ["case_code 为空"]
    if code in seen:
        return None, [f"case_code 在 CSV 中重复（第 {seen[code]} 行已出现）"]
    seen[code] = line_no

    case = case_repo.get_by_code(db, code)
    if case is None:
        return None, [f"库中不存在 case_code={code}"]

    approved = (row.get("approved") or "").strip()
    if approved.lower() != "yes":
        reasons.append(f"approved 必须为 yes（当前：{approved or '<空>'}）")
    deidentified = (row.get("deidentified") or "").strip()
    if deidentified.lower() != "yes":
        reasons.append(f"deidentified 必须为 yes（当前：{deidentified or '<空>'}）")
    note = (row.get("review_note") or "").strip()
    if len(note) < MIN_REVIEW_NOTE:
        reasons.append(f"review_note 去空白后长度 {len(note)} < {MIN_REVIEW_NOTE}（当前：{note or '<空>'}）")
    reviewer = (row.get("reviewed_by") or "").strip()
    if not reviewer:
        reasons.append("reviewed_by 为空")
    reviewed_at, date_error = _parse_reviewed_at(row.get("reviewed_at") or "")
    if date_error:
        reasons.append(date_error)

    # 复用 API 的发布校验（来源标题/URL/许可/整理依据 + review_note），脚本不绕过。
    payload = {
        "source_title": case.source_title,
        "source_url": case.source_url,
        "source_license": case.source_license,
        "source_note": case.source_note,
        "review_note": note,
    }
    try:
        _validate_source(payload, publishing=True)
    except HTTPException as exc:
        # 上面的长度校验已覆盖 review_note；这里只补充来源字段本身的问题。
        detail = str(exc.detail)
        missing = detail.rsplit(":", 1)[-1].strip()
        if missing != "review_note":
            reasons.append(f"来源字段校验未通过（与 API 一致）：{detail}")

    if reasons:
        return None, reasons
    return (case, note, reviewer, reviewed_at), []


def main() -> int:
    parser = argparse.ArgumentParser(description="按人工复核 CSV 发布病例（只搬运结论，不做判定）")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="复核结果 CSV 路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么，不写库")
    args = parser.parse_args()

    if not args.csv.is_file():
        print(f"[错误] 找不到 CSV：{args.csv}", file=sys.stderr)
        print("       请先复制 data/cases/case_review_template.csv 为 case_review.csv 并交复核人填写。", file=sys.stderr)
        return 2

    with args.csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            print(f"[错误] CSV 缺少列：{', '.join(missing)}", file=sys.stderr)
            return 2
        rows = list(reader)

    with session_scope() as db:
        public_before = db.scalar(
            select(func.count()).select_from(CaseRecord).where(CaseRecord.is_public.is_(True))
        ) or 0
        print(f"CSV：{args.csv}")
        print(f"库中当前公开病例：{public_before} 条；CSV 数据行：{len(rows)} 行")
        if args.dry_run:
            print("模式：dry-run（只打印，不写库）")
        print()

        seen: dict[str, int] = {}
        passed: list[tuple[CaseRecord, str, str, datetime]] = []
        rejected = 0

        for line_no, row in enumerate(rows, start=2):
            candidate, reasons = _validate_candidate(db, row, seen, line_no)
            code = (row.get("case_code") or "").strip() or "<空 case_code>"
            if candidate is None:
                rejected += 1
                print(f"[拒绝] {code}（CSV 第 {line_no} 行）")
                for reason in reasons:
                    print(f"         - {reason}")
                continue

            case, note, reviewer, reviewed_at = candidate
            passed.append(candidate)
            public_before_this = case.is_public
            state = "已是公开，将更新复核信息" if public_before_this else "将把 is_public 置为 true"
            print(f"[通过] {code}（CSV 第 {line_no} 行）：{state}")
            print(f"         reviewed_by={reviewer}；reviewed_at={reviewed_at.isoformat()}；review_note 长度 {len(note)}")
            hint = _license_hint(case.source_license)
            if hint:
                print(f"         提示：{hint}")
            if not args.dry_run:
                # review_note 不参与 case_text()，无需重建向量。
                case.review_note = note
                case.reviewed_by = reviewer
                case.reviewed_at = reviewed_at
                case.is_public = True
                db.add(AuditLog(
                    created_at=datetime.now(timezone.utc),
                    username=ACTOR,
                    action="case_publish",
                    resource="case",
                    resource_id=str(case.id),
                    success=True,
                    detail={
                        "case_code": case.case_code,
                        "source_url": case.source_url,
                        "source_license": case.source_license,
                        "reviewed_by": reviewer,
                        "reviewed_at": reviewed_at.isoformat(),
                        "review_note": note,
                        "is_public_before": public_before_this,
                        "via": "publish_reviewed_cases",
                        "csv": str(args.csv),
                    },
                ))

        print("-" * 64)
        if args.dry_run:
            print(f"dry-run 完成：通过 {len(passed)} 条，拒绝 {rejected} 条；未修改数据库（公开病例仍为 {public_before} 条）。")
            db.rollback()
            return 0

        if passed:
            db.commit()
            public_after = db.scalar(
                select(func.count()).select_from(CaseRecord).where(CaseRecord.is_public.is_(True))
            ) or 0
            print(f"发布完成：写入 {len(passed)} 条，拒绝 {rejected} 条；公开病例 {public_before} → {public_after} 条。")
        else:
            print(f"未发布任何病例：通过 0 条，拒绝 {rejected} 条；公开病例仍为 {public_before} 条。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
