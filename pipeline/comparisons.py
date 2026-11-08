"""Comparisons in board commentary are facts, so code computes or verifies them (never Claude).

Two parts, both pure functions over ``values`` ({metric_id: value or None}; ids look like
``slug.period.metric`` with period ``YYYY-MM`` or ``12m``):

* Comparison tokens Claude may write instead of its own words:
  ``{id|rank}`` -> the figure plus its place among companies with a checked value;
  ``{id|vs:other}`` -> the figure plus 'up from'/'down from' (same company, earlier period) or
  'above'/'below' (another company, same period). Direction and rank come from the values as
  displayed, so a reader never sees 'up from' between two equal-looking figures; a shared place
  reads 'tied for ...', and equal-looking but different figures read 'in line with', never
  'unchanged'.
* A claim check for the comparison words Claude writes itself. A superlative (highest, lowest,
  fastest, leads, trails, best, worst, ...) must hold for a figure referenced in the same sentence,
  across the portfolio or, when the sentence is about time ('since', 'of the year', ...), across
  that company's periods. A direction word between two figures of the same metric (rose/fell
  between periods of one company; above/below between companies) must match their order.
  Sentences with a superlative but no figure to check are rejected: the claim is unverifiable.

Qualitative words (steady, healthy, worth watching) are not checked: they describe, they do not
order. Nothing here reads files or calls Claude.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

LOWER_IS_BETTER = {"churn_rate"}

_HIGH = {"highest", "fastest", "largest", "biggest", "strongest*", "best*", "leads", "led",
         "leader", "tops"}
_LOW = {"lowest", "slowest", "smallest", "weakest*", "worst*", "trails", "laggard"}
# * = direction depends on whether a higher value is better for the metric
_SUPERLATIVE_RE = re.compile(
    r"\b(" + "|".join(sorted({w.rstrip("*") for w in _HIGH | _LOW}, key=len, reverse=True)) + r")\b")
_NOT_A_RANKING = re.compile(r"\bled (?:to|by)\b")          # causal 'led to', passive 'led by'
_TIME_SCOPE = re.compile(r"\b(since|on record|of the year|this year|in a year|in months|for months|"
                         r"over the period|over the year|ever|in the period|recent months)\b")

_RISE = {"rose", "risen", "rising", "increased", "increasing", "up", "higher", "grew", "improved",
         "climbed", "accelerated", "above", "ahead", "exceeds", "exceeded", "outpaced", "more"}
_FALL = {"fell", "fallen", "falling", "decreased", "decreasing", "declined", "declining", "down",
         "lower", "slowed", "dropped", "eased", "below", "behind", "less", "trailed"}
_WORD_RE = re.compile(r"[a-z]+")
_NUM_WORDS = "zero one two three four five six seven eight nine ten eleven twelve".split()
_ORDINALS = "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth".split()


class BadToken(ValueError):
    """A comparison token that is malformed or compares unlike figures."""


# --------------------------------------------------------------------------- ids and display
def split_id(metric_id: str) -> tuple[str, str, str]:
    slug, period, name = metric_id.split(".")
    return slug, period, name


def _shown(metric_id: str, value: float, fmt: Callable[[str, float], str]) -> float:
    """The value as a reader sees it (rounded the way it is displayed)."""
    return float(re.sub(r"[^\d.\-]", "", fmt(metric_id, value)) or 0)


def peers(metric_id: str, values: dict) -> list[str]:
    """All ids with the same period and metric name, one per company (including blocked ones)."""
    _, period, name = split_id(metric_id)
    return [i for i in values if i.count(".") == 2 and split_id(i)[1:] == (period, name)]


def _count(n: int) -> str:
    return _NUM_WORDS[n] if n < len(_NUM_WORDS) else str(n)


def portfolio_rank(metric_id: str, values: dict, fmt) -> Optional[dict]:
    """Position from the top among companies with a checked value (ties share a place)."""
    v = values.get(metric_id)
    if v is None:
        return None
    checked = [values[i] for i in peers(metric_id, values) if values[i] is not None]
    me = _shown(metric_id, float(v), fmt)
    shown = [_shown(metric_id, float(x), fmt) for x in checked]
    return {"position_from_top": 1 + sum(1 for x in shown if x > me),
            "position_from_bottom": 1 + sum(1 for x in shown if x < me),
            "tied_with": sum(1 for x in shown if x == me) - 1,     # others that read the same
            "companies_with_checked_value": len(checked),
            "companies": len(peers(metric_id, values))}


def _rank_phrase(r: dict) -> str:
    n, total = r["companies_with_checked_value"], r["companies"]
    if n < 2:
        return "the only company with a checked figure"
    scope = f"of the {_count(n)} companies" + ("" if n == total else " with a checked figure")
    top, bottom = r["position_from_top"], r["position_from_bottom"]
    if top == 1 and bottom == 1:
        return f"level with every company {scope}"
    tied = "tied for " if r.get("tied_with") else ""     # a shared place is never told as sole
    if top == 1:
        return f"{tied}the highest {scope}"
    if bottom == 1:
        return f"{tied}the lowest {scope}"
    if top <= bottom:
        return f"{tied}the {_ORDINALS[top - 1]}-highest {scope}"
    return f"{tied}the {_ORDINALS[bottom - 1]}-lowest {scope}"


# --------------------------------------------------------------------------- tokens
def parse_token(token: str) -> tuple[str, list[str]]:
    """'id' -> ('plain', [id]); 'id|rank' -> ('rank', [id]); 'id|vs:other' -> ('vs', [id, other])."""
    if "|" not in token:
        return "plain", [token]
    base, mod = token.split("|", 1)
    if mod == "rank":
        return "rank", [base]
    if mod.startswith("vs:"):
        return "vs", [base, mod[3:]]
    raise BadToken(token)


def check_vs(a: str, b: str) -> None:
    """Same metric; same company at an earlier period, or another company at the same period."""
    try:
        (sa, pa, na), (sb, pb, nb) = split_id(a), split_id(b)
    except ValueError:
        raise BadToken(f"{a}|vs:{b}")
    same_company_earlier = sa == sb and "12m" not in (pa, pb) and pb < pa
    other_company_same_period = sa != sb and pa == pb
    if na != nb or not (same_company_earlier or other_company_same_period):
        raise BadToken(f"{a}|vs:{b}")


def month_label(period: str) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if not m:
        return period
    names = ("January February March April May June July August September October November "
             "December").split()
    return f"{names[int(m.group(2)) - 1]} {m.group(1)}"


def render_token(kind: str, ids: list[str], values: dict, fmt, company_name: Callable[[str], str]) -> str:
    a = ids[0]
    shown_a = fmt(a, float(values[a]))
    if kind == "plain":
        return shown_a
    if kind == "rank":
        return f"{shown_a}, {_rank_phrase(portfolio_rank(a, values, fmt))}"
    b = ids[1]
    shown_b = fmt(b, float(values[b]))
    x, y = _shown(a, float(values[a]), fmt), _shown(b, float(values[b]), fmt)
    (sa, _, _), (sb, pb, _) = split_id(a), split_id(b)
    if sa == sb:
        # equal as displayed but not in fact: 'unchanged' would overstate it
        word = "up from" if x > y else "down from" if x < y else \
            "unchanged from" if float(values[a]) == float(values[b]) else "in line with"
        return f"{shown_a}, {word} {shown_b} in {month_label(pb)}"
    word = "above" if x > y else "below" if x < y else "level with"
    return f"{shown_a}, {word} {company_name(sb)}'s {shown_b}"


# --------------------------------------------------------------------------- claim check
def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _extreme(metric_id: str, pool: list[str], values: dict, fmt, want: str) -> bool:
    checked = [i for i in pool if values.get(i) is not None]
    if len(checked) < 2:
        return False
    me = _shown(metric_id, float(values[metric_id]), fmt)
    others = [_shown(i, float(values[i]), fmt) for i in checked if i != metric_id]
    # strictly: a figure that reads the same as another is tied, not 'the highest'/'the lowest'
    return all(me > o for o in others) if want == "max" else all(me < o for o in others)


def _direction(word: str, metric: str) -> str:
    if word.rstrip("*") in {"best", "strongest"}:
        return "min" if metric in LOWER_IS_BETTER else "max"
    if word.rstrip("*") in {"worst", "weakest"}:
        return "max" if metric in LOWER_IS_BETTER else "min"
    return "max" if word in {w.rstrip("*") for w in _HIGH} else "min"


def unsupported_claim(sentence_text: str, refs: list[str], values: dict, fmt) -> Optional[str]:
    """None if every checkable comparison in the sentence holds, else a short description.

    ``sentence_text`` is the sentence with all tokens removed; ``refs`` are its plain references
    (rank and vs tokens are computed, so they need no check)."""
    text = _NOT_A_RANKING.sub(" ", sentence_text.lower())
    supers = _SUPERLATIVE_RE.findall(text)
    over_time = bool(_TIME_SCOPE.search(text))
    for word in supers:
        if not refs:
            return f"'{word}' with no figure to check"
        ok = False
        for r in refs:
            slug, period, name = split_id(r)
            want = _direction(word, name)
            pool = ([i for i in values if i.count(".") == 2 and split_id(i)[0] == slug
                     and split_id(i)[2] == name and split_id(i)[1] != "12m"]
                    if over_time else peers(r, values))
            if _extreme(r, pool, values, fmt, want):
                ok = True
                break
        if not ok:
            return f"'{word}' is not true for any figure in the sentence"

    words = _words(text)
    rise, fall = bool(words & _RISE), bool(words & _FALL)
    if rise == fall:            # no direction word, or both (e.g. 'rose, then fell'): nothing to check
        return None
    by_metric: dict[str, list[str]] = {}
    for r in refs:
        by_metric.setdefault(split_id(r)[2], []).append(r)
    for name, ids in by_metric.items():
        if len(ids) != 2:
            continue
        a, b = ids                                  # in reading order
        (sa, pa, _), (sb, pb, _) = split_id(a), split_id(b)
        if sa == sb and pa != pb and "12m" not in (pa, pb):
            later, earlier = (a, b) if pa > pb else (b, a)
            x, y = _shown(later, float(values[later]), fmt), _shown(earlier, float(values[earlier]), fmt)
        elif sa != sb and pa == pb:
            x, y = _shown(a, float(values[a]), fmt), _shown(b, float(values[b]), fmt)
        else:
            continue
        if (rise and not x > y) or (fall and not x < y):
            return f"direction word does not match {a} vs {b}"
    return None
