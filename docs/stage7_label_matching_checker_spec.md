# Stage 7 — Claude Label Matching Checker Spec

Nov 6, 2026. Write and lock the checker before any build work starts. Commit this doc together with `tests/test_label_matching_checker.py`.

## Prerequisite

- **Branch:** `label-matching`, cut from `dashboard-dev` at `b65864f` (stage 6 is not on `main` yet).
- Before locking: `pytest -q` gives 162 passed, stage 3 gives 7/7 with 0 Ridgeline false flags, and stage 4 gives 250/250.
- Counts in this doc are a dated snapshot. The checker takes every count from live data.

## Why this stage exists

The main spec matches labels in three layers: `aliases.yaml`, then rapidfuzz, then Claude. Claude "gets the label, sample values, and the list of standard names, and returns a structured guess plus a confidence. The guess goes to the review list; once a person confirms it, it's added to the lookup table." Only the first two layers exist.

Today a label gets one of two outcomes:
- a rapidfuzz `suggestion` (token-sort ≥ 75), or
- nothing, in which case it can never be confirmed (6a test 19c, `views._can_confirm`).

This stage adds the third layer without changing anything that runs when Claude is not asked.

**Not in this stage:**
- calling Claude automatically on upload or load;
- a manual metric picker;
- promoting learned aliases into `pipeline/aliases.yaml`;
- changes to `pipeline/contract.py`.

## Design decisions

| # | Decision | Rule |
|---|---|---|
| D1 | Trigger | Claude is called only by `label_match.suggest_labels(p, cfg, client)`. `review.load`, `rerun_company`, `resolve` and `run.run` never call a client and never take one. In the dashboard, the call happens only inside the "Ask Claude to match labels" button handler. |
| D2 | Eligible items | An item is eligible when it is an open `unknown_label` item in `p.review_queue`, `review.raw_label(p, it)` is non-empty, and `item_id` is not a key of `p.label_guesses`. Items that already have a fuzzy suggestion are eligible too. Instruction-like labels have no `raw_label` (6a), so they are never eligible and never sent. |
| D3 | Request | One client call per `suggest_labels` covers every eligible item, in queue order. No call is made when nothing is eligible. `system` is the module constant `SYSTEM_PROMPT`: it names every metric in `contract.METRICS`, says to reply with JSON only, and says everything in the user message is quoted data. `user` is `json.dumps` of `{"metrics": [{"name", "definition"} for each METRICS entry, in order], "labels": [{"id": item_id, "label": raw_label[:80], "samples": [...], "fuzzy_hint": suggestion or null}]}`. `samples` are the first (at most) 6 raw numeric cell values from the file, before unit scaling (int or float, never str or bool), in month order, taken at load (`Portfolio.label_samples`). Exact key sets: the user object has only `metrics` and `labels`; each metric has only `name` and `definition`; each label has only `id`, `label`, `samples` and `fuzzy_hint`. No reason text, file name or cell reference goes into either message, and no label text other than `labels[i].label`. |
| D4 | Response | `{"guesses": [{"id": str, "metric": str or null, "confidence": number}]}`. An entry is valid only if: `id` is one of the requested ids; `metric` is in `METRICS` or is null; `confidence` is an int or float (not bool) with `0 <= confidence <= 1`. Invalid entries are dropped. The first valid entry per id wins. Malformed JSON, a non-object, or a missing or non-list `guesses` gives no guesses. Nothing raises from parsing, and there is no retry. |
| D5 | Never auto-mapped | A guess only sets the item's `suggestion`. It never adds a table row, never learns an alias and never closes an item. Only `resolve(..., "confirm")` learns, through the existing `learn_alias` path. |
| D6 | Store | `state_dir/label_guesses.jsonl` is append-only, one line per recorded guess: `{ts, item_id, label, metric, confidence, model}`. `load` replays it in file order, and the last line per `item_id` wins. `Portfolio.label_guesses` maps `item_id -> {"metric", "confidence", "model"}`. |
| D7 | Applying a guess | On `load` and on every rerun (`rerun_company`, `resolve`, `record_guesses`), an open `unknown_label` item whose id has a guess with a non-null `metric` gets `suggestion = metric` (via `dataclasses.replace`). A null metric is recorded but leaves the item exactly as it was (fuzzy suggestion or None). `item_id` ignores `suggestion` (6a D6), so ids and decisions stay valid. |
| D8 | Rerun scope | `record_guesses` reruns, through `run.run`, only the companies of items whose guess has a non-null metric. A null-only result reruns nothing. Other companies' rows are carried over unchanged, in canonical order. |
| D9 | Logging | Each client call appends one line to `state_dir/label_log.jsonl`: `ts, model, attempt (always 1), system, user, output`. `model == client.model`, and `output` is exactly what the client returned. A line is written even when nothing in the output is valid. A client call that raises writes no log line. |
| D10 | Atomicity | `suggest_labels` builds the new Portfolio first. Only if that succeeds does it append to `label_guesses.jsonl` and write the run record. If the client raises, or the rerun raises, the store, the run records and the aliases file are unchanged, and the exception propagates. A client call that raises writes no log line, so nothing at all changes. The log line for a call that returned is still written even if the rerun then raises. |
| D11 | Run record | When at least one valid guess is recorded, it writes exactly one `state_dir/runs/<µs>-<seq>-suggest.json`, created in `"x"` mode like 6a D7. The record is a JSON object with: `kind` (`"suggest"`); `inputs` (`[{file, sha256}]`: the file name and sha256 of every file of the rerun companies, in `files_by_company` order, and no others); `open_item_ids` (the ids of the open queue after the suggest); `guesses` (`[{item_id, metric, confidence}]`). A result whose guesses are all null still writes one record, with `inputs` `[]`. No string value in the record is label text, reason text or cell text. With no valid guess, nothing is written to the store or to `runs/`. |
| D12 | Purity | `suggest_labels` and `record_guesses` return a new Portfolio and never mutate their input. When nothing is recorded (no valid guess), the input object may be returned unchanged. |
| D13 | Decision audit | A `confirm` decision on an `unknown_label` item also stores `suggested_by` and `confidence`. `suggested_by` is `"claude"` when `p.label_guesses[id]["metric"]` equals the item's `suggestion` (and is non-null); `confidence` is then that guess's confidence. Otherwise `suggested_by` is `"fuzzy"` and `confidence` is null. Decisions on items that are not `unknown_label` have no `suggested_by` or `confidence` key. Every other decision field and rule is unchanged from 6a. |
| D14 | Module boundaries | `pipeline/label_match.py` imports no `anthropic`, `streamlit` or `pipeline.llm`; the client is injected. `pipeline/review.py` and `pipeline/mapping.py` import neither `anthropic`, `pipeline.llm` nor `label_match`. No file in `pipeline/` names the scorer folder, `answer_key` or `ground_truth`. |
| D15 | Defaults unchanged | With no `label_guesses.jsonl`: `load` gives the same table, queue and metrics as today; `label_guesses == {}`; `queue_rows` keeps every existing key and value. The stage 3 and stage 4 scorers and every locked checker pass unchanged. |
| D16 | Dashboard | The Review queue page shows `st.button("Ask Claude to match labels", key="suggest_labels")` only when `label_match.eligible(p)` is non-empty. In the handler, the client is `session_state["label_client"]` if set, else `llm.AnthropicClient()`, built through the module attribute inside the handler. It is never taken from `session_state["claude_client"]`, which is the commentary fake in the stage 6 tests. On success: `session_state["portfolio"] = new` and `st.rerun()`. On any exception, including building the client: `st.error("Claude could not suggest labels.")`, a fixed string, with the portfolio unchanged (same object). All rendering follows S6 D9. |
| D17 | Queue rows | `views.queue_rows` adds two keys. `suggested_by`: `"claude"` if the item has a guess with a non-null metric equal to its `suggestion`, else `"fuzzy"` if it has a `suggestion`, else `—`. `confidence`: `f"{c:.2f}"` when `suggested_by == "claude"`, else `—`. The page's queue dataframe shows both columns. |

## Interface the checker tests

```python
# pipeline/label_match.py
SYSTEM_PROMPT: str
@dataclass(frozen=True)
class LabelGuess:
    metric: str | None
    confidence: float
def eligible(p: Portfolio) -> list[ReviewItem]
def guess_labels(client, requests: list[dict], log_path: Path) -> dict[str, LabelGuess]
def suggest_labels(p: Portfolio, cfg: Config, client) -> Portfolio
# pipeline/review.py  (new fields have defaults; all 6a signatures unchanged)
@dataclass
class Portfolio:
    ...                                   # 6a fields
    label_samples: dict = {}              # item_id -> list[float], unknown_label items with a raw label
    label_guesses: dict = {}              # item_id -> {"metric", "confidence", "model"}
def record_guesses(p: Portfolio, cfg: Config, guesses: dict[str, LabelGuess], model: str) -> Portfolio
```

The client is any object with an attribute `model: str` and a method `create(system: str, user: str) -> str`: the `Client` protocol of `pipeline/commentary.py`, which `llm.AnthropicClient` satisfies.

`requests` passed to `guess_labels` are the `labels` entries of D3. `suggest_labels` = `eligible`, then `guess_labels(client, requests, cfg.state_dir / "label_log.jsonl")`, then `record_guesses`.

## Fixtures

- **`cfg`:** a tmp `Config` as in 6a.
- **Guards:** the module-wide network block and the real-file guard (`pipeline/aliases.yaml`, `data/raw/*`, `ROOT/outputs/`) from 6a and 6.
- **`FakeLabelClient`:** `.model = "fake-label-model"`. It records `(system, user)` for every call. It answers either with a fixed raw string or with a function of the parsed `labels` list, which lets it echo the real ids.
- **Far label:** a tmp copy of the Verity CSV with the `Net New Logos ARR (USD)` header replaced. The replacement is a label with no fuzzy suggestion (`Zzyzx Quotient`, asserted to have `suggestion is None` first).
- **Imports:** `pipeline.label_match`, the new `review` names and `dashboard.views` are imported lazily, so each test fails on its own before the build. No skip, xfail or importorskip.

## Checker tests (`tests/test_label_matching_checker.py`, locked)

1. **Defaults unchanged.**
   - `load(cfg)` with no store gives `label_guesses == {}`.
   - Table, queue and metrics equal `run(RAW)` plus `compute_metrics`.
   - `label_samples` has an entry for E7 equal to the first (at most) 6 raw CSV values under the E7 header, in month order.
   - `eligible(p)` is exactly the open `unknown_label` items with a raw label, which includes E7.
   - In `queue_rows`, E7 shows `suggested_by == "fuzzy"`, and items without a suggestion show `—` / `—`.
2. **A Claude guess replaces the suggestion (D5, D7).** The fake answers E7 with `expansion_arr` at 0.4.
   - E7 is still open with the same id, and its `suggestion` is now `expansion_arr`.
   - `label_guesses[id]` has metric, confidence and model.
   - The table is equal and the aliases copy is byte-equal to the seed.
   - No `decisions.jsonl` is written, and the other four companies are equal.
   - `queue_rows` shows `claude` / `0.40`, and E7 is no longer eligible.
3. **Confirm after a Claude guess.** The fake answers `new_arr` at 0.9, then E7 is confirmed.
   - The label is learned under `new_arr` in `cfg.aliases_path` only.
   - The unblocked metric ids and their values equal an independent alias-aware run, as in 6a test 4.
   - The decision line has `suggested_by == "claude"` and `confidence == 0.9`.
   - A confirm without a Claude guess, on a fresh state, has `suggested_by == "fuzzy"` and `confidence is None`.
4. **Far label becomes confirmable.**
   - Before: the item has no `confirm:` action and `resolve` confirm raises `ValueError`.
   - After a fake guess of `new_arr`: the item has a `confirm:` action, and confirm learns `Zzyzx Quotient` under `new_arr`.
   - Verity's `growth_12m` is then non-blank.
5. **Invalid output (parametrized):**
   - malformed JSON;
   - a list instead of an object;
   - no `guesses` key;
   - an unknown metric;
   - a non-str metric;
   - confidence 1.5, -0.1, `"high"` or `true`;
   - an unknown id;
   - a missing id;
   - a non-dict entry;
   - a list-valued id;
   - a dict-valued metric and a list-valued metric;
   - `guesses: null`;
   - a `NaN` confidence.

   Each case gives `guess_labels == {}`, and `suggest_labels` makes 1 call, writes 1 log line, writes no store line and no `-suggest.json`, and returns a state equal to the input. E7 stays eligible.
   - Mixed valid and invalid entries (including non-dict entries and a list-valued id before the valid one): only the valid one is applied.
   - Duplicate ids: the first valid entry wins.
6. **Null metric.**
   - The guess is recorded (one store line, metric null).
   - E7 keeps its fuzzy suggestion and is not eligible any more.
   - `run.run` is not called.
   - A second `suggest_labels` makes no call.
   - A null-only result writes exactly one `-suggest.json` record: `kind == "suggest"`, `inputs == []`, and the null guess in `guesses`.
   - E7 still has `suggestion == "new_arr"` and `label_guesses[id]["metric"] is None` after `review.load(cfg)`, after `rerun_company(p, VERITY)` and after `resolve` of E6.
   - The E6 confirm decision has no `suggested_by` or `confidence` key.
   - Confirming E7 afterwards stores `suggested_by == "fuzzy"` and `confidence is None`, with `learned_metric == "new_arr"`.
7. **Untrusted text.** A tmp Verity CSV with two changed headers: one instruction-like label (the injection phrase plus a planted figure), and a far label longer than 80 characters.
   - The instruction-like header produces an `unknown_label` item whose `raw_label` is None, and that item is not in `eligible(p)`.
   - The injection phrase is in no `system` or `user`, checked on what the client received and on the lines of `label_log.jsonl` (which must be equal).
   - The far label appears in `user` only as `labels[i].label`, equal to its first 80 characters, and not in `system`.
   - Exact key sets: top level `{metrics, labels}`, each metric `{name, definition}`, each label `{id, label, samples, fuzzy_hint}`.
   - The far label has `fuzzy_hint` None. On the live data, E7 has `fuzzy_hint == "new_arr"`.
   - `samples` equal `p.label_samples[id]`, which equals the first (at most) 6 raw CSV values under that header in month order, all int or float (not bool).
   - `metrics` names equal `METRICS` in order, and `SYSTEM_PROMPT` contains every metric name.
   - No message contains any review item's `reason`, any review item's source file name (or the `verity_kpis` stem) or any review item's cell reference.
8. **Logging (D9).**
   - Over three `suggest_labels` calls on fresh states (valid, malformed, null), the log has one line per call.
   - Every line has all six fields, `model == "fake-label-model"`, `attempt == 1`, and `output` equal to the returned text.
9. **Replay.** After a guess on E7:
   - A fresh `load(cfg)` reproduces the post-guess state (table, queue with the new suggestion, metrics, `label_guesses`) without any client.
   - `rerun_company(p, VERITY)` keeps it.
   - Confirming E6 (another Verity item) keeps it, and E6's decision has no `suggested_by` or `confidence` key.
   - Two guesses for the same id in the store (written by `record_guesses` across two loads): the last line wins.
10. **Batching and no-op.**
    - A tmp CSV with two far labels gives one call containing both ids.
    - A second `suggest_labels` makes no call.
    - A Ridgeline-only load has nothing eligible: no call, no log, no store, no record, and an equal state.
11. **Scope (D8).** A spy on `run.run` during a valid E7 guess sees only Verity's files, exactly once, and the other companies are equal.
12. **Run record (D11).**
    - Exactly one new `-suggest.json` record. Its name matches `^\d+-\d+-suggest\.json$`.
    - `kind == "suggest"`.
    - `inputs` equal exactly Verity's file names with their sha256 (the rerun company's files, no others).
    - `open_item_ids` equal the ids of the open queue after the suggest.
    - `guesses` equal `[{item_id, metric, confidence}]` for the one guess.
    - No string value in the parsed record contains the label or any review item's `reason`.
13. **Atomicity and purity (D10, D12).**
    - A client that raises: the exception propagates, and the store, `runs/` and aliases are unchanged.
    - `run.run` patched to raise during `record_guesses`: same result, plus one log line.
    - The input Portfolio is unchanged after a successful suggest, and the result is a new object.
14. **Static scan (D14).**
    - An `ast` scan, not a regex, of `label_match.py` finds no import of `anthropic`, `streamlit` or `pipeline.llm`, in any spelling: `import`, `from … import` (absolute or relative, multi-line, comma lists, nested in functions) and `importlib.import_module` / `__import__` with a literal argument.
    - The same scan of `review.py` and `mapping.py` finds no `anthropic`, `pipeline.llm` or `pipeline.label_match`. A comment or docstring that only names a module is not an import and is allowed.
    - No `pipeline/*.py` names the scorer folder, `answer_key` or `ground_truth` (text scan).
    - Subprocess check, `python -I -c` with cwd=ROOT (the snippet puts ROOT on `sys.path` itself, because `-I` drops the cwd): after `import pipeline.review, pipeline.mapping`, `sys.modules` holds none of `anthropic`, `pipeline.llm`, `pipeline.label_match`; after `import pipeline.label_match`, none of `anthropic`, `pipeline.llm`, `streamlit`.
    - The forbidden names are built by concatenation in the checker.
15. **Default config, real files, network.** `pipeline/aliases.yaml`, `data/raw/*` and `ROOT/outputs/` are unchanged across the module, and no network attempt happens.

**AppTest** (`pages/2_Review_queue.py`, `default_timeout=60`, tmp `cfg`):

16. **Button visibility.**
    - With the live portfolio, `suggest_labels` is shown.
    - With a Ridgeline-only portfolio, or after E7 has a guess, it is not.
    - No client is built on a plain run.
17. **Click flow.** With `label_client` = fake (E7 → `new_arr`, 0.9) and a commentary fake in `claude_client` that must not be called:
    - one label call is made;
    - the session portfolio's E7 has `suggested_by` `claude`;
    - the queue dataframe has `suggested_by` and `confidence` columns, with `claude` / `0.90` in E7's row;
    - one store line exists in `cfg`.

    Clicking `confirm:<E7>` then appends a confirm line with `suggested_by == "claude"`.
18. **Far label in the app.**
    - On a far-label portfolio, `confirm:<id>` is absent before the click and present after it.
    - Clicking it learns the alias.
19. **Errors.**
    - A `label_client` whose `create` raises: `[e.value for e in at.error] == ["Claude could not suggest labels."]`, the session portfolio is the same object, and there is no store line.
    - No `label_client` and `llm.AnthropicClient` patched to raise: same single fixed error.
    - No `label_client` and a counting stand-in for `llm.AnthropicClient`: it is built only on the click, exactly once.
20. **Untrusted label on the page.** A far label containing `**bold** https://evil.example :smile:`:
    - after the click, no markdown-family element or container label contains any part of it;
    - it appears in a text or dataframe value.

## Audit mutations (fresh session, after the loop)

| Mutation | Test that must catch it |
|---|---|
| `load` calls Claude, or `suggest_labels` runs at load | 1, 16 |
| Guess applied to the table or alias learned on suggest | 2 |
| Guess changes `item_id` (e.g. id includes suggestion) | 2, 9 |
| Null metric overwrites the fuzzy suggestion | 6 |
| Metric not in METRICS accepted | 5 |
| Confidence out of range, string or bool accepted | 5 |
| Unknown id applied to another item | 5 |
| Instruction-like label sent | 7 |
| Label not truncated, or reason text sent | 7 |
| Label placed in `system` | 7 |
| Log skipped on invalid output | 5, 8 |
| Guesses not persisted, or not replayed on load | 9 |
| Store replay keeps the first line instead of the last | 9 |
| Second suggest re-asks for guessed items | 6, 10 |
| One call per label instead of one batch | 10 |
| Every company rerun | 11 |
| Record contains label text | 12 |
| Store written before the rerun succeeds | 13 |
| Input Portfolio mutated | 13 |
| `review.py` imports `anthropic` / `llm` / `label_match` | 14 |
| `suggested_by` missing or wrong on confirm | 3, 17 |
| Button uses `claude_client` | 17 |
| Client built at page load | 16, 19 |
| Exception crashes the page or changes the session | 19 |
| Label rendered via markdown | 20 |
| Null guess replayed on `load`, `rerun_company` or `resolve` overwrites the fuzzy suggestion | 6 |
| `suggested_by` is `claude` for a null guess (decision or queue row) | 6 |
| A non-label confirm decision gains `suggested_by` or `confidence` | 6, 9 |
| A null-only result writes no record | 6 |
| Record `inputs` list the wrong files, `open_item_ids` missing, or `kind` wrong | 12 (and 6 for a null-only `inputs` and `kind`) |
| User JSON has extra keys (file name, cell), `fuzzy_hint` always null, or samples reversed or not the file's values | 7 |
| A non-dict entry, a list-valued id, a dict or list metric, `guesses: null` or a `NaN` confidence crashes parsing or is applied | 5, 5b |
| Import bypass in `review.py` or `label_match.py`: relative, multi-line, comma list, `importlib` or `__import__` with a literal, or a nested import | 14 |

## Locking

New locked files: `tests/test_label_matching_checker.py` and `docs/stage7_label_matching_checker_spec.md`. Add 4 deny rules (Edit and Write for each) to `.claude/settings.json` by hand, which makes 32 in total. Check them with `python -m json.tool`, commit, and record `START_HASH_7`.

## /goal prompt for the build loop

```
/goal On branch label-matching, build stage 7 (Claude label matching) so that tests/test_label_matching_checker.py passes, following docs/stage7_label_matching_checker_spec.md: add pipeline/label_match.py; add Portfolio.label_samples, Portfolio.label_guesses, record_guesses and guess replay to pipeline/review.py, plus suggested_by/confidence on unknown_label confirm decisions; add suggested_by/confidence to dashboard/views.queue_rows; add the "Ask Claude to match labels" button to pages/2_Review_queue.py.

Rules:
- Do not edit locked files: eval/**, pipeline/contract.py, tests/test_scorer.py, tests/test_metrics_checker.py, tests/test_commentary_checker.py, tests/test_metrics.py, tests/fixtures/commentary/**, docs/stage5_checker_spec.md, .claude/settings.json, docs/stage6a_review_loop_checker_spec.md, tests/test_review_loop_checker.py, tests/test_dashboard_checker.py, docs/stage6_dashboard_checker_spec.md, requirements.txt, tests/test_label_matching_checker.py, docs/stage7_label_matching_checker_spec.md.
- Do not write the substring "eval/" anywhere in pipeline/, not even inside words or comments (test_scorer bans it).
- Default behavior of run, load, rerun_company, resolve, match_label, to_records and metrics.compute must not change when no label guesses exist.
- Claude is called only from label_match.suggest_labels via an injected client. review.py and mapping.py never import anthropic, pipeline.llm or label_match.
- Never write pipeline/aliases.yaml, data/raw/ or outputs/ from tests. No code may read the scorer folder, answer_key or ground_truth.
- No skip, xfail or importorskip. No real API calls.
- Update CLAUDE.md (label matching layers, modules, commands).
- You are allowed to commit to label-matching. Do not push.

Done only when the transcript contains, pasted verbatim and untrimmed from the final committed state:
1. pytest -q: "203 passed" (162 existing + 41 in tests/test_label_matching_checker.py), 0 failed, 0 errors, and no "skipped" in the summary line
2. python eval/score.py --stage 3 (full output): recall 7/7, Ridgeline 0 false flags
3. python eval/score.py --stage 4 (full output): 250/250
4. git --no-pager diff --stat <START_HASH_7> HEAD -- eval/ pipeline/contract.py tests/test_scorer.py tests/test_metrics_checker.py tests/test_commentary_checker.py tests/test_metrics.py tests/fixtures/commentary/ docs/stage5_checker_spec.md .claude/settings.json docs/stage6a_review_loop_checker_spec.md tests/test_review_loop_checker.py tests/test_dashboard_checker.py docs/stage6_dashboard_checker_spec.md requirements.txt tests/test_label_matching_checker.py docs/stage7_label_matching_checker_spec.md : empty output
5. git --no-pager status --porcelain: empty output
```

## Review response

Response to the pre-lock review of this checker (findings #1-#20). Test numbers are those above. `X12` (log `output` stripped), `Y3` (no-op returns the same object, now allowed by D12) and `Y16` (int confidence cast to float) stay uncaught on purpose.

| # | Finding | Handled |
|---|---|---|
| 1 | A null guess could overwrite the fuzzy suggestion on replay (audit row "Null metric overwrites…" missed) | Test 6 now checks E7 after `load`, `rerun_company` and `resolve` of E6 |
| 2 | Confirm after a null guess untested (D13) | Test 6 confirms E7 afterwards: `fuzzy` / null |
| 3 | `eval/score.py` takes only `--stage 3\|4` | /goal proofs 2-3 use `--stage 3` and `--stage 4` |
| 4 | Proof 4 had placeholders | All 16 paths written out; `<START_HASH_7>` is the only placeholder |
| 5 | Pager, "0 skipped" and a pinned count | `git --no-pager` in proofs 4-5; proof 1 pins "203 passed" |
| 6 | Run record body unasserted | Test 12 checks `kind`, `inputs`, `open_item_ids`, `guesses`, and text against the parsed record's string values; D11 spells the fields |
| 7 | D3 payload not pinned | Test 7 checks exact key sets, `fuzzy_hint`, samples against the CSV, no file name or cell, and the logged messages; D3 states the key sets and "raw, before unit scaling" |
| 8 | Malformed entry shapes untested | Test 5 gains six cases; 5b gains a mixed non-dict / list-id case |
| 9 | Regex import scan bypassable | Test 14 uses an `ast` scan plus a `python -I` subprocess `sys.modules` check. A comment that only names a module is no longer flagged (the old regex did) |
| 10 | Vacuous live loop in test 7 | Deleted; test 7 asserts the instruction-like header gives an `unknown_label` with `raw_label` None, not eligible |
| 11 | D12 "new Portfolio" on the no-op path | D12 now allows returning the input unchanged when nothing is recorded |
| 12 | Raising client writes no log line (test 13 stricter than the spec) | D9 and D10 say so; test 13 unchanged |
| 13 | Client protocol unstated | Interface section states `model` and `create(system, user) -> str` |
| 14 | Samples semantics unstated | D3: raw numeric cell values, before unit scaling |
| 15 | Null-only record undefined | D11: still writes one record, `inputs` `[]`; asserted in test 6 |
| 16 | Non-label decisions might gain audit keys | D13 forbids it; asserted on E6's decision in tests 6 and 9 |
| 17 | Error text was a membership check | Test 19 asserts the exact list |
| 18 | Test 15 is redundant with the autouse guard | Kept by design |
| 19 | `.claude/settings.json`: whitespace-only diff, 4 new deny rules not yet added | Done by hand at lock time (28 rules now, 32 after) |
| 20 | `eval/` substring ban in `pipeline/` | New /goal rule |
