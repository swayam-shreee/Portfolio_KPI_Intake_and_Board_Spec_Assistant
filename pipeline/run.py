"""Single entry point: raw files in, RunResult out."""
from __future__ import annotations

import pandas as pd

from pipeline import checks, ingest, mapping
from pipeline.contract import TABLE_COLUMNS, RunResult

_COLS = list(TABLE_COLUMNS) + ["file_ts", "step_k"]


def _apply_corrections(df: pd.DataFrame, corrections: dict) -> pd.DataFrame:
    """Reviewer corrections: {(company, month, metric, source_file, source_cell): value_usd_k}.
    A correction only hits the exact cell it was made for, so a new file for that month (another
    cell or file) is checked on its own. The row keeps its source file and cell."""
    if not corrections or df.empty:
        return df
    df = df.copy()
    keys = list(zip(df.company, df.month, df.metric, df.source_file, df.source_cell))
    hit = [k in corrections for k in keys]
    df.loc[hit, "value_usd_k"] = [corrections[k] for k, h in zip(keys, hit) if h]
    return df


def run(paths: list[str], *, aliases_path=None, accepted: frozenset = frozenset(),
        corrections: dict | None = None) -> RunResult:
    """``aliases_path``, ``accepted`` and ``corrections`` default to today's behavior (tracked
    aliases, nothing accepted, nothing corrected)."""
    files, seen_hashes = [], set()
    for p in paths:
        fd = ingest.read_file(p)
        if fd.sha256 in seen_hashes:      # byte-identical upload: process once
            continue
        seen_hashes.add(fd.sha256)
        files.append(fd)
    recs, queue = [], []
    for fd in files:
        r, items = mapping.to_records(fd, aliases_path)
        recs += r
        queue += items
    df = pd.DataFrame(recs, columns=_COLS)
    df, dup_items = ingest.resolve_duplicates(df)
    queue += dup_items
    df = _apply_corrections(df, corrections or {})     # before derivation: next month's beginning follows
    df = ingest.derive_beginning(df).reset_index(drop=True)
    df, check_items = checks.run_checks(df, files, accepted)
    queue += check_items + checks.injection_items(files)
    table = df[list(TABLE_COLUMNS)].astype({"derived": bool})
    table = table.sort_values(["company", "month", "metric"]).reset_index(drop=True)
    return RunResult(table=table, review_queue=queue)
