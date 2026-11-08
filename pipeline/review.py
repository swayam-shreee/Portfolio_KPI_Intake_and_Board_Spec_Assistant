"""Review loop: decisions on review items, alias learning, per-company reruns, recorded label guesses.

No Streamlit and no Claude here (guesses arrive as plain values from ``label_match``, which owns the call). ``decisions.jsonl`` in the state dir is the source of truth;
a ``Portfolio`` is a cache of "run the pipeline with those decisions applied". All state is
immutable from the caller's view: ``resolve`` and ``rerun_company`` return a new Portfolio.
"""
from __future__ import annotations

import glob
import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline import ingest, mapping, metrics, run
from pipeline.contract import METRIC_COLUMNS, METRICS, ReviewItem

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
    label_samples: dict = field(default_factory=dict)   # item_id -> first <=6 raw numbers under the label
    label_guesses: dict = field(default_factory=dict)   # item_id -> {"metric", "confidence", "model"}


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


def _undo_key(d: dict) -> tuple:
    return (d["item_id"], d["ts"])


def effective(decisions: list) -> list:
    """The decisions still in force: undo lines and every decision an undo line points at are dropped."""
    undone = {(d["item_id"], d.get("undoes_ts")) for d in decisions if d["action"] == "undo"}
    return [d for d in decisions if d["action"] != "undo" and _undo_key(d) not in undone]


def latest_decisions(decisions: list) -> dict:
    """item_id -> that item's latest decision still in force."""
    out: dict = {}
    for d in effective(decisions):
        out[d["item_id"]] = d
    return out


def _accepted(decisions: list) -> frozenset:
    return frozenset((d["company"], d["month"], d["metric"]) for d in effective(decisions)
                     if d["action"] == "confirm" and d["error_type"] == "impossible_value")


def _corrections(decisions: list) -> dict:
    """{(company, month, metric, source_file, source_cell): value_usd_k} from corrections in force."""
    return {(d["company"], d["month"], d["metric"], d["source_file"], d["source_cell"]):
            float(d["corrected_value_usd_k"])
            for d in effective(decisions) if d["action"] == "correct"}


def proposed_value(item: ReviewItem) -> float | None:
    """The code-proposed corrected value ($K) of an impossible_value item, or None."""
    if item.error_type != "impossible_value" or not item.suggestion:
        return None
    try:
        return float(item.suggestion)
    except ValueError:
        return None


def _closed(decisions: list) -> frozenset:
    return frozenset(d["item_id"] for d in effective(decisions)
                     if d["action"] == "confirm" and d["error_type"] in CLOSED_AT_INGEST)


def _open_items(queue: list, decisions: list) -> list:
    closed = _closed(decisions)
    return [i for i in queue if item_id(i) not in closed]


def _write_json_record(cfg: Config, kind: str, record: dict) -> None:
    runs = cfg.state_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    seq = sum(1 for _ in runs.iterdir())
    while True:
        target = runs / f"{time.time_ns() // 1000}-{seq}-{kind}.json"
        try:
            with open(target, "x") as f:
                json.dump(record, f, indent=1)
            return
        except FileExistsError:
            seq += 1


def _write_record(cfg: Config, kind: str, paths: list, queue: list, decisions: list) -> None:
    _write_json_record(cfg, kind, {
        "kind": kind,
        "inputs": [{"file": Path(x).name, "sha256": _sha256(x)} for x in paths],
        "open_item_ids": [item_id(i) for i in queue],
        "decisions": decisions})


def _read_guesses(cfg: Config) -> dict:
    """Replay ``label_guesses.jsonl`` in file order; the last line per item wins. Bad lines are skipped."""
    f = cfg.state_dir / "label_guesses.jsonl"
    out: dict = {}
    if not f.exists():
        return out
    for line in f.read_text().splitlines():
        try:
            d = json.loads(line)
            iid, metric, conf = d["item_id"], d["metric"], d["confidence"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(iid, str) and (metric is None or metric in METRICS):
            out[iid] = {"metric": metric, "confidence": conf, "model": d.get("model")}
    return out


def _apply_guesses(queue: list, guesses: dict) -> list:
    """An open unknown_label item whose guess has a metric gets it as its suggestion; a null guess changes nothing."""
    if not guesses:
        return list(queue)
    out = []
    for i in queue:
        g = guesses.get(item_id(i)) if i.error_type == "unknown_label" else None
        out.append(replace(i, suggestion=g["metric"]) if g and g.get("metric") else i)
    return out


def _clone(p: Portfolio, **changes) -> Portfolio:
    """A Portfolio that shares no mutable state with ``p``."""
    base = Portfolio(paths=list(p.paths), files_by_company={c: list(v) for c, v in p.files_by_company.items()},
                     months=list(p.months), table=p.table.copy(), review_queue=list(p.review_queue),
                     metrics=p.metrics.copy(), decisions=list(p.decisions), raw_labels=dict(p.raw_labels),
                     label_samples={k: list(v) for k, v in p.label_samples.items()},
                     label_guesses={k: dict(v) for k, v in p.label_guesses.items()})
    return replace(base, **changes)


def _seed_aliases(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.aliases_path.exists():
        cfg.aliases_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cfg.seed_aliases_path, cfg.aliases_path)
    mapping.load_aliases.cache_clear()


def _replay_aliases(cfg: Config, decisions: list) -> None:
    for d in effective(decisions):
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
    samples_at: dict = {}
    for path in paths:
        fd = ingest.read_file(path)
        files_by_company.setdefault(fd.company or fd.source_file, []).append(path)
        for s in fd.series:
            if not mapping.looks_like_instruction(s.label):
                labels_at[(fd.source_file, s.label_cell)] = s.label
                samples_at[(fd.source_file, s.label_cell)] = [s.values[m][0] for m in sorted(s.values)][:6]

    res = run.run(paths, aliases_path=cfg.aliases_path, accepted=_accepted(decisions),
                  corrections=_corrections(decisions))
    guesses = _read_guesses(cfg)
    queue = _apply_guesses(_open_items(res.review_queue, decisions), guesses)
    months = sorted(res.table["month"].unique())
    raw_labels = {item_id(i): labels_at[(i.source_file, i.source_cell)] for i in queue
                  if i.error_type == "unknown_label" and (i.source_file, i.source_cell) in labels_at}
    label_samples = {iid: samples_at[(i.source_file, i.source_cell)] for i in queue
                     if (iid := item_id(i)) in raw_labels and (i.source_file, i.source_cell) in samples_at}
    table = res.table.reset_index(drop=True)
    p = Portfolio(paths=paths, files_by_company=files_by_company, months=months, table=table,
                  review_queue=queue, metrics=metrics.compute(table, queue, months=months),
                  decisions=decisions, raw_labels=raw_labels, label_samples=label_samples,
                  label_guesses=guesses)
    _write_record(cfg, "load", paths, queue, decisions)
    return p


def _rerun(p: Portfolio, company: str, cfg: Config, decisions: list) -> Portfolio:
    files = p.files_by_company[company]
    res = run.run(files, aliases_path=cfg.aliases_path, accepted=_accepted(decisions),
                  corrections=_corrections(decisions))
    queue = _apply_guesses(_open_items(res.review_queue, decisions), p.label_guesses)
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
                     metrics=merged, decisions=list(decisions), raw_labels=dict(p.raw_labels),
                     label_samples={k: list(v) for k, v in p.label_samples.items()},
                     label_guesses={k: dict(v) for k, v in p.label_guesses.items()})


def rerun_company(p: Portfolio, company: str, cfg: Config) -> Portfolio:
    return _rerun(p, company, cfg, p.decisions)


def _restore_aliases(path, snapshot) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(snapshot)


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
    snapshot = None          # (bytes or None) of the learned aliases file, taken before learn_alias
    learned = False
    if action == "confirm":
        if item.error_type not in CONFIRMABLE:
            raise ValueError(f"{item.error_type} items cannot be confirmed")
        if item.error_type == "unknown_label":
            label = raw_label(p, item)
            if label is None or item.suggestion is None:
                raise ValueError("no label or no suggested metric to learn")
            ap = Path(cfg.aliases_path)
            snapshot = ap.read_bytes() if ap.exists() else None
            learned = True
            try:
                mapping.learn_alias(label, item.suggestion, cfg.aliases_path)
            except BaseException:
                _restore_aliases(ap, snapshot)
                raise
            g = p.label_guesses.get(item_id)
            by_claude = bool(g) and g.get("metric") is not None and g["metric"] == item.suggestion
            decision.update(label=label, learned_metric=item.suggestion,
                            suggested_by="claude" if by_claude else "fuzzy",
                            confidence=g["confidence"] if by_claude else None)

    decisions = p.decisions + [decision]
    try:
        new = p if action == "reject" else _rerun(p, item.company, cfg, decisions)
    except BaseException:
        if learned:
            _restore_aliases(Path(cfg.aliases_path), snapshot)
        raise
    out = _clone(new, decisions=decisions)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "decisions.jsonl", "a") as f:
        f.write(json.dumps(decision) + "\n")
    _write_record(cfg, "resolve", p.files_by_company[item.company], out.review_queue, [decision])
    return out


def correct(p: Portfolio, item_id: str, cfg: Config) -> Portfolio:  # noqa: A002
    """Apply the code-proposed value of an ``impossible_value`` item (a unit slip under which the
    month's ARR bridge ties). A person clicks it; nothing here guesses or types a number.

    Appends ``{"action": "correct", "original_value" (as typed in the file), "corrected_value_usd_k", ...}`` to
    ``decisions.jsonl`` and reruns that company only; ``undo`` reverses it. Raises ``KeyError`` for an
    unknown id and ``ValueError`` when the item has no proposed value; nothing is written then.
    """
    item = next((i for i in p.review_queue if _item_id(i) == item_id), None)
    if item is None:
        raise KeyError(item_id)
    value = proposed_value(item)
    if value is None:
        raise ValueError(f"{item.error_type} item {item_id} has no proposed correction")
    original = original_k = None  # as typed in the file, and as read in $K (the flagged value is out of the table)
    for path in p.files_by_company.get(item.company, []):
        if Path(path).name != item.source_file:
            continue
        for s in ingest.read_file(path).series:
            if item.month in s.values and s.values[item.month][1] == item.source_cell:
                original = s.values[item.month][0]
                original_k = original * (s.unit_factor or s.sheet_unit or 1.0)
    decision = {"ts": datetime.now(timezone.utc).isoformat(), "item_id": item_id, "action": "correct",
                "company": item.company, "month": item.month, "metric": item.metric,
                "error_type": item.error_type, "source_file": item.source_file,
                "source_cell": item.source_cell, "original_value": original,
                "original_value_usd_k": original_k, "corrected_value_usd_k": value}
    decisions = p.decisions + [decision]
    new = _rerun(p, item.company, cfg, decisions)
    out = _clone(new, decisions=decisions)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "decisions.jsonl", "a") as f:
        f.write(json.dumps(decision) + "\n")
    _write_record(cfg, "correct", p.files_by_company[item.company], out.review_queue, [decision])
    return out


def undo(p: Portfolio, item_id: str, cfg: Config) -> Portfolio:  # noqa: A002
    """Undo the latest decision still in force for ``item_id`` by appending an ``undo`` line.

    Nothing is removed from ``decisions.jsonl``. A reject undo changes only ``decisions``. A confirm
    undo reruns the item's company so the item reopens; for ``unknown_label`` the learned aliases
    file is first rebuilt from the seed plus the confirms still in force. Raises ``KeyError`` when the
    item has no decision to undo; nothing is written when anything raises.
    """
    target = latest_decisions(p.decisions).get(item_id)
    if target is None:
        raise KeyError(item_id)
    decision = {k: target.get(k) for k in ("item_id", "company", "month", "metric", "error_type",
                                           "source_file", "source_cell")}
    decision.update(ts=datetime.now(timezone.utc).isoformat(), action="undo", undoes_ts=target["ts"],
                    undoes_action=target["action"])
    decisions = p.decisions + [decision]
    raw_labels = dict(p.raw_labels)
    snapshot = None
    relearn = target["action"] == "confirm" and target["error_type"] == "unknown_label"
    ap = Path(cfg.aliases_path)
    if relearn:
        snapshot = ap.read_bytes() if ap.exists() else None
    try:
        if relearn:
            ap.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cfg.seed_aliases_path, ap)
            mapping.load_aliases.cache_clear()
            _replay_aliases(cfg, decisions)
            mapping.load_aliases.cache_clear()
            if target.get("label"):
                raw_labels[item_id] = target["label"]      # the reopened item needs its label back
        if target["action"] == "reject":
            new = p
        else:
            new = _rerun(_clone(p, raw_labels=raw_labels), target["company"], cfg, decisions)
    except BaseException:
        if relearn:
            _restore_aliases(ap, snapshot)
            mapping.load_aliases.cache_clear()
        raise
    out = _clone(new, decisions=decisions)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "decisions.jsonl", "a") as f:
        f.write(json.dumps(decision) + "\n")
    _write_record(cfg, "undo", p.files_by_company[target["company"]], out.review_queue, [decision])
    return out


# --------------------------------------------------------------------------- recorded label guesses
def record_guesses(p: Portfolio, cfg: Config, guesses: dict, model: str) -> Portfolio:
    """Record ``{item_id: guess}`` (objects with ``metric`` and ``confidence``) and apply them.

    Only ids of open unknown_label items count. A guess with a metric reruns that item's company only;
    a null guess is recorded and changes nothing. The new Portfolio is built first: the store and the run
    record are written only after it succeeded. Returns ``p`` itself when nothing is recorded.
    """
    open_items = {item_id(i): i for i in p.review_queue if i.error_type == "unknown_label"}
    valid = {iid: g for iid, g in guesses.items() if iid in open_items}
    if not valid:
        return p
    stored = {iid: {"metric": g.metric, "confidence": g.confidence, "model": model} for iid, g in valid.items()}
    cur = _clone(p, label_guesses={**{k: dict(v) for k, v in p.label_guesses.items()}, **stored})
    changed = {open_items[iid].company for iid, g in valid.items()
               if g.metric is not None or (p.label_guesses.get(iid) or {}).get("metric") is not None}
    companies = [c for c in p.files_by_company if c in changed]
    for company in companies:
        cur = _rerun(cur, company, cfg, cur.decisions)

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(cfg.state_dir / "label_guesses.jsonl", "a", encoding="utf-8") as f:
        for iid, g in stored.items():
            f.write(json.dumps({"ts": ts, "item_id": iid, "label": raw_label(p, open_items[iid]),
                                "metric": g["metric"], "confidence": g["confidence"], "model": model}) + "\n")
    _write_json_record(cfg, "suggest", {
        "kind": "suggest",
        "inputs": [{"file": Path(x).name, "sha256": _sha256(x)}
                   for c in companies for x in p.files_by_company[c]],
        "open_item_ids": [item_id(i) for i in cur.review_queue],
        "guesses": [{"item_id": iid, "metric": g["metric"], "confidence": g["confidence"]}
                    for iid, g in stored.items()]})
    return cur
