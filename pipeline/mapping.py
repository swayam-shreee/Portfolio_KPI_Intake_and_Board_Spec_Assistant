"""Label matching: aliases.yaml exact lookup, then rapidfuzz. No Claude layer yet.

An unknown label returns ``metric=None`` plus (when one is close enough) a
``suggestion``. Suggestions are never used as values; the caller sends them to review.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from rapidfuzz import fuzz, process

from pipeline.contract import METRICS

ALIASES_PATH = Path(__file__).with_name("aliases.yaml")
AUTO_ACCEPT = 90.0   # fuzz.ratio at or above this: treated as a typo of a known alias
SUGGEST_MIN = 75.0   # token-sort ratio at or above this: offered as a suggestion only

GENERIC = frozenset({"arr"})
_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NOISE = re.compile(r"[^a-z0-9 ]+")


@dataclass(frozen=True)
class Match:
    metric: Optional[str]
    method: str                      # "alias" | "fuzzy" | "none"
    score: float = 100.0
    suggestion: Optional[str] = None


def normalize_label(label: str) -> str:
    s = _PAREN.sub(" ", str(label).lower())
    s = s.replace("%", " pct ").replace("&", " and ")
    s = _NOISE.sub(" ", s)
    s = re.sub(r"\busd\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=4)
def load_aliases(path: str = str(ALIASES_PATH)) -> dict[str, str]:
    """normalized alias -> metric"""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    out: dict[str, str] = {}
    for metric, names in raw.items():
        for n in names or []:
            out[normalize_label(n)] = metric
    return out


def match_label(label: str, aliases_path=None) -> Match:
    norm = normalize_label(label)
    # "gross margin %" normalizes to "gross margin pct"; keep both spellings usable
    norm_nopct = re.sub(r"\bpct\b", "", norm).strip()
    aliases = load_aliases() if aliases_path is None else load_aliases(str(aliases_path))
    for cand in (norm, norm_nopct):
        if cand in aliases:
            return Match(aliases[cand], "alias")
    if not norm_nopct:
        return Match(None, "none", 0.0)
    keys = [k for k in aliases if k not in GENERIC]   # a bare "arr" would swallow every ARR-ish label
    best = process.extractOne(norm_nopct, keys, scorer=fuzz.ratio)
    if best and best[1] >= AUTO_ACCEPT:
        return Match(aliases[best[0]], "fuzzy", float(best[1]))
    best = process.extractOne(norm_nopct, keys, scorer=fuzz.token_sort_ratio)
    if best and best[1] >= SUGGEST_MIN:
        return Match(None, "none", float(best[1]), suggestion=aliases[best[0]])
    return Match(None, "none", float(best[1]) if best else 0.0)


# --------------------------------------------------------------------------- alias learning
_LINES = re.compile(r"[^\n]*\n|[^\n]+")


def learn_alias(label: str, metric: str, aliases_path: Path) -> None:
    """Add one quoted list line for ``label`` under ``metric`` in the YAML file at ``aliases_path``.

    Every existing line is kept as is: the new line goes directly after the last entry of the
    metric's block (a missing block is created at the end). Nothing is written when the request
    is refused (ValueError) or when the label is already an alias of that metric.
    """
    path = Path(aliases_path)
    if metric not in METRICS:
        raise ValueError(f"{metric!r} is not a metric")
    if not isinstance(label, str) or looks_like_instruction(label):
        raise ValueError("label is empty or looks like an instruction")
    norm = normalize_label(label)
    bare = re.sub(r"\bpct\b", "", norm).strip()
    if not bare or norm in GENERIC or bare in GENERIC:
        raise ValueError(f"label {label!r} is empty or too generic to learn")
    original = path.read_bytes()
    text = original.decode("utf-8")
    data = yaml.safe_load(text) or {}
    for other, names in data.items():
        for n in names or []:
            if normalize_label(n) == norm:
                if other == metric:
                    return
                raise ValueError(f"label {label!r} already maps to {other}")
    quoted = json.dumps(label, ensure_ascii=False)        # a JSON string is a YAML double-quoted scalar
    lines = _LINES.findall(text)
    header = re.compile(rf"{re.escape(metric)}:[ \t]*(#.*)?\r?\n?")
    start = next((i for i, l in enumerate(lines) if header.fullmatch(l)), None)
    if start is None:
        if metric in data:
            raise ValueError(f"cannot edit the {metric} block in place")
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{metric}:\n")
        lines.append(f"  - {quoted}\n")
    else:
        end = next((k for k in range(start + 1, len(lines))
                    if lines[k][:1] not in (" ", "\t", "#", "\n", "\r", "-")), len(lines))
        last = max((k for k in range(start + 1, end) if lines[k].lstrip().startswith("- ")), default=start)
        eol = "\r\n" if lines[last].endswith("\r\n") else "\n"
        indent = lines[last][:len(lines[last]) - len(lines[last].lstrip())] if last > start else "  "
        newline = f"{indent}- {quoted}"
        if lines[last].endswith("\n"):
            newline += eol
        else:                                              # last line of a file with no trailing newline
            lines[last] += eol
        lines.insert(last + 1, newline)
    new_text = "".join(lines)
    path.write_bytes(new_text.encode("utf-8"))
    try:
        back = yaml.safe_load(new_text)
        ok = back[metric][-1] == label and {k: v for k, v in back.items() if k != metric} == \
            {k: v for k, v in data.items() if k != metric}
    except Exception:
        ok = False
    if not ok:
        path.write_bytes(original)
        raise ValueError(f"label {label!r} could not be written safely")
    load_aliases.cache_clear()


# --------------------------------------------------------------------------- canonical records
MONEY = ("beginning_arr", "new_arr", "expansion_arr", "contraction_arr", "churned_arr",
         "ending_arr", "revenue")
_INJECTION_CUES = re.compile(
    r"ignore (all |any )?(previous|prior|above|earlier)|disregard|system prompt|"
    r"you are (now )?an? |act as |new instructions|override|do not flag|instead report",
    re.I)


def looks_like_instruction(text: str) -> bool:
    return _INJECTION_CUES.search(text) is not None


def to_records(fd, aliases_path=None) -> tuple[list[dict], list]:
    """Map a FileData's series to canonical rows (value in $K / percent) and review items."""
    from pipeline.contract import ReviewItem
    recs: list[dict] = []
    items: list = []
    seen: set = set()
    for s in fd.series:
        m = match_label(s.label, aliases_path)
        if m.metric is None:
            quoted = "" if looks_like_instruction(s.label) else f" '{s.label[:60]}'"
            note = f"; closest known metric is {m.suggestion}" if m.suggestion else ""
            items.append(ReviewItem(
                company=fd.company or fd.source_file, month=None, metric=None,
                error_type="unknown_label", source_file=fd.source_file, source_cell=s.label_cell,
                reason=f"Label{quoted} is not a known metric name{note}; its values were left out.",
                suggestion=m.suggestion))
            continue
        key = (s.label_cell.rpartition("!")[0], m.metric)   # per sheet: tabs repeat labels
        if key in seen:            # two rows for one metric on a sheet: keep the first, never merge
            continue
        seen.add(key)
        factor = s.unit_factor if s.unit_factor is not None else (
            s.sheet_unit if s.sheet_unit is not None else 1.0)
        pct_scale = 1.0
        if m.metric == "gross_margin_pct":
            pct_scale = 100.0 if statistics.median(abs(v) for v, _ in s.values.values()) <= 1.0 else 1.0
        step_k = (10.0 ** -s.decimals) * factor if m.metric in MONEY else 0.0
        for month, (x, cell) in s.values.items():
            if m.metric in ("contraction_arr", "churned_arr"):
                x = abs(x)
            v = x * factor if m.metric in MONEY else x * pct_scale
            recs.append({"company": fd.company, "month": month, "metric": m.metric,
                         "value_usd_k": round(v, 6), "source_file": fd.source_file,
                         "source_cell": cell, "file_ts": fd.submitted, "derived": False,
                         "step_k": step_k})
    return recs, items
