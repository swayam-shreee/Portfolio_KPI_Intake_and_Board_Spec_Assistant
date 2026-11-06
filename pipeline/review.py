"""Review loop: decisions on review items, alias learning, per-company reruns.

No Streamlit and no Claude here. ``decisions.jsonl`` in the state dir is the source of truth;
a ``Portfolio`` is a cache of "run the pipeline with those decisions applied". All state is
immutable from the caller's view: ``resolve`` and ``rerun_company`` return a new Portfolio.
"""
from __future__ import annotations

import glob
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline import ingest, mapping, metrics, run
from pipeline.contract import METRIC_COLUMNS, ReviewItem

ROOT = Path(__file__).resolve().parents[1]
CONFIRMABLE = ("unknown_label", "impossible_value", "duplicate_submission", "prompt_injection")
CLOSED_AT_INGEST = ("unknown_label", "duplicate_submission", "prompt_injection")
_TABLE_KEYS = ["company", "month", "metric"]
_METRIC_KEYS = ["company", "month", "metric_id"]


@dataclass(frozen=True)
class Config:
    data_dir: Path
    state_dir: Path
    aliases_path: Path
    seed_aliases_path: Path

    @classmethod
    def default(cls) -> "Config":
        state = ROOT / "outputs" / "state"
        return cls(data_dir=ROOT / "data" / "raw", state_dir=state,
                   aliases_path=state / "aliases.yaml",
                   seed_aliases_path=ROOT / "pipeline" / "aliases.yaml")


@dataclass
class Portfolio:
    paths: list
    files_by_company: dict
    months: list
    table: pd.DataFrame
    review_queue: list
    metrics: pd.DataFrame
    decisions: list
    raw_labels: dict          # item_id -> label, cached at load


# --------------------------------------------------------------------------- small helpers
def item_id(item: ReviewItem) -> str:
    parts = (item.company, item.month, item.metric, item.error_type, item.source_file, item.source_cell)
    return hashlib.sha1("|".join("" if x is None else str(x) for x in parts).encode()).hexdigest()[:12]


_item_id = item_id     # resolve() has a parameter that shadows the name


def raw_label(p: Portfolio, item: ReviewItem) -> str | None:
    if item.error_type != "unknown_label":
        return None
    return p.raw_labels.get(item_id(item))


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_decisions(cfg: Config) -> list:
    f = cfg.state_dir / "decisions.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def _accepted(decisions: list) -> frozenset:
    return frozenset((d["company"], d["month"], d["metric"]) for d in decisions
                     if d["action"] == "confirm" and d["error_type"] == "impossible_value")


def _closed(decisions: list) -> frozenset:
    return frozenset(d["item_id"] for d in decisions
                     if d["action"] == "confirm" and d["error_type"] in CLOSED_AT_INGEST)


def _open_items(queue: list, decisions: list) -> list:
    closed = _closed(decisions)
    return [i for i in queue if item_id(i) not in closed]


def _write_record(cfg: Config, kind: str, paths: list, queue: list, decisions: list) -> None:
    runs = cfg.state_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    record = {"kind": kind,
              "inputs": [{"file": Path(x).name, "sha256": _sha256(x)} for x in paths],
              "open_item_ids": [item_id(i) for i in queue],
              "decisions": decisions}
    seq = sum(1 for _ in runs.iterdir())
    while True:
        target = runs / f"{time.time_ns() // 1000}-{seq}-{kind}.json"
        try:
            with open(target, "x") as f:
                json.dump(record, f, indent=1)
            return
        except FileExistsError:
            seq += 1


def _seed_aliases(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.aliases_path.exists():
        cfg.aliases_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cfg.seed_aliases_path, cfg.aliases_path)
    mapping.load_aliases.cache_clear()


def _replay_aliases(cfg: Config, decisions: list) -> None:
    for d in decisions:
        if d["action"] == "confirm" and d["error_type"] == "unknown_label" and d.get("label"):
            try:
                mapping.learn_alias(d["label"], d["learned_metric"], cfg.aliases_path)
            except ValueError:
                continue     # a stale decision never stops a load


# --------------------------------------------------------------------------- load / rerun / resolve
def load(cfg: Config, paths: list | None = None) -> Portfolio:
    if paths is None:
        paths = sorted(glob.glob(str(cfg.data_dir / "*.xlsx")) + glob.glob(str(cfg.data_dir / "*.csv")))
    paths = [Path(x) for x in paths]
    _seed_aliases(cfg)
    decisions = _read_decisions(cfg)
    _replay_aliases(cfg, decisions)

    files_by_company: dict = {}
    labels_at: dict = {}
    for path in paths:
        fd = ingest.read_file(path)
        files_by_company.setdefault(fd.company or fd.source_file, []).append(path)
        for s in fd.series:
            if not mapping.looks_like_instruction(s.label):
                labels_at[(fd.source_file, s.label_cell)] = s.label

    res = run.run(paths, aliases_path=cfg.aliases_path, accepted=_accepted(decisions))
    queue = _open_items(res.review_queue, decisions)
    months = sorted(res.table["month"].unique())
    raw_labels = {item_id(i): labels_at[(i.source_file, i.source_cell)] for i in queue
                  if i.error_type == "unknown_label" and (i.source_file, i.source_cell) in labels_at}
    table = res.table.reset_index(drop=True)
    p = Portfolio(paths=paths, files_by_company=files_by_company, months=months, table=table,
                  review_queue=queue, metrics=metrics.compute(table, queue, months=months),
                  decisions=decisions, raw_labels=raw_labels)
    _write_record(cfg, "load", paths, queue, decisions)
    return p


def _rerun(p: Portfolio, company: str, cfg: Config, decisions: list) -> Portfolio:
    files = p.files_by_company[company]
    res = run.run(files, aliases_path=cfg.aliases_path, accepted=_accepted(decisions))
    queue = _open_items(res.review_queue, decisions)
    new_metrics = metrics.compute(res.table, queue, months=p.months)
    table = pd.concat([p.table[p.table["company"] != company], res.table], ignore_index=True)
    table = table.sort_values(_TABLE_KEYS).reset_index(drop=True)
    merged = pd.concat([p.metrics[p.metrics["company"] != company], new_metrics], ignore_index=True)
    # rebuild from rows so every column gets the dtype metrics.compute gives a whole table
    merged = pd.DataFrame(merged.to_dict("records"), columns=list(METRIC_COLUMNS))
    merged["value"] = merged["value"].astype(object).where(merged["value"].notna(), None)
    merged = merged.sort_values(_METRIC_KEYS, kind="stable").reset_index(drop=True)
    return Portfolio(paths=list(p.paths),
                     files_by_company={c: list(v) for c, v in p.files_by_company.items()},
                     months=list(p.months), table=table,
                     review_queue=[i for i in p.review_queue if i.company != company] + queue,
                     metrics=merged, decisions=list(decisions), raw_labels=dict(p.raw_labels))


def rerun_company(p: Portfolio, company: str, cfg: Config) -> Portfolio:
    return _rerun(p, company, cfg, p.decisions)


def resolve(p: Portfolio, item_id: str, action: str, cfg: Config) -> Portfolio:  # noqa: A002
    if action not in ("confirm", "reject"):
        raise ValueError(f"unknown action {action!r}")
    item = next((i for i in p.review_queue if _item_id(i) == item_id), None)
    if item is None:
        raise KeyError(item_id)
    decision = {"ts": datetime.now(timezone.utc).isoformat(), "item_id": item_id, "action": action,
                "company": item.company, "month": item.month, "metric": item.metric,
                "error_type": item.error_type, "source_file": item.source_file,
                "source_cell": item.source_cell}
    if action == "confirm":
        if item.error_type not in CONFIRMABLE:
            raise ValueError(f"{item.error_type} items cannot be confirmed")
        if item.error_type == "unknown_label":
            label = raw_label(p, item)
            if label is None or item.suggestion is None:
                raise ValueError("no label or no suggested metric to learn")
            mapping.learn_alias(label, item.suggestion, cfg.aliases_path)
            decision.update(label=label, learned_metric=item.suggestion)

    decisions = p.decisions + [decision]
    new = p if action == "reject" else _rerun(p, item.company, cfg, decisions)
    out = Portfolio(paths=list(new.paths),
                    files_by_company={c: list(v) for c, v in new.files_by_company.items()},
                    months=list(new.months), table=new.table.copy(), review_queue=list(new.review_queue),
                    metrics=new.metrics.copy(), decisions=decisions, raw_labels=dict(new.raw_labels))
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "decisions.jsonl", "a") as f:
        f.write(json.dumps(decision) + "\n")
    _write_record(cfg, "resolve", p.files_by_company[item.company], out.review_queue, [decision])
    return out
