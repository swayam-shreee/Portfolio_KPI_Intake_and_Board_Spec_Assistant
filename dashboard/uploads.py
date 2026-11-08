"""Saving uploaded KPI files into the state dir, with name, type, duplicate and readability checks."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from pipeline import ingest


@dataclass
class UploadResult:
    saved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)   # (name, reason)


SKIPPED_TEXT = "already uploaded (same name and file hash)"
REFUSED_TEXT = {
    "bad_name": "file name not allowed",
    "bad_type": "only .xlsx and .csv files are accepted",
    "name_conflict": "a different file with this name was already uploaded",
    "duplicate_content": "the same content was already uploaded under another name",
    "unreadable": "no monthly KPI data, or no company name in the file's title line or file name",
}


def _bad_name(name: str) -> bool:
    return not name or "/" in name or "\\" in name or ".." in name or name.startswith(".")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_uploads(cfg, files: list[tuple[str, bytes]]) -> UploadResult:
    uploads = Path(cfg.state_dir) / "uploads"
    incoming = Path(cfg.state_dir) / "incoming"
    result = UploadResult()
    for name, data in files:
        if _bad_name(name):
            result.refused.append((name, "bad_name"))
            continue
        if Path(name).suffix.lower() not in (".xlsx", ".csv"):
            result.refused.append((name, "bad_type"))
            continue
        existing = {f.name: _digest(f.read_bytes()) for f in uploads.iterdir()} if uploads.exists() else {}
        digest = _digest(data)
        if name in existing:
            if existing[name] == digest:
                result.skipped.append(name)
            else:
                result.refused.append((name, "name_conflict"))
            continue
        if digest in existing.values():
            result.refused.append((name, "duplicate_content"))
            continue
        incoming.mkdir(parents=True, exist_ok=True)
        staged = incoming / name
        staged.write_bytes(data)
        try:
            readable = ingest.read_file(staged).company is not None
        except Exception:
            readable = False
        if not readable:
            staged.unlink(missing_ok=True)
            result.refused.append((name, "unreadable"))
            continue
        uploads.mkdir(parents=True, exist_ok=True)
        os.replace(staged, uploads / name)
        result.saved.append(name)
    return result
