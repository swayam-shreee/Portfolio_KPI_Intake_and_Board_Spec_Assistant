# Stage 6a — Review Loop Checker Spec

Nov 6, 2026. Write and lock the checker in its own session before any build work starts. Copy this doc into the repo as `docs/stage6a_review_loop_checker_spec.md` and commit it together with `tests/test_review_loop_checker.py`. The stage 6 dashboard checker is written and locked later, after 6a is built and merged, in a second checker commit with its own START_HASH.

## Prerequisite

Stages 2–5 are green on main: stage 3 recall 7/7 with Ridgeline at 0 false flags, stage 4 250/250, pytest all passed (77 today), and the repo is pushed.

Numbers in this doc are a dated snapshot ("today: 8 review items, 16 blocked metrics, 24 open questions"). The checker never hardcodes them: every count and expected value comes from live data. Do not copy the "21 blocked" figure from the stage 5 spec; it is stale.

## Why this stage exists

The dashboard promises that resolving a review item reruns checks and metrics for that company only. The pipeline has none of the pieces for that: no decision store, no per-company rerun, no alias learning, no "accept as reported". They are pure pipeline logic, testable without Streamlit, and CLAUDE.md says to keep processing out of the pages. So they get their own locked stage first.

Not in this stage: the Claude label-matching layer (stage 7, with its own checker), correcting a value by hand, approving a draft, the reporting-month picker.

## Design decisions

| # | Decision | Rule |
|---|---|---|
| D1 | Where decisions live | `state_dir/decisions.jsonl`, append-only, is the source of truth and the audit trail. `load` replays its `confirm` lines in file order. `reject` lines are audit only and never change data. A UI may keep the `Portfolio` in session state, but only as a cache. `Portfolio.decisions` is the full ordered list of the parsed lines. |
| D2 | Alias learning | `learn_alias(label, metric, aliases_path)` inserts **exactly one new list line** directly after the last entry of that metric's block (creating the block at the end of the file if the metric has none). Every original line stays, unchanged and in order, so comments and ordering survive; the file is never re-serialized with `yaml.safe_dump`. The label is **always** written as a double-quoted YAML scalar (labels such as `&Growth ARR`, `*x`, `% x`, `@x`, `yes`, `null` and `1e3` break any narrower quoting rule), and after the write `yaml.safe_load` must return the exact label under that metric; otherwise the file is restored byte for byte and `ValueError` is raised. Insertion is directly after the last list entry of the block, not after the header, trailing blank lines or comments. It raises `ValueError`, writing nothing, for: a `looks_like_instruction` label, a label whose normalized form is empty or in `GENERIC`, a label whose normalized form already maps to a **different** metric, or a metric that is not in `METRICS`. A label already present for that metric is a no-op. After a write it calls `load_aliases.cache_clear()`. It never touches any other path. |
| D2b | Alias file location | `Config.default().aliases_path = ROOT/outputs/state/aliases.yaml`. On `load`, if `aliases_path` does not exist it is seeded with a byte copy of `cfg.seed_aliases_path` (default `pipeline/aliases.yaml`); an existing file is never overwritten. The tracked `pipeline/aliases.yaml` is only ever read. Promoting learned aliases into it is a manual, reviewed change this stage does not cover. Reason: writing the tracked file would dirty git, drop stage 3 recall to 6/7 and break stage 5 test 6. |
| D3 | Actions | `confirm` is valid for four error types. `unknown_label`: learn the alias (`ValueError` if the item has no suggestion). `impossible_value`: accept the value as reported. `duplicate_submission`: keep the newer value, close the item. `prompt_injection`: close the item and take no value from the cell. `missing_month`, `rollforward_mismatch` and `continuity_break` raise `ValueError` for confirm: accepting a rollforward value just makes the next month fail and creates a new item. `reject` is valid for every open item, logs only, and the item stays open as a question. An unknown or already-closed item ID raises `KeyError`; any other action name raises `ValueError`. A failed call writes nothing. |
| D4 | Rerun scope | `resolve` reruns only that item's company, from `p.files_by_company[company]` (Northpass has two files), and splices the result into the portfolio. Other companies' table, queue and metrics rows are carried over unchanged. `resolve` and `rerun_company` return a **new** `Portfolio` and never mutate their input. The spliced table and metrics keep the pipeline's canonical row order (table by company, month, metric; metrics by company, month, metric_id), so a rerun is indistinguishable from a full run. |
| D5 | Defaults unchanged | `run(paths)`, `run_checks(df, files)`, `match_label(label)`, `to_records(fd)` and `metrics.compute(table, queue)` behave exactly as today when the new keyword arguments are not given. |
| D6 | Item identity | `item_id(it)` = first 12 hex characters of sha1 over `company\|month\|metric\|error_type\|source_file\|source_cell` (`None` as an empty string). It never includes `reason` or `suggestion`, and never an index. |
| D7 | Run audit | Each `load` and each `resolve` writes exactly one record `state_dir/runs/<epoch µs>-<seq>-<load\|resolve>.json`, created in `"x"` mode. It holds the input file names and sha256s, the open item IDs and the decisions applied. It never holds `reason` text or cell text. `resolve` does not call `load`, so it writes one record, not two. |
| D8 | No reimplementation | `review.load` and `rerun_company` call `run.run(paths, aliases_path=..., accepted=...)` and then `metrics.compute(..., months=...)`, both through their module (`run.run`, `metrics.compute`). `review` never rebuilds the body of `run()`. |
| D9 | Accepted keys and records | `accepted` is a `frozenset[(company, month, metric)]`. `checks.run_checks(df, files, accepted=frozenset())` skips flagging those keys. Closed ingest-level items (duplicate, injection, unknown_label) are filtered out of the queue by `review`, by item ID. Each `decisions.jsonl` line stores `ts, item_id, action, company, month, metric, error_type, source_file, source_cell`, plus `label` and `learned_metric` for an alias decision. It never stores `reason`. Replay rebuilds the accepted keys and closed IDs from these fields, in file order, and calls `learn_alias` again (a no-op if the line exists). `load(cfg, paths=subset)` tolerates decisions about files that are not in `subset`: they match no loaded item and are ignored, and `load` never raises for them. |
| D10 | Period | `Portfolio.months` is the full portfolio's sorted months from the first load and never changes. Every compute, full or per company, passes `months=p.months`. Without it a Harborview-only compute loses 2026-03 and gets the wrong prior month. |
| D11 | Scorer isolation | No new code or comment in `pipeline/` contains `eval/`, `from eval`, `import eval`, `answer_key` or `ground_truth`. |
| D12 | Input listing | `load(cfg)` with `paths=None` reads `sorted(glob(data_dir/*.xlsx) + glob(data_dir/*.csv))`, the same listing the other checkers use. |

## Interface the checker tests

```python
# pipeline/run.py
def run(paths, *, aliases_path=None, accepted: frozenset = frozenset()) -> RunResult
# pipeline/checks.py
def run_checks(df, files, accepted: frozenset = frozenset()) -> tuple[pd.DataFrame, list[ReviewItem]]
# pipeline/mapping.py
def match_label(label, aliases_path=None) -> Match
def to_records(fd, aliases_path=None) -> tuple[list[dict], list]
def learn_alias(label: str, metric: str, aliases_path: Path) -> None
# pipeline/metrics.py
def compute(table, review_queue, months: list[str] | None = None) -> pd.DataFrame   # compute_metrics wrapper unchanged
# pipeline/review.py
@dataclass(frozen=True)
class Config:
    data_dir: Path; state_dir: Path; aliases_path: Path; seed_aliases_path: Path
    @classmethod
    def default(cls) -> "Config"   # data/raw, outputs/state, outputs/state/aliases.yaml, pipeline/aliases.yaml
@dataclass
class Portfolio:
    paths: list[Path]; files_by_company: dict[str, list[Path]]; months: list[str]
    table: pd.DataFrame; review_queue: list[ReviewItem]; metrics: pd.DataFrame
    decisions: list[dict]; raw_labels: dict[str, str]        # item_id -> label, cached at load
def item_id(item: ReviewItem) -> str
def raw_label(p: Portfolio, item: ReviewItem) -> str | None   # unknown_label only; None if instruction-like; no file reads
def load(cfg: Config, paths: list[Path] | None = None) -> Portfolio
def rerun_company(p: Portfolio, company: str, cfg: Config) -> Portfolio
def resolve(p: Portfolio, item_id: str, action: str, cfg: Config) -> Portfolio   # "confirm" | "reject"
```

The checker calls `ingest.read_file`, `checks.run_checks`, `metrics.compute` and `run.run` through their modules (spies), and `Config.default()` without letting it create files.

## Fixtures

There are no fixture files. Everything is built under `tmp_path`:

- `cfg`: `Config(data_dir=data/raw, state_dir=tmp/state, aliases_path=tmp/state/aliases.yaml, seed_aliases_path=pipeline/aliases.yaml)`.
- Module-wide network block: `socket.socket.connect` and `connect_ex` refuse every family except AF_UNIX (AppTest and asyncio need it later); `create_connection`, `getaddrinfo` and `anthropic.Anthropic.__init__` raise.
- Real-file guard (function scope, runs after every test): the bytes of `pipeline/aliases.yaml` and every `data/raw/*` file are snapshotted once. After each test the guard restores changed bytes and deletes anything added to `data/raw/` **first**, in a `finally`, and only then fails the test if anything was touched, so a buggy build cannot poison later tests. The same guard fails any test during which a refused network attempt was recorded, so a build that catches and swallows the error is still caught.
- Test 19 builds a tmp CSV: a copy of the Verity file with the `Net New Logos ARR (USD)` header renamed to a label far from any alias. It first asserts the result is an `unknown_label` item with `suggestion=None`.
- Independent expectations (test 4) come from `run(paths, aliases_path=<tmp yaml built with yaml.safe_dump in the test>)` plus `metrics.compute(..., months=)`, never from `pipeline.review` or `learn_alias`.
- `pipeline.review` is imported lazily inside each test or fixture, so every test fails on its own before the build. Nothing in the file is conditional, and none of its tests can be marked skipped or expected-to-fail.

## Checker tests (`tests/test_review_loop_checker.py`, locked; 26 test functions, 41 cases after parametrization)

1. **Baseline equals today.** `load(cfg)` with no decisions gives the same table, queue and metrics as `run(paths)` plus `compute_metrics()`. The seeded aliases copy is byte-equal to the real file. Every input file belongs to exactly one company. `months` and `decisions` have their baseline values.
2. **item_id.** IDs are 12 hex characters, unique across the queue and reproducible across two loads. Replacing `reason` or `suggestion` keeps the ID; changing any one of the six identity fields changes it. After `resolve(E7)` the queue's IDs are exactly the old set minus E7.
3. **Per-company rerun equivalence.** For each of the five companies, `rerun_company` gives table, queue and metrics rows equal to the full run's rows for that company, every other company is unchanged, and the spliced table and metrics are in canonical row order. Harborview catches the period bug. A rerun writes no decision.
4. **Confirming E7 unblocks Verity.** The learned-alias copy gains the label under `new_arr`. The set of metric IDs that went from blocked to non-blank equals what an independent alias-aware run unblocks. It includes `verity.12m.growth_12m`, `verity.12m.nrr_12m`, `verity.12m.grr_12m` and the first month's `verity.<month>.churn_rate`, all of them Verity's. Values equal the independent values. As non-vacuity bands, growth is between 30 and 50 and nrr between 100 and 120. Verity then has the highest growth_12m of the five companies. The E7 item is gone, the blocked count drops, and the other companies' rows are equal. The spliced frames are in canonical row order.
5. **Company scope.** Spies on `ingest.read_file`, `checks.run_checks` and `metrics.compute` during `resolve(E7)` see only Verity's files and Verity's rows, and are each called at least once.
6. **Persistence, audit and reject.** A confirm writes one `decisions.jsonl` line with every D9 field, the right `label` and `learned_metric`, and no `reason`; `Portfolio.decisions` equals the file. A fresh `load` on the same state dir reproduces the post-decision state and its `decisions` equal the file. A `reject` changes no data, keeps the item open, learns no alias, appends one `reject` line, and survives a reload. Rejecting every open item writes one line per call.
7. **Accept as reported.** Confirming Verity's `impossible_value` adds exactly one row to the table (its own `source_file` and `source_cell`) and removes exactly one item from the queue, and `verity.<month>.gross_margin` equals the table value. The item closes, other companies are unchanged, and a fresh load gives the same state.
8. **Duplicate confirm.** The Northpass item closes. The row for that company-month-metric is unchanged (still from the newer file), Northpass's table is unchanged, and the queue's IDs are the old set minus that item. Northpass then has no open items and no blocked metrics. A fresh `load` gives the same state (the item stays closed).
9. **Injection confirm.** The planted cell's text is read from the source file. The item closes, no table row cites that cell, every other company is unchanged, and the item's own company keeps its table, its metrics and all of its other review items (Harborview's missing month and rollforward items must survive). The queue's IDs are the old set minus that item, and a fresh `load` gives the same state. The text appears nowhere in the table, the queue, `decisions.jsonl` or any run record.
10. **Invalid actions.** Confirm raises `ValueError` on the missing_month item (Harborview), the rollforward_mismatch item (Harborview) and the continuity_break item (Cobalt). An unknown ID raises `KeyError`, an unknown action name raises `ValueError`, and confirming an already-closed item raises `KeyError`. After each failure the state dir (decisions, runs, aliases) is byte-identical. Rejecting a missing_month item works.
11. **Cache and path isolation.** Before the confirm, E7's raw label maps to no metric in the copy. After it, it maps to `new_arr` in the copy only. A second fresh copy and the default `match_label(raw)` still return no metric.
12. **Real files untouched.** After a load and a confirm, `pipeline/aliases.yaml` and every `data/raw/*` file are byte-identical and the `data/raw` listing is unchanged, and a default `run(paths)` still flags E7.
13. **Run records.** `load` writes one record matching `^\d+-\d+-load\.json$`, containing the input sha256s and every open item ID and none of their reason text. `resolve` writes exactly one more, matching `-resolve.json`, containing the item ID and action and none of the queue's reason text. Twenty back-to-back loads leave 22 distinct, correctly named files.
14. **raw_label.** For E7 it equals the CSV header cell at the item's `source_cell` (`Net New Logos ARR (USD)`) and equals `p.raw_labels[item_id]`. It is None for every item that is not `unknown_label`. It works with every file-reading path made to raise: `ingest.read_file`, `ingest._read_grids`, `openpyxl.load_workbook` (also as `review.load_workbook`), `pandas.read_csv` / `read_excel`, `builtins.open`, `io.open`, and `Path.open` / `read_text` / `read_bytes`.
15. **Static scan.** `pipeline/review.py` contains none of `eval/`, `answer_key`, `ground_truth`, `from eval`, `import eval`, `streamlit`, `lru_cache`. No file in `pipeline/` contains any of the first five.
16. **Default config and seeding.** `Config.default()` creates no file or folder (the `outputs/` tree is identical before and after) and points at `outputs/state` and `outputs/state/aliases.yaml` (not `pipeline/aliases.yaml`), with the seed path at `pipeline/aliases.yaml`. A missing aliases file is seeded with exact bytes on `load`; an existing learned file is never overwritten by a later `load`.
17. **Idempotent and pure.** `resolve(p0, e7, "confirm")` called twice on the same pre-state gives equal portfolios, the aliases file gains exactly one line in total, and `p0` (table, metrics, queue, decisions, months, labels, `files_by_company`, `paths`) is unchanged. The results are new objects.
18. **Period.** `p.months` equals the baseline table's sorted months and is unchanged by rerun and resolve. Harborview's own table has fewer months (the reason the test is meaningful), and `metrics.compute` receives the portfolio's months during a Harborview rerun.
19. **learn_alias text safety.** (a) For twelve tricky labels (`:`, `#`, `"`, `'`, padding, `&`, `%`, `*`, `@`, `yes`, `null`, `1e3`): exactly one line is inserted, directly after the `new_arr` block's last list entry and written as a double-quoted scalar, with every other line unchanged and in order, `safe_load` returns the exact label, no other metric's list changes, `match_label` maps the label to `new_arr`, and a second call is a no-op. (b) Empty, whitespace-only, units-only, generic (`ARR`), instruction-like, cross-metric (`Upsell` → `new_arr`) and unknown-metric requests all raise `ValueError` and leave the file byte-identical; a unit/case variant of an existing alias is a no-op. (c) A tmp CSV whose unknown label has no suggestion makes confirm raise `ValueError` and write nothing. Case (d) is test 19d.
(d) **Layout edge cases.** For tmp alias files: a block followed by a blank line and a comment, with a comment between two entries, gets the new line directly after the last `- ` entry with the blank and comment lines still after it; a metric with no block gets one created at the end of the file with every original byte kept; a file without a trailing newline works for both an existing last block and a missing block. In each case `safe_load` returns the label and every original line is preserved.
20. **Uses run.run.** A spy on `pipeline.run.run` is hit exactly once by `load` (all files, with `aliases_path` and `accepted` as keywords), by `rerun_company` (only that company's files) and by a Northpass `resolve` (both Northpass files).
21. **No network.** An AF_INET connect and `create_connection` raise and are recorded; a `load` plus a `resolve` then record no attempt at all.
22. **Several decisions replay in order.** E7 confirm, then Verity's impossible_value confirm, then a Harborview reject, all in one state dir: `decisions.jsonl` has those three lines in that order; nothing Verity-related stays blocked; a fresh `load` equals the final state and its `decisions` equal the file; `load(paths=[Ridgeline only])` with those decisions on file works and returns the clean control with no review items. **22b (order):** in a tmp CSV with two unknown labels that both suggest `new_arr`, confirming them in either order puts the two aliases into the block in that order, and rebuilding from `decisions.jsonl` alone (fresh seeded aliases) reproduces each file byte for byte; the two orders must give different files, so replay by sorted ID, by set, or by last-only fails one of them.

## Audit mutations (fresh session, after the loop)

Each of the 37 rows below must make at least one checker test fail. Revert it after each run.

| Mutation | Test that must catch it |
|---|---|
| `rerun_company` computes with the company's own months | 3, 18 |
| `resolve` reruns every company | 5, 20 |
| Alias cache not cleared after learning | 4, 11 |
| Alias written to the default or tracked path instead of `cfg` | 11, 12, 16 |
| `Config.default().aliases_path` set to `pipeline/aliases.yaml` | 16 |
| `accepted` ignored in `run_checks` | 7 |
| Decisions not persisted, or not replayed on load | 6, 7 |
| `item_id` random or index-based | 2 |
| `item_id` includes `reason` | 2 |
| Confirm allowed on missing_month, rollforward or continuity | 10 |
| Instruction-like, empty, generic or cross-metric label learnable | 19 |
| `learn_alias` rewrites the file with `safe_dump` | 19 |
| `reject` changes data or closes the item | 6 |
| Splice drops or changes other companies' rows | 3, 4 |
| Duplicate confirm restores the older file's value | 8 |
| `resolve` mutates its input | 17 |
| A second confirm appends a duplicate alias line | 17 |
| Run-record file named `<ts>.json`, or `resolve` also calls `load` | 13 |
| `review` re-implements the `run()` body | 20 |
| Default `run()` behavior changed | 12 (E7 still flagged), the stage 3 and 4 scorers. Test 1 compares against `run()` itself, so it cannot catch this alone |
| Injection text copied into a record or decision | 9 |
| `raw_label` re-reads files (any route, incl. `Path.read_text` or `from openpyxl import load_workbook`) | 14 |
| `learn_alias` inserts after the block header | 19a |
| `learn_alias` inserts after trailing blank or comment lines | 19d |
| `learn_alias` cannot create a missing block, or breaks a file without a trailing newline | 19d |
| `learn_alias` quotes too narrowly (`&x`, `yes`, `1e3` load as other types) | 19 |
| Only the last decision is replayed | 22 |
| Replay ignores file order (sorted, set-based) | 22b |
| Spliced table or metrics lose canonical row order | 3, 4 |
| `Config.default()` creates files or folders | 16 |
| `resolve` record copies reason text | 13 |
| Closed duplicate or injection item reappears on reload | 8, 9 |
| `resolve` mutates `files_by_company` or `paths` | 17 |
| A build catches and swallows a network error | 21 and the per-test guard |
| A test or build writes the tracked alias file or adds a file to `data/raw/` | the per-test guard |
| Accepting an item drops a different item from its company's queue | 7, 9 |
| Failed call still writes a record | 10 |

## Locking

`tests/test_review_loop_checker.py`, `docs/stage6a_review_loop_checker_spec.md`, `tests/test_metrics.py` and `.claude/settings.json` are in the deny rules in `.claude/settings.json`, next to the existing locked list. `tests/test_metrics.py` was not locked before; it is added here so the build loop cannot weaken it. The deny entries must be present in `.claude/settings.json` (edited by hand, since the file is itself denied) **before** `START_HASH_6A` is recorded. Deny rules only load when Claude Code is started with the repo folder as its workspace. The `git diff --stat <START_HASH_6A> HEAD -- <locked files>` check in the `/goal` prompt is the real safety net: it catches a change however it was made. Record `START_HASH_6A` after this spec and the checker are committed.

The stage 6 dashboard checker does not exist yet, so the 6a loop cannot weaken it. It is written and locked after 6a merges, as a second checker commit.

## /goal prompt for the build loop

```
/goal On branch review-loop, build the stage 6a review loop so that tests/test_review_loop_checker.py passes, following docs/stage6a_review_loop_checker_spec.md: add pipeline/review.py, and add the keyword parameters to pipeline/run.py (aliases_path, accepted), pipeline/checks.py (accepted), pipeline/mapping.py (aliases_path, learn_alias) and pipeline/metrics.py (months).

Rules:
- Do not edit locked files: eval/**, pipeline/contract.py, tests/test_scorer.py, tests/test_metrics_checker.py, tests/test_commentary_checker.py, tests/test_metrics.py, tests/fixtures/commentary/**, docs/stage5_checker_spec.md, .claude/settings.json, docs/stage6a_review_loop_checker_spec.md, tests/test_review_loop_checker.py.
- The pipeline must never read the scorer folder, answer_key or ground_truth, and no new code or comment may contain those names or the strings "eval/" or "from eval".
- Default behavior of run(paths), run_checks(df, files), match_label(label), to_records(fd) and metrics.compute(table, queue) must not change.
- Never write pipeline/aliases.yaml or data/raw/. Learned aliases go only to Config.aliases_path (default outputs/state/aliases.yaml, seeded from pipeline/aliases.yaml).
- review.py calls run.run and metrics.compute through their modules; it does not re-implement run().
- No skip, xfail or importorskip anywhere. No real API calls.
- Update CLAUDE.md (commands and modules) to describe pipeline/review.py.
- You are allowed to commit to review-loop. Do not push.

Done only when the transcript contains, pasted verbatim from the final committed state:
1. pytest -q: all passed, 0 failed, 0 errors, 0 skipped
2. python eval/score.py --stage 3 summary: recall 7/7, Ridgeline 0 false flags
3. python eval/score.py --stage 4 summary: 250/250
4. git diff --stat <START_HASH_6A> HEAD -- eval/ pipeline/contract.py tests/test_scorer.py tests/test_metrics_checker.py tests/test_commentary_checker.py tests/test_metrics.py tests/fixtures/commentary/ docs/stage5_checker_spec.md .claude/settings.json docs/stage6a_review_loop_checker_spec.md tests/test_review_loop_checker.py : empty output
5. git status --porcelain: empty output (CLAUDE.md and every other changed file must be committed first; outputs/ is gitignored)
```
