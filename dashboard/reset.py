"""Starting over: move the saved state aside so the next session opens empty. Nothing is deleted."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path


def archive_state(cfg) -> Path | None:
    """Move every entry in cfg.state_dir into <state_dir parent>/archive/<UTC timestamp>/.

    Returns the archive folder, or None when there was nothing to move. The learned-alias copy
    goes too; review.load re-seeds it from pipeline/aliases.yaml.
    """
    state = Path(cfg.state_dir)
    entries = sorted(state.iterdir()) if state.is_dir() else []
    if not entries:
        return None
    root = state.parent / "archive"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest, n = root / stamp, 1
    while dest.exists():
        n += 1
        dest = root / f"{stamp}-{n}"
    dest.mkdir(parents=True)
    for entry in entries:
        shutil.move(str(entry), str(dest / entry.name))
    return dest


def has_saved_state(cfg) -> bool:
    uploads = Path(cfg.state_dir) / "uploads"
    return uploads.is_dir() and any(uploads.iterdir())
