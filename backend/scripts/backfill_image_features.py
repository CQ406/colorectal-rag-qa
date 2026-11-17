"""Backfill missing RGB histogram features without changing publication state.

Preview: python -m scripts.backfill_image_features
Apply:   python -m scripts.backfill_image_features --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, or_

from app.db.session import SessionLocal
from app.models.knowledge import ImageAsset
from app.repositories import image_repo
from app.services.image_features import FEATURE_VERSION


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write missing features (default: preview only)")
    parser.add_argument("--limit", type=int, default=100, help="Maximum images to inspect per invocation")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    with SessionLocal() as db:
        ids = list(db.scalars(
            select(ImageAsset.id).where(or_(ImageAsset.feature_vector.is_(None),
                ImageAsset.feature_model.is_(None), ImageAsset.feature_model != FEATURE_VERSION))
            .order_by(ImageAsset.created_at, ImageAsset.id).limit(args.limit)
        ))
        print(f"Missing or outdated features in this batch: {len(ids)}; target={FEATURE_VERSION}")
        if not args.apply:
            print("Preview only. Pass --apply to write features; publication state is preserved.")
            return 0
        succeeded = failed = 0
        for image_id in ids:
            try:
                asset = db.get(ImageAsset, image_id)
                if asset is None:
                    continue
                vector = image_repo.ensure_feature(db, asset)
                if vector is None:
                    db.rollback()
                    failed += 1
                    continue
                db.commit()
                succeeded += 1
            except Exception as exc:
                db.rollback()
                failed += 1
                print(f"Failed {image_id}: {type(exc).__name__}")
        print(f"Backfilled: {succeeded}; failed: {failed}")
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
