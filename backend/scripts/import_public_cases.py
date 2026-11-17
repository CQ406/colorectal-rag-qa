"""Import attributed public-case excerpts as unpublished drafts (idempotent).

Preview: python -m scripts.import_public_cases
Apply: python -m scripts.import_public_cases --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.api.v1.cases import _validate_source
from app.core.config import PROJECT_ROOT
from app.db.session import session_scope
from app.models.knowledge import CaseRecord
from app.models.system import AuditLog
from app.schemas.common import CaseCreateRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=PROJECT_ROOT / "data/cases/public_case_drafts.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.path.read_text(encoding="utf-8"))
    entries = [CaseCreateRequest.model_validate(item) for item in payload["cases"]]
    for entry in entries:
        _validate_source(entry.model_dump(), publishing=False)
        if entry.is_public or not all((entry.source_title, entry.source_url, entry.source_license, entry.source_note)):
            raise ValueError("Import requires attributed drafts, never published entries")
    with session_scope() as db:
        known = set(db.scalars(select(CaseRecord.case_code)))
        pending = [entry for entry in entries if entry.case_code not in known]
        print(f"Validated: {len(entries)}; new drafts: {len(pending)}; existing: {len(entries)-len(pending)}")
        if not args.apply:
            print("Preview only; use --apply to import drafts.")
            return 0
        for entry in pending:
            data = entry.model_dump()
            data["prepared_by"] = "public-source-import"
            case = CaseRecord(**data)
            db.add(case)
            db.flush()
            db.add(AuditLog(created_at=datetime.now(timezone.utc), username="public-source-import",
                action="case_import_draft", resource="case", resource_id=str(case.id), success=True,
                detail={"case_code": case.case_code, "source_url": case.source_url, "source_license": case.source_license,
                        "status": "source_excerpt_pending_review", "embedding": "pending; lexical fallback available"}))
        print(f"Imported {len(pending)} drafts; existing entries and publication state preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
