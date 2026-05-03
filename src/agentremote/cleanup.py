from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .common import PARTIAL_DIR_NAME


def cleanup_stale_partials(root: Path, *, older_than_hours: float = 24.0) -> dict[str, Any]:
    root = root.resolve()
    partial_dir = root / PARTIAL_DIR_NAME
    cutoff = time.time() - max(0.0, older_than_hours) * 3600
    removed = []
    kept = 0
    freed_bytes = 0
    if partial_dir.exists():
        for path in sorted(partial_dir.iterdir()):
            if not path.is_file():
                kept += 1
                continue
            if path.stat().st_mtime > cutoff:
                kept += 1
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            freed_bytes += size
            removed.append(str(path))
    return {
        "root": str(root),
        "partialDir": str(partial_dir),
        "olderThanHours": older_than_hours,
        "removedFiles": len(removed),
        "freedBytes": freed_bytes,
        "keptFiles": kept,
        "removed": removed,
    }
