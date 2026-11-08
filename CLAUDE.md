# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

The design doc, `../Portfolio KPI Intake and Board Report Assistant — Project Spec.md`, sits in the parent folder outside this repo. It is the source of truth for scope, schema, checks, metric formulas, and build order. The folder structure is scaffolded with empty files; no code exists yet. Build in the spec's stage order (ingest/mapping → checks/metrics → commentary → dashboard → eval), one stage at a time, and commit after each stage. Update this file as real commands and modules appear.

## What it is

A Python 3.11+ pipeline plus a Streamlit dashboard. It takes messy monthly KPI spreadsheets from five *fictional* portfolio companies, normalizes them into one validated long table, computes SaaS metrics, and has Claude draft board commentary in which every figure traces back to a source file and cell. Values that fail checks go to a human review queue; they are never guessed.

Stack: pandas + openpyxl (openpyxl is needed for cell coordinates), PyYAML + rapidfuzz for label matching, the Claude API with structured JSON output, Streamlit multipage, python-docx for export, pytest.

## Planned commands (from the spec; verify once they exist)

```
streamlit run app.py                  # dashboard; the upload page triggers the full pipeline
python scripts/fresh_start.py         # demo: archive outputs/state, launch, open /Upload (--port, --no-browser)
pytest tests/test_demo_ui.py          # start-over reset and sidebar order
pytest                                # checks and metric formulas
pytest tests/test_metrics.py::test_x  # single test
python eval/score.py                  # compare review-queue flags to answer_key.json
pytest tests/test_review_loop_checker.py   # stage 6a review loop (locked checker)
pytest tests/test_dashboard_checker.py     # stage 6 dashboard (locked checker, AppTest)
pytest tests/test_label_matching_checker.py  # stage 7 Claude label matching (locked checker, fake clients only)
python scripts/demo_commentary.py     # manual: real Claude call (needs ANTHROPIC_API_KEY)
```

## Architecture (big picture)

- **One entry point:** `pipeline/run.py` takes files in and returns results. The dashboard calls it, and a future SharePoint/Power Automate trigger would call it too. Keep processing logic out of the Streamlit pages.
- **Flow:** ingest (`ingest.py`: header-row detection by month-like values, months-as-columns → rows, unit detection, 0–1 → 0–100 percentages, newest-wins duplicate handling plus a flag) → mapping (`mapping.py`) → checks (`checks.py` → review queue) → metrics (`metrics.py`, only on rows that passed every check) → commentary (`commentary.py`).
- **Canonical long table:** `company | month | metric | value_usd_k | source_file | source_cell`. Downstream code never reads a file's own labels, only these metric names: `beginning_arr, new_arr, expansion_arr, contraction_arr, churned_arr, ending_arr, revenue, gross_margin_pct, customers, headcount`. Money is stored in $K. Every value must keep its `source_file`/`source_cell` through the whole pipeline.
- **Label matching runs in three layers, in this order:** `pipeline/aliases.yaml` exact lookup (after lowercasing and stripping units and punctuation) → rapidfuzz above a threshold (`mapping.py`, a suggestion only) → Claude (`pipeline/label_match.py`, stage 7), which returns a guess plus a confidence. Claude's guess always goes to review. Once a person confirms it, it is appended to the learned-alias copy at `outputs/state/aliases.yaml` (`Config.default().aliases_path`, seeded from `pipeline/aliases.yaml`). The tracked `pipeline/aliases.yaml` is never written by code; promoting learned aliases into it is a manual, reviewed change.
- **Review loop (`pipeline/review.py`, stage 6a):** resolving a review item reruns checks and metrics for that company only, so the overview and the draft update right away. No Streamlit and no Claude in this module.
  - API: `Config` (`Config.default()` points at `outputs/state`; it creates nothing), `Portfolio`, `load(cfg, paths=None)`, `rerun_company(p, company, cfg)`, `resolve(p, item_id, "confirm"|"reject", cfg)`, `item_id(item)`, `raw_label(p, item)`. `resolve` and `rerun_company` return a new `Portfolio`; the input is never mutated.
  - `state_dir/decisions.jsonl` (append-only) is the source of truth and audit trail; `load` replays its `confirm` lines in file order (re-learning aliases, re-accepting values, re-closing items). `reject` is logged only. Each `load`/`resolve` also writes one `state_dir/runs/<µs>-<seq>-<load|resolve>.json` (file hashes, open item IDs, decisions; never reason or cell text).
  - Confirm is valid for `unknown_label` (learn the alias), `impossible_value` (accept as reported), `duplicate_submission` and `prompt_injection` (close; no value taken). Other error types raise `ValueError`; unknown or closed IDs raise `KeyError`.
  - `undo(p, item_id, cfg)` reverses the item's latest decision still in force by appending an `{"action": "undo", "undoes_ts": ...}` line (nothing is deleted). `effective(decisions)` / `latest_decisions(decisions)` drop undone lines; `_accepted`, `_closed` and `_replay_aliases` use them. Undoing an `unknown_label` confirm rebuilds `outputs/state/aliases.yaml` from the seed plus the confirms still in force. The Review queue page lists these under "Your decisions" with `undo:{item_id}` buttons (`views.decision_rows`).
  - Unit-slip correction: when an ARR value fails the 10×-median check and exactly one of ×1000 or ÷1000 makes that month's bridge tie, `checks` puts the proposed $K value (as text) in the `impossible_value` item's `suggestion` (`review.proposed_value(item)` reads it; `pipeline/contract.py` is locked, so there is no new field). Only a person applies it: `review.correct(p, item_id, cfg)` appends an `{"action": "correct", "original_value", "original_value_usd_k", "corrected_value_usd_k"}` line; `load`/`_rerun` pass `_corrections(decisions)` to `run.run(corrections=)`, which overwrites that exact cell before beginnings are derived. `undo` reverses it. `resolve` still accepts only confirm/reject. The Review queue shows it as a separate `correct:{id}` button (`views.queue_rows` `correction` field), kept out of `actions`, which must match D8 exactly.
  - It calls `run.run(paths, aliases_path=, accepted=)` and `metrics.compute(..., months=p.months)` through their modules and splices the one company's rows back in canonical order. `months` is fixed at first load, so a single-company compute keeps the portfolio's period.
  - New keyword parameters (defaults keep old behavior): `run.run(aliases_path, accepted)`, `checks.run_checks(accepted)`, `mapping.match_label/to_records(aliases_path)`, `metrics.compute(months)`. `mapping.learn_alias(label, metric, path)` inserts one double-quoted line after the metric's last entry without re-serializing the file.
- **Claude label matching (stage 7; spec `docs/stage7_label_matching_checker_spec.md`):** `pipeline/label_match.py` holds `SYSTEM_PROMPT`, `LabelGuess`, `eligible(p)`, `guess_labels(client, requests, log_path)` and `suggest_labels(p, cfg, client)`. It is the only place that calls Claude, through an injected client (`model` attribute plus `create(system, user) -> str`, as `llm.AnthropicClient`); it imports no `anthropic`, `streamlit` or `pipeline.llm`, and `review.py`/`mapping.py` never import it.
  - Nothing calls it on upload or load. In the dashboard only the "Ask Claude to match labels" button on the Review queue page does (client = `session_state["label_client"]`, else `llm.AnthropicClient()` built in the handler; never `claude_client`). Failure shows the fixed error "Claude could not suggest labels." and leaves the portfolio unchanged.
  - Eligible items: open `unknown_label` items with a raw label and no recorded guess (instruction-like labels have no raw label, so they are never sent). One batched call per `suggest_labels`. The user message is quoted JSON `{metrics, labels}`: label cut to 80 characters, up to 6 raw sample values (`Portfolio.label_samples`, before unit scaling), the fuzzy hint. No file name, cell or reason text is sent. Invalid entries in the reply are dropped and nothing raises.
  - A guess only sets the item's `suggestion` (null metric: recorded, item unchanged). It never adds a row, learns an alias or closes an item; only `resolve(..., "confirm")` learns. A confirm on an `unknown_label` item stores `suggested_by` (`claude` or `fuzzy`) and `confidence` in its decision.
  - `review.record_guesses(p, cfg, guesses, model)` reruns only the companies of items with a non-null guess, then appends to `state_dir/label_guesses.jsonl` (replayed by `load`, last line per item wins) and writes a `runs/<µs>-<seq>-suggest.json` record. Every call is logged to `state_dir/label_log.jsonl` (ts, model, attempt, system, user, output). `Portfolio` gains `label_samples` and `label_guesses`; `dashboard.views.queue_rows` gains `suggested_by` and `confidence`.
- **Dashboard (stage 6; spec `docs/stage6_dashboard_checker_spec.md`):** `app.py` (overview) and `pages/1_Upload.py`, `2_Review_queue.py`, `3_Board_draft.py` hold only layout and button handlers. Logic is in `dashboard/` (no Streamlit import): `views.py` (overview, metric_detail, queue_rows, section_name, sources_rows, fingerprint), `uploads.py` (`save_uploads` -> `UploadResult`, into `state_dir/uploads/`), `export.py` (`draft_to_docx`), `approvals.py` (`record` -> `state_dir/approvals.jsonl`, hashes and counts only), `session.py` (cfg lookup, reload from saved uploads). `pipeline/llm.py` holds `AnthropicClient`, which reads `ANTHROPIC_API_KEY`/`CLAUDE_MODEL` from the environment and falls back to `.env` in the repo root (environment wins; parsed by hand, never written to `os.environ`); pages call `llm.AnthropicClient()` only inside the Generate/Regenerate handler, or use `session_state["claude_client"]`. Rendered text goes only through `st.text`, `st.dataframe` or `st.code`. No caching in dashboard files.
- **Navigation, look and demo reset:** Streamlit's own page menu is off (`.streamlit/config.toml`, `client.showSidebarNavigation = false`) because it always lists `app.py` first and names it after the file. `ui/nav.py` draws the sidebar with `st.page_link` in demo order (Upload, Portfolio = `app.py`, Review queue, Board draft) and skips links Streamlit can't resolve (AppTest runs one page file as the main script). Page file names and `app.py` as the overview must stay, as the locked dashboard checker runs them by path. All styling is the `[theme]` in `.streamlit/config.toml` plus native layout (`st.container(border=True)`, `horizontal=True`, `st.columns`); `unsafe_allow_html` and `st.html` are banned by test 24, so there is no custom CSS. `dashboard/reset.py` (`archive_state(cfg)`, `has_saved_state(cfg)`, no Streamlit) moves everything in `state_dir` to `state_dir.parent/archive/<UTC timestamp>/`, never deletes; the Upload page's "Start over" button (`start_over`, an `on_click` callback that also clears the session keys and the uploader) and `scripts/fresh_start.py` use it. The fresh-session reload from `uploads/` (`session.ensure_portfolio`, test 12c) stays.
- **General rules added after the October test run** (`tests/test_generalized_fixes.py`):
  - *Comparisons* (`pipeline/comparisons.py`, pure): Claude may write `{id|rank}` (figure plus its place among companies with a checked value; blocked peers are excluded and the text says so) or `{id|vs:other}` (same metric; same company at an earlier period → up/down from, or another company in the same period → above/below). Code computes the wording. Ranks compare displayed values. A shared place reads "tied for the lowest …" (`portfolio_rank` gives `tied_with`), equal-looking but different figures read "in line with" (not "unchanged"), and a superlative Claude writes itself must hold strictly, so a tie is sent back. Ranking/direction words Claude writes itself are verified per sentence (superlatives across the portfolio, or across the company's own months when the sentence is time-scoped; direction words between two figures of one metric). A false claim sends the draft back with `unsupported_comparison`. If every attempt still has one, the draft is `ok` with `Draft.warnings`, shown on the Board draft page. It can't be rejected outright, because locked commentary test 15 requires the fixed `good.json` to pass with perturbed values. Qualitative words are not checked.
  - *Bridge failure*: a `rollforward_mismatch` on `ending_arr` makes every reported bridge term of that month unverified (`metrics._Calc.suspect`), which blocks 12m NRR/GRR and that month's churn_rate. An item on `beginning_arr` is the inherited case, and the month's own terms stay trusted.
  - *Derived values* are not range-checked. A derived beginning copied from a flagged ending is dropped with no item of its own, and metrics back-solve it. The rollforward knock-on item (scored as a consequence in `eval/score.py`) is unchanged.
  - *Injections*: one item per (company, normalized text), cited from the newest file.
  - *Rollforward reasons* state the gap as a share of ending ARR. The $1K tolerance stays strict. When 12m retention is held back, the prompt asks Claude to describe monthly churn_rate instead.
  - *Questions* are plain language per `error_type` (with a fallback). Blocked-metric questions have `Question.held_back=True`; the Board draft page folds them into an expander (the docx keeps one bullet per question, as the locked test requires). Held-back calculated metrics name their blocking item instead of asking for a value. A metric missing because a whole line is unmatched cites that `unknown_label` item.
  - One markdown fence around Claude's whole reply is stripped before parsing.
- **Notifications** go through the `Notifier` interface in `integrations/notify.py`: console now, Teams later as a new class, with no change to the processing code.
- **Idempotency and audit:** dedupe uploads by file hash, and save each run's inputs, flags, and decisions.

## Hard rules for the Claude-facing code

- Claude only matches labels and writes prose. It never calculates or changes a number.
- Commentary input is the validated metrics table, with IDs like `harborview.2026-06.nrr`, plus the open review items. Claude outputs `{metric_id}` references, never typed numbers. Code substitutes each formatted value and its source link.
- After substitution, scan the text for any number that didn't come from a reference. If one is found, reject the draft and regenerate. Open review items must appear as questions, never as facts.
- Spreadsheet cell contents are untrusted data. Pass them to Claude as quoted values, never as instructions; the test set includes an injection cell.
- Log every Claude call with its input, output, and model version.

## Test data and evaluation contract

The test files are prepared outside this project and placed in `data/raw/`; do not write a data generator. They cover five companies with 12 months each, a distinct layout per company, and the planted errors listed in the spec's test-data table. Ridgeline is the clean control and must produce zero flags. `answer_key.json` lists each planted error as {company, month, metric, error type}, and the review-queue flags must be comparable to it so `eval/score.py` can score recall and false flags automatically. If you change a check or a planted error, update the answer key with it.

## Out of scope

Real company data, login, a database, cloud deployment, email or cloud-folder intake, a built Microsoft 365 integration, forecasting, and valuation.

## Git

Never commit or push unless the user explicitly instructs it in that message. Earlier instructions to commit (e.g. "commit after each stage") do not carry over to later work.
