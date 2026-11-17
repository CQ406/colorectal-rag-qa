"""Share graph-write invalidation between the importer and API processes."""
from __future__ import annotations

import os
from uuid import uuid4

from app.core.config import PROJECT_ROOT
from app.core.logging import get_logger

logger = get_logger("services.graph_revision")
REVISION_PATH = PROJECT_ROOT / "data/eval/graph_revision.txt"


def graph_revision() -> str:
    try:
        return REVISION_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning("图谱更新标记不可读，本次重新核对图谱: %s", exc)
        return uuid4().hex


def mark_graph_changed() -> None:
    """Publish after each committed batch, including a partially completed import."""
    temporary = REVISION_PATH.with_name(f".graph_revision_{uuid4().hex}.tmp")
    try:
        REVISION_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(uuid4().hex, encoding="utf-8")
        os.replace(temporary, REVISION_PATH)
    except OSError as exc:
        logger.warning("图谱更新标记写入失败，请重启 API 刷新实体索引: %s", exc)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
