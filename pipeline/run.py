"""Single entry point: raw files in, RunResult out."""
from __future__ import annotations

import pandas as pd

from pipeline import checks, ingest, mapping
from pipeline.contract import TABLE_COLUMNS, RunResult

_COLS = list(TABLE_COLUMNS) + ["file_ts", "derived", "step_k"]


def run(paths: list[str]) -> RunResult:
    files, seen_hashes = [], set()
    for p in paths:
        fd = ingest.read_file(p)
        if fd.sha256 in seen_hashes:      # byte-identical upload: process once
            continue
        seen_hashes.add(fd.sha256)
        files.append(fd)
    recs, queue = [], []
    for fd in files:
        r, items = mapping.to_records(fd)
        recs += r
        queue += items
    df = pd.DataFrame(recs, columns=_COLS)
    df, dup_items = ingest.resolve_duplicates(df)
    queue += dup_items
    df = ingest.derive_beginning(df).reset_index(drop=True)
    df, check_items = checks.run_checks(df, files)
    queue += check_items + checks.injection_items(files)
    table = df[list(TABLE_COLUMNS)].sort_values(["company", "month", "metric"]).reset_index(drop=True)
    return RunResult(table=table, review_queue=queue)
