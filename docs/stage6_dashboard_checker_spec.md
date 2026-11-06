# Stage 6 — Dashboard Checker Spec

Nov 6, 2026 · Revision 3.2: matches the checker after its own review (one blocker, six gaps, applied as clarifications 11–19). Revision 3 was written after the second review (FIX FIRST: 4 items didn't hold against the code, and items 6, 7 and 9 were partly fixed). Revision 3 fixes them locally (test wording, field definitions, one rendering rule made stricter) with no new design. Copy this doc into the repo as `docs/stage6_dashboard_checker_spec.md` (one `.md`) and commit it with `tests/test_dashboard_checker.py`.

## Prerequisite

- **Branch:** `dashboard-dev` (already created). It must contain the 6a merge:
  ```
  git fetch origin
  git log --oneline -1 origin/main          # expect ddd4f1e Merge pull request #6 ...
  git merge-base --is-ancestor ddd4f1e dashboard-dev && echo OK
  ```
  If that doesn't print OK, run `git checkout main && git pull && git checkout dashboard-dev && git merge main` first. Your local `origin/main` was still at `8e34d5b` because nothing had been fetched since the merge.
- `pytest -q` on `dashboard-dev`: 119 passed. Run it again here, before locking.
- If the 6a spot-checks (M1: restore removed → test_10c fails; M2: learn_alias moved after the rerun → an E7/Verity test fails) weren't run before the merge, run them now, read-only, and revert.
- Pinned versions: `streamlit==1.67.0`, `python-docx==1.2.0` (pandas 3.1.1 present). Paste `pip show streamlit python-docx pandas`. Pin streamlit and python-docx in `requirements.txt` in the checker commit.

### AppTest facts (checked on streamlit 1.67.0)

- `AppTest.from_file(path, *, default_timeout=...)`.
- Objects set in `at.session_state[...]` before `run()` reach the script.
- `switch_page` keeps `session_state`.
- `at.get("download_button")` gives `.key` and `.proto.disabled` but not the bytes. Monkeypatching `streamlit.download_button` before `run()` captures `data`, passed by keyword or position.
- `at.get("file_uploader")[0].upload(name, content, mime)` uploads by key. Within one tree, `upload()` appends; `set_value([...])` replaces the list (see clarification 1). The uploader keeps its files across plain reruns.
- `at.dataframe[i].value` comes back with pandas `StringDtype` columns.
- `st.markdown`, `st.subheader` and `st.caption` parse markdown. `st.text` parses neither markdown nor HTML.
- The docx styles `Title`, `Heading 1`, `List Bullet` and `Normal` exist.

## Definitions used here

- **F1** = `tests/fixtures/commentary/good.json` (stage 5 spec). Sections `portfolio` and `cobalt`, 7 references, including `ridgeline.12m.growth_12m`.
- **made-up fixture** = `tests/fixtures/commentary/made_up_number.json` (stage 5 F2).
- **E7** = the review item with `error_type == "unknown_label"`, `company` = Verity and `suggestion == "new_arr"`, found as in `tests/test_review_loop_checker.py`.
- **E6** = the Verity `impossible_value` item on `gross_margin_pct`, month 2026-06.
- **RAW** = sorted xlsx and csv paths in `data/raw/`. **RAW−R** = RAW without the Ridgeline file.
- **Non-confirmable** = every `error_type` not in `review.CONFIRMABLE` (missing_month, rollforward_mismatch, continuity_break).
- **Rendered text** = any string that came from Claude (`section.company`, paragraphs) or from a file (reasons, labels, suggestions, file names, cells, question text, sources).

## What exists today

- `app.py` and the three page files are empty.
- The backend is complete: `review.load / resolve / rerun_company / item_id / raw_label / CONFIRMABLE`, `commentary.draft_commentary` and `format_value`, the draft dataclasses, and `ingest.read_file(path) -> FileData` (with `.company`).
- The real Anthropic client exists only in `scripts/demo_commentary.py`.
- `pipeline/mapping.py` uses `lru_cache` on `load_aliases`. That's out of scope for the cache ban (D4).
- `commentary._parse` accepts any string as `section.company`.

## Design decisions

| # | Decision | Rule |
|---|---|---|
| D1 | Pages stay thin | All logic goes in `dashboard/` (pure functions, no Streamlit import) and `pipeline/llm.py`. |
| D2 | Real client | `AnthropicClient` moves to `pipeline/llm.py` (the demo script imports it from there). Pages use `session_state["claude_client"]` if set. Otherwise they build `llm.AnthropicClient()` only inside the Generate/Regenerate handler. |
| D3 | Config | `session_state["cfg"]` if set, else `review.Config.default()`. Tests always set a tmp `Config`. |
| D4 | State | The `Portfolio` lives in `session_state["portfolio"]`. No caching in the dashboard files (test 24). On a session with no portfolio, if `cfg.state_dir/uploads/` has files, the page calls `review.load(cfg, sorted upload paths)`, which replays `decisions.jsonl`. |
| D5 | Uploads | See UploadResult. Saved only to `cfg.state_dir/"uploads"/`. "Load sample data" runs `review.load(cfg)` on `cfg.data_dir` and saves nothing. |
| D6 | Reporting month | No picker. The period `p.months[0]` – `p.months[-1]` is shown as `st.text`. This is a documented deviation from the main spec. |
| D7 | Blocked metrics | Never shown as blank, 0, NaN or "None". They read `under review`. Every metrics row is in `metric_detail`. |
| D8 | Review actions | See "Widget keys and labels". Confirm only for `CONFIRMABLE`, and for `unknown_label` only if both `suggestion` and `raw_label` are non-empty. The page also wraps every `resolve` call: on `ValueError` or `KeyError` it shows `st.error("This item can't be resolved here.")`, a fixed string, and leaves `portfolio` unchanged. "Mark as question" (`resolve(..., "reject")`) appears for every item, and the item stays open. |
| D9 | No rendered text through markdown | All rendered text (definition above) is shown only with `st.text`, `st.dataframe` or `st.code`. `st.markdown`, `st.write`, `st.caption`, `st.title`, `st.header`, `st.subheader`, `st.html` and widget labels/help get **string literals only**. Paragraphs are one `st.text` each. A section heading is `st.subheader` of a **code-owned name**: "Portfolio" for `portfolio`, otherwise the full company name from `p.metrics` whose `metrics.slug` equals `section.company`. If nothing matches, the subheader is "Other" and the raw `section.company` follows as `st.text`. This replaces revision 2's `escape_md`, because escaping can't stop autolinks or `:emoji:` reliably. `raw_label`, `suggestion` and an empty `sources` show `—`. |
| D10 | Sources | Each paragraph is followed by an `st.dataframe` of its figures, one row per (figure, source): metric_id, display, source_file, source_cell. A figure with no sources gets one row with `—` for file and cell. Same layout in the docx (D14). |
| D11 | No in-app editing | The person approves or rejects in the app and edits in the exported Word file. This is a documented deviation. |
| D12 | Approvals | `dashboard.approvals.record(cfg, action, draft, p, model, fingerprint)` appends one line to `cfg.state_dir/"approvals.jsonl"`. Fields: `ts` (UTC ISO), `action` (`"approved"` or `"rejected"`), `draft_sha256`, `draft_status`, `model`, `fingerprint`, `open_question_count` = `len(draft.questions)`, `decision_count` = `len(p.decisions)`. The same fields are used for both actions. `model` = `session_state["draft_model"]`. `draft_sha256` = SHA-256 of `json.dumps([[s.company, [pa.rendered for pa in s.paragraphs]] for s in draft.sections] + [q.text for q in draft.questions])`. No paragraph, question or reason text is stored. |
| D13 | Stale drafts | `fingerprint(p)` = SHA-256 over the sorted (`metric_id`, value rounded to 6 places or `None`, `blocked_reason`) rows of `p.metrics`, **plus** the sorted open `item_id`s. It is saved at generation (`session_state["draft_fingerprint"]`). If the current fingerprint differs, the page shows a `st.warning` starting `Out of date`, and Approve and Export are disabled. A reject changes neither metrics nor open items, so it isn't stale. Closing a duplicate_submission or prompt_injection item changes only the open items, and is stale. |
| D14 | Export | `draft_to_docx(draft, p) -> bytes`. A `Title` "Board draft". Per section, a `Heading 1` with the same code-owned name as D9. Per paragraph, a `Normal` paragraph followed by its sources table (same rows as D10). Then a `Heading 1` "Open questions" and one `List Bullet` per question. The button is `st.download_button("Export to Word", data=draft_to_docx(draft, p), file_name="board_draft.docx", key="export", disabled=...)`. |
| D15 | Logs | The commentary log is `cfg.state_dir/"commentary_log.jsonl"`. |

### Session-state keys

| Key | Type | Set by |
|---|---|---|
| `cfg` | `review.Config` | tests, else `Config.default()` |
| `claude_client` | object with `.model`, `.create` | tests, else lazy (D2) |
| `portfolio` | `review.Portfolio` or absent | Upload, review actions, D4 reload |
| `draft` | `Draft` or `None` | Generate/Regenerate; Reject draft sets `None` |
| `draft_model` | str | Generate/Regenerate |
| `draft_fingerprint` | str | Generate/Regenerate |
| `approved` | bool | Approve → `True`; Generate, Regenerate and Reject draft → `False` |
| `upload_result` | `UploadResult` or absent | Upload page |

### Widget keys and labels

| Widget | Key | Label | Shown / enabled |
|---|---|---|---|
| Confirm | `confirm:<item_id>` | unknown_label → "Confirm alias"; impossible_value → "Accept as reported"; duplicate_submission, prompt_injection → "Close" | D8 |
| Mark as question | `reject:<item_id>` | "Mark as question" | every open item |
| Uploader | `uploads` | "Upload KPI files" | always (`accept_multiple_files=True`) |
| Load sample | `load_sample` | "Load sample data" | always |
| Generate | `generate` | "Generate draft" | portfolio loaded and `draft` is None |
| Regenerate | `regenerate` | "Regenerate" | `draft` is not None |
| Approve | `approve` | "Approve" | enabled only if status `ok`, not stale and not approved |
| Reject draft | `reject_draft` | "Reject draft" | `draft` is not None |
| Export | `export` | "Export to Word" | rendered whenever a draft exists; enabled only if `approved` and not stale |

### UploadResult

```python
@dataclass
class UploadResult:
    saved: list[str]
    skipped: list[str]
    refused: list[tuple[str, str]]   # (name, reason)
```

The rules are checked in this order for each file:

1. `bad_name`: the name contains `/`, `\` or `..`, starts with `.`, or is empty.
2. `bad_type`: the extension, compared case-insensitively, isn't `.xlsx` or `.csv`.
3. Same name and same bytes as an existing upload → `skipped`.
4. `name_conflict`: same name, different bytes. The old file is unchanged.
5. `duplicate_content`: a different name but the same bytes as an existing upload or an earlier file in the batch.
6. `unreadable`: the bytes are written to `cfg.state_dir/"incoming"/<name>` and `ingest.read_file` is called on that path. If it raises or returns `company is None`, the file is deleted and refused. Otherwise it is moved into `uploads/`. An unreadable file never reaches `uploads/`, so a later D4 reload can't crash on it.

The page shows each list with `st.text`. If anything was saved, it calls `review.load(cfg, sorted upload paths)`. On a rerun with the same uploader files, everything is `skipped`, and `upload_result` shows that.

## Pages

| Page | Shows | Actions |
|---|---|---|
| `app.py`: Overview | `st.dataframe(overview(p))`, `st.dataframe(metric_detail(p))`, the period as `st.text`. With no portfolio, `st.info` (literal) pointing to Upload. | none |
| `pages/1_Upload.py` | uploader, Load sample, UploadResult lists | upload, load sample |
| `pages/2_Review_queue.py` | `st.dataframe` of the queue display fields, then per item a `st.text` line and its buttons. With no portfolio, `st.info` (literal). | Confirm, Mark as question (D8) |
| `pages/3_Board_draft.py` | `st.text` status line `Draft status: <ok|rejected> · attempts <n>`. Rejected: `st.error("Draft rejected")` plus one `st.text` per reason, and no paragraphs. Ok: per section a subheader (D9), then per paragraph a `st.text` and its sources dataframe (D10). Stale warning (D13). `st.subheader("Open questions")` and one `st.text` per question. With no portfolio, `st.info` (literal). | Generate, Regenerate, Approve, Reject draft, Export |

## Pure functions (`dashboard/`)

```python
# dashboard/views.py
overview(p) -> pd.DataFrame        # company, ending_arr, growth_12m, nrr_12m, churn_rate, gross_margin, open_items, status
metric_detail(p) -> pd.DataFrame   # metric_id, display, status, sources   (one row per p.metrics row, same order)
queue_rows(p) -> list[dict]        # item_id, company, month, metric, error_type, source_file, source_cell,
                                   # reason, raw_label, suggestion, actions: list[tuple[key, label]]
section_name(section_company, p) -> str | None   # "Portfolio", a full company name, or None
sources_rows(paragraph) -> list[dict]            # D10 rows
fingerprint(p) -> str
# dashboard/uploads.py
save_uploads(cfg, files: list[tuple[str, bytes]]) -> UploadResult
# dashboard/export.py
draft_to_docx(draft, p) -> bytes
# dashboard/approvals.py
record(cfg, action, draft, p, model, fingerprint) -> dict   # the line written
```

**Overview rules.** "Latest" = `p.months[-1]`.

- `company` is the full company name, as in `p.metrics.company`.
- `ending_arr` comes from `p.table` (metric `ending_arr`, latest month), displayed as `format_value(f"{slug}.{latest}.ending_arr", v)`, which gives `$x.xM`.
- `growth_12m` and `nrr_12m` come from the `12m` rows. `churn_rate` and `gross_margin` come from the latest-month rows. All are displayed with `format_value`.
- A blocked row shows `under review`. A missing row or table value shows `—`.
- `open_items` is the count of that company's items in `p.review_queue`, as a string. `status` is `Needs review` if that count is above 0, else `Ready`.

**metric_detail rules.**

- `display` = `format_value(...)`, or `under review` if blocked.
- `status` is `under review` if blocked, `accepted as reported` if any input `(file, cell)` is the source of a `p.table` row whose `(company, month, metric)` is in `review._accepted(p.decisions)`, else `ok`.
- `sources` = `"; ".join(f"{f} {c}" for f, c in inputs)`, or `—` if `inputs` is empty. Four live blocked rows have empty inputs.

**Type rule.** Every cell `overview` and `metric_detail` return is a `str`, with no None or NaN. The dtype is not specified. Frames are compared with `a.astype(str).to_dict("records") == b.astype(str).to_dict("records")` plus equal column lists. This works for both object and pandas-3 `StringDtype` frames.

## Checker tests (`tests/test_dashboard_checker.py`, locked)

General rules:

- Counts and values come from live data (`review.load(cfg)` into `tmp_path`).
- `dashboard.*` and `pipeline.llm` are imported lazily inside tests and fixtures.
- No skip, xfail or importorskip.
- Fakes record calls and have `.model = "fake-model"`.
- AppTest uses `from_file(ROOT / <page>, default_timeout=60)` with `cfg` and `claude_client` set.
- An inline fake fixture **COBALT_ONLY** = `{"sections": [{"company": "cobalt", "paragraphs": ["Cobalt's net retention was {cobalt.12m.nrr_12m}."]}]}`.

**Pure functions**

1. **Overview.** One row per company, `company` equal to the full names in `p.metrics`. Each cell follows the overview rules, compared to `p.metrics` and `p.table` directly. `status` is `Needs review` exactly where `open_items != "0"`. Ridgeline is `Ready`. At least one cell is `under review`. Every ending-ARR cell matches `^\$\d+\.\dM$`.
2. **Metric detail.** Same rows and `metric_id` order as `p.metrics`. Every blocked row has display and status `under review`. Every non-blocked display equals `format_value`. `sources` is `—` exactly when `inputs` is empty, and at least one live row is like that. Every cell is a non-empty `str`, and none is `None`, `nan` or `NaN`.
3. **Queue rows.**
   - One row per open item, with matching `item_id`.
   - The action keys are exactly `reject:<id>`, plus `confirm:<id>` per D8.
   - E7's `raw_label` equals `review.raw_label(p, E7)`.
   - An empty `raw_label` or `suggestion` shows `—`.
   - Injected case: a copy of the Portfolio with an extra `unknown_label` item that has `suggestion=None` gets no `confirm:` action.
4. **Labels.** The Confirm label follows the D8 table for each of the four confirmable types (all occur live). Non-confirmable rows have no `confirm:` action (all three types occur live).
5. **Confirm E7.** After `resolve(E7, "confirm")`, Verity `growth_12m` in `overview` equals `format_value` of the resolved value. E7 is gone from `queue_rows`, and `metric_detail` has fewer `under review` cells.
6. **Accept E6.** Afterward, only `verity.2026-06.gross_margin` changes status, and it reads `accepted as reported` with display `format_value` of its value.
7. **Section names.**
   - `section_name("portfolio", p) == "Portfolio"`.
   - `section_name("cobalt", p)` is Cobalt's full name.
   - `section_name("**x** ![i](http://a/b.png) https://evil.example :smile:", p) is None`.
   - `sources_rows` gives one row per (figure, source), and a figure with no sources gives one `—` row.
8. **Uploads.**
   - `len(RAW)` files saved with matching bytes; saving again skips all of them.
   - `name_conflict` leaves the original unchanged.
   - A renamed copy → `duplicate_content`.
   - `.txt` → `bad_type`, while `.CSV` (a RAW CSV renamed with a new stem, so its bytes are new) is not refused as `bad_type`.
   - `../x.csv`, `a/b.csv` and `.hidden.csv` → `bad_name`.
   - A corrupt file `junk.xlsx` (random bytes) → `unreadable`, absent from `uploads/` and `incoming/`. A following `review.load(cfg, upload paths)` doesn't raise.
   - `data/raw/` is byte-identical.
9. **Export.** Parse `draft_to_docx(F1 draft, p)`:
   - one `Title`;
   - `Heading 1` texts equal `[section_name(...) for each section] + ["Open questions"]`, in order;
   - `Normal` paragraph texts equal every `rendered` paragraph, in order;
   - there is one table per paragraph, in order, and its rows equal `sources_rows(paragraph)`;
   - `List Bullet` texts equal the question texts, in order;
   - no `{` or `}` anywhere.
10. **Approvals.**
    - `record(..., "approved", ...)` then `record(..., "rejected", ...)` add exactly two lines with exactly the D12 fields.
    - `open_question_count == len(draft.questions)`, `decision_count == len(p.decisions)`, and `model` and `fingerprint` equal the arguments.
    - `draft_sha256` follows the formula and is stable.
    - No line contains a paragraph, question or reason string.
11. **Fingerprint.**
    - Stable across two loads.
    - Changes after confirming E7.
    - Unchanged after a reject on a non-confirmable item.
    - **After confirming the prompt_injection item (and separately the duplicate_submission item), `p.metrics` is equal but the fingerprint changes.**
    - Differs between a load of RAW−R and a load of RAW.

**AppTest**

12. **Every page runs.** All four pages run with no exception, with and without a portfolio. Without one, the queue and draft pages show their `st.info` and no buttons. Smoke test: one AppTest from `app.py`, `switch_page` through all pages and back, no exception, and `portfolio` is the same object.
13. **Overview wiring.** Both dataframes' `.value` equal `overview(p)` and `metric_detail(p)` under the Type rule's comparison.
14. **Review buttons.** First, build the expected Portfolio: `review.load(cfg2)` in a separate tmp state dir, then `review.resolve(..., E7, "confirm", cfg2)`. Then in the AppTest (its own `cfg`):
    - `reject:<id>` is present for each open item;
    - `confirm:<id>` is present exactly per D8, with the D8 label, and absent for non-confirmable items;
    - clicking `confirm:<E7 id>` appends one confirm line to `cfg`'s `decisions.jsonl`;
    - the session `portfolio.metrics` equals the expected metrics.
15. **Mark as question.** A click appends one reject line, and the item is still listed.
16. **Upload page.** Uploading RAW through `uploads` gives a portfolio whose metrics equal `review.load(cfg, sorted upload paths)`. On the next rerun with the same files, every name is in `skipped` and the metrics are unchanged.
17. **Generate.** With an F1 fake:
    - one call is made, and the log is in `cfg.state_dir`;
    - every rendered paragraph equals the value of a **text** element;
    - each paragraph is followed by a dataframe equal to `sources_rows`;
    - every question text is a text element;
    - the subheaders are exactly `[section_name(...)] + ["Open questions"]`.
18. **Rejected draft.** With a fake that always returns the made-up fixture:
    - the status text says `rejected`, there is an `st.error` "Draft rejected", and one text per reason;
    - no paragraph string from the fixture appears in any element value in the whole tree;
    - `approve` is disabled.
19. **Regenerate, Approve, Reject draft.**
    - Regenerate makes exactly one more call and sets `approved` to False.
    - Approve on an ok draft appends exactly one `approved` line whose `fingerprint` and `model` equal `session_state["draft_fingerprint"]` and `["draft_model"]`.
    - Reject draft appends one `rejected` line, sets `draft` to None and shows `generate`.
20. **Stale draft.**
    - (a) From `app.py`: go to the draft page, generate (F1) and approve; go to the queue page and confirm E7; go back. The `Out of date` warning is shown, and `approve` and `export` are disabled. After `regenerate`, `approve` is enabled.
    - (b) Use a COBALT_ONLY fake. Upload RAW−R and generate; assert status `ok` and `approve` enabled. Then upload RAW (the uploader replaces its list). Stale, with `approve` disabled.
    - (c) Same as (a), but click Mark as question instead of Confirm: not stale.
21. **Export button.** Monkeypatch `streamlit.download_button` to capture `data` (keyword or position) and call the original. Before Approve, `export` exists with `disabled=True`. After Approve it is enabled, and the captured bytes parse (test 9's parser) to the same content as `draft_to_docx(session draft, p)`.
22. **Untrusted text, whole tree.** Let `S = "**bold** <b>x</b> $1$ ![i](http://a/b.png) https://evil.example :smile:"`.
    - Insert into the session Portfolio a review item whose reason, source_file and suggestion contain S. Its question text then contains S through `source_file`.
    - Use a fake whose single section has `company = S` and a paragraph that is a plain sentence with one valid reference.
    - Walk every element in `at.main` and `at.sidebar` recursively on (i) the queue page and (ii) the draft page after Generate.
    - S may appear only inside text, dataframe or code element values. No markdown, subheader, caption, title, header, html, info, warning, error or toast element value contains any part of S (`**bold**`, `<b>`, `![i]`, `evil.example`, `:smile:`).
    - On the draft page, the section's subheader is "Other".

**Safety**

23. **No network, no real client.** Block `socket.socket.connect`, `socket.create_connection` and `socket.getaddrinfo` (not socket creation, which asyncio needs), plus `anthropic.Anthropic.__init__`, for the whole module. No AppTest run constructs the real client, and importing `pipeline.llm` doesn't either.
24. **Static scan.** Over `app.py`, `pages/*.py`, `dashboard/*.py` and `pipeline/llm.py`, forbid these regexes (built by concatenation in the test):
    - `st\.cache`, `cache_data`, `cache_resource`, `lru_cache`, `functools\.cache`, `import\s+cache`, `experimental_memo`, `experimental_singleton`, `unsafe_allow_html\s*=\s*True`, `st\.html\s*\(`;
    - `answer_key`, `ground_truth`, `(?<![\w.])eval/`, `\bfrom\s+eval\b`, `\bimport\s+eval\b`.
    
    `pipeline/review.py` and `scripts/demo_commentary.py` get only the last five.
25. **Tracked and default state untouched.** A module fixture snapshots `pipeline/aliases.yaml`, `data/raw/*` and `ROOT/outputs/` (recursive listing with SHA-256, or "absent"). It restores the first two in `finally` and asserts all three are unchanged.

## Checker clarifications (from the /plan session, revision 3.1)

The checker pins these. Where they differ from the test text above, they take precedence.

1. **Uploader replacement (tests 16, 20b).** In AppTest, `upload()` appends within one tree. The tests use `file_uploader.set_value([...])` to replace the list.
2. **Test 8 `.CSV`.** A renamed file keeps its bytes. The `.CSV` case runs in a fresh state dir and must land in `saved`, not `bad_type`.
3. **Docx sources tables (tests 9, 21).** The first row is the header `metric_id | display | source_file | source_cell`. The remaining rows equal `sources_rows(paragraph)`.
4. **Test 18 reasons.** For each reason, the number of text elements containing it equals the number of times the draft was rejected for it.
5. **Test 22.** Widget labels and help text are also checked; D9 requires them to be literals.
6. **Test 24.** Every scanned file must exist and be non-empty.
7. **Exact strings.** `UploadResult.refused` reasons are exactly `bad_name`, `bad_type`, `name_conflict`, `duplicate_content`, `unreadable`. The draft status line is exactly `Draft status: <status> · attempts <n>`.
8. **Lazy client (test 23).** Pages call `llm.AnthropicClient()` through the module attribute (`from pipeline import llm`), not a name imported at load time.
9. **Pages render something.** Tests 12 and 12b require each page to render at least one element, so empty files can't pass.
10. **Item count.** 35 collected items (test 12 is parametrized into 8; 12b and 20a–c add more). Before the build: 34 fail, test 25 passes (it can only fail once code writes to the wrong place). Clarifications 11–19 add a few items; the "all but test 25 fail before the build" rule still holds.

### From the checker review (revision 3.2)

11. **Test 8 file names.** `ingest._detect_company` uses the file stem plus the top cells, and the Verity CSV has no company name in its cells. Every Verity copy in test 8 that is expected in `saved` must have a stem containing `verity` (e.g. `verity_a.csv`, `verity_b.csv`). A stem without a company name is expected to be `unreadable`, which is correct behaviour.
12. **Test 3, value check.** `month` and `metric` in each `queue_rows` row equal the item's `month` and `metric` (or `—` when None).
13. **Test 22, container labels.** The walk also checks the labels of block containers (expander, tab, column, popover, form) as well as leaf nodes. No part of S may appear in any container label.
14. **Test 12c, D4 reload.** Save RAW into `cfg.state_dir/uploads/` with `save_uploads`, start a fresh AppTest with no `portfolio` in session state, and run `app.py`. The session now holds a portfolio whose metrics equal `review.load(cfg, sorted upload paths)`. Write one E7 confirm with `review.resolve` first, so the reload must also replay it (E7 is absent from the reloaded queue).
15. **Test 16b, Load sample data.** Clicking `load_sample` gives a portfolio whose metrics equal `review.load(cfg)`, and `cfg.state_dir/uploads/` is still absent or empty.
16. **Test 14b, D8 error wrapper.** Insert into the session Portfolio an `impossible_value` review item (so D8 shows its Confirm; an injected `unknown_label` would have no `raw_label`) whose `item_id` isn't in the real queue, so `resolve` raises `KeyError`. Clicking its Confirm shows `st.error` with exactly "This item can't be resolved here." and leaves `session_state["portfolio"]` the same object. `decisions.jsonl` has no new line.
17. **Test 19, double approval.** After one Approve click, `approve` is disabled. `approvals.jsonl` has exactly one `approved` line after a further rerun.
18. **Test 21, export args.** The captured call has `file_name == "board_draft.docx"` and `key == "export"`.
19. **Test 10, timestamp.** `ts` must be offset-aware UTC (`datetime.now(timezone.utc).isoformat()`). A naive timestamp fails.
20. **Test 20b, start page.** `AppTest.switch_page` resolves paths relative to the directory of the starting script, so an AppTest started from `pages/1_Upload.py` would look for `pages/pages/…`. 20b therefore starts from `app.py` (as 20a does) and reaches the pages with `switch_page("pages/1_Upload.py")` and `switch_page("pages/3_Board_draft.py")`. All assertions are unchanged.

### From the stage 6 audit (test-only fix)

21. **Test 18b, injected rejected draft.** `commentary.py` always returns empty `sections` on rejection, so test 18 can't reach the page's guard (the `draft.status == "rejected"` branch in `pages/3_Board_draft.py`). 18b injects a `Draft` with status `rejected`, attempts 3, `reject_reasons == ["made_up_number"]`, the real open questions, and one non-empty `cobalt` section whose single paragraph (template and rendered text both a distinctive sentence containing S) has no figures. It sets `draft`, `draft_model`, `draft_fingerprint` (the current one) and `approved=False`, then runs the page. It asserts "Draft rejected" is shown, the paragraph string is in no element value or container label anywhere in `main` or `sidebar`, no part of S is in any markdown-family element, and Approve and Export are disabled.

## Audit mutations (fresh session, after the loop)

| Mutation | Caught by |
|---|---|
| Blocked metric shown as "None" or blank | 1, 2 |
| Empty `sources` shown as "" | 2 |
| `metric_detail` drops blocked rows | 2, 13 |
| Confirm for missing_month | 3, 14 |
| Confirm for unknown_label without a suggestion | 3 |
| Wrong Confirm label for impossible_value | 4, 14 |
| raw_label "" instead of "—" | 3 |
| Confirm changes the session only (no decisions.jsonl line) | 14 |
| `st.cache_data` on load | 24 |
| A paragraph rendered with `st.markdown` | 17 |
| `st.subheader(section.company)` (raw) | 17, 22 |
| A reason in an `st.expander` label | 22 (clarification 13) |
| No reload from uploads/ on a fresh session | 12c |
| Load sample does nothing, or saves into uploads/ | 16b |
| `resolve` errors crash the page or change the session | 14b |
| Approve clickable twice | 19 |
| Wrong export file name | 21 |
| Wrong `month` or `metric` in queue rows | 3 |
| A reason or question rendered with `st.markdown` | 18, 22 |
| Rejected draft still shows paragraphs | 18b |
| Approve enabled on a rejected draft | 18 |
| Fingerprint omits open items | 11 |
| Stale check removed | 20a, 20b |
| Upload doesn't trigger a reload/refresh | 16, 20b |
| Approve doesn't write a line, or writes the wrong fingerprint/model | 19 |
| Export enabled before Approve | 21 |
| Export omits open questions, or uses one table per section | 9, 21 |
| Real client built at import | 23 |
| Upload overwrites a same-name file | 8 |
| Duplicate content under a new name accepted | 8 |
| Path traversal in an upload name accepted | 8 |
| Unreadable file saved to uploads/ | 8 |
| Approval line stores paragraph text, or lacks `action` | 10 |
| "accepted as reported" missing | 6 |
| Default `Config` used despite the session `cfg` | 25 |

Note: "Default `Config` used despite the session `cfg`" is caught by test 25 only when the whole module runs.

## Not in this stage

Trend charts, per-CFO question export, Notifier/Teams, Claude label matching (stage 7), in-app editing (D11), the README.

## Locking

New locked files: `tests/test_dashboard_checker.py`, `docs/stage6_dashboard_checker_spec.md`, `requirements.txt`. Add 6 deny rules (Edit and Write each) by hand, for 28 total, with names matching exactly. Check with `python -m json.tool`, confirm in a new conversation that an Edit is refused, then record `START_HASH_6`.

## /plan prompt for the checker session

```
/plan On branch dashboard-dev, write tests/test_dashboard_checker.py from docs/stage6_dashboard_checker_spec.md (tests 1–25). Read pipeline/review.py, pipeline/commentary.py, pipeline/ingest.py, tests/test_review_loop_checker.py and tests/test_commentary_checker.py first and reuse their fixtures and guards. Import dashboard.* and pipeline.llm lazily inside tests or fixtures so each test fails on its own before the build. Every count from live data, no skip/xfail/importorskip, no real API calls. Before writing, list any spec rule you can't test as written against streamlit 1.67.0 and pandas 3.1.1. Do not write dashboard code.
```

## /goal prompt for the build loop

```
/goal On branch dashboard-dev, build the Streamlit dashboard so that tests/test_dashboard_checker.py passes, following docs/stage6_dashboard_checker_spec.md: app.py, pages/1_Upload.py, pages/2_Review_queue.py, pages/3_Board_draft.py, dashboard/ (views, uploads, export, approvals), and pipeline/llm.py (move AnthropicClient there; scripts/demo_commentary.py imports it).

Rules:
- Do not edit locked files: eval/**, pipeline/contract.py, tests/test_scorer.py, tests/test_metrics_checker.py, tests/test_commentary_checker.py, tests/test_metrics.py, tests/fixtures/commentary/**, docs/stage5_checker_spec.md, .claude/settings.json, docs/stage6a_review_loop_checker_spec.md, tests/test_review_loop_checker.py, tests/test_dashboard_checker.py, docs/stage6_dashboard_checker_spec.md, requirements.txt.
- Do not change the behavior of pipeline/review.py, checks.py, metrics.py, mapping.py, ingest.py, run.py or commentary.py. Pages hold no processing logic.
- No code may read the scorer folder, answer_key or ground_truth, and no new code or comment may contain those names.
- No caching in the dashboard files. No unsafe_allow_html, no st.html. Rendered text only through st.text, st.dataframe or st.code. No skip, xfail or importorskip. No real API calls.
- Never write pipeline/aliases.yaml, data/raw/ or outputs/ from tests.
- Update CLAUDE.md (commands and modules).
- You are allowed to commit to dashboard-dev. Do not push.

Done only when the transcript contains, pasted verbatim and untrimmed from the final committed state:
1. pytest -q: all passed, 0 failed, 0 errors, 0 skipped
2. python eval/score.py --stage 3 summary: recall 7/7, Ridgeline 0 false flags
3. python eval/score.py --stage 4 summary: 250/250
4. git diff --stat <START_HASH_6> HEAD -- eval/ pipeline/contract.py tests/test_scorer.py tests/test_metrics_checker.py tests/test_commentary_checker.py tests/test_metrics.py tests/fixtures/commentary/ docs/stage5_checker_spec.md .claude/settings.json docs/stage6a_review_loop_checker_spec.md tests/test_review_loop_checker.py tests/test_dashboard_checker.py docs/stage6_dashboard_checker_spec.md requirements.txt : empty output
5. git status --porcelain: empty output
```

## Review response (revision 2 → 3)

| Review fix | Done |
|---|---|
| Test 11 and mutation table (fingerprint omits open items) | Test 11 adds the injection and duplicate closes with equal metrics; the mutation row now points at 11 |
| Test 13 dtype | Type rule drops object dtype; normalised `to_dict("records")` comparison |
| `metric_detail.sources` empty | `—`; test 2 checks it and needs one live case |
| Test 20b vacuous | COBALT_ONLY fake; asserts ok and Approve enabled before the upload |
| Test 9 / D14 table scope | One table per paragraph in both the page (D10) and docx (D14) |
| D10 autolinks and emoji | Escaping dropped: all rendered text via `st.text`/dataframe/code (D9); only literals reach markdown elements |
| section.company | Code-owned heading via `section_name`, otherwise "Other" plus `st.text`; tests 7 and 22 |
| Test 22 scope | Named pages and states; S carries autolink and emoji; mutation note fixed (S reaches questions through source_file) |
| save_uploads hardening | `bad_name`, case-insensitive type, parse in `incoming/` before saving (`unreadable`); test 8 |
| D12 counts, Approve click | Counts defined; test 19 asserts the Approve line against session values |
| Test 14 ordering | Expected Portfolio built first in a separate cfg |
| D8 unconfirmable unknown_label | Confirm hidden without suggestion/raw_label; ValueError/KeyError caught with a fixed message; test 3 injected case |
| Test 24 regexes | Whitespace-tolerant `unsafe_allow_html`; `st.html` added; demo script added to the reduced scan |
| Test 23 sockets | Block connect/create_connection/getaddrinfo, not creation |
| Prerequisites | Branch `dashboard-dev`; fetch and ancestor check; re-run pytest |
| Company cell content (test 1) | Full company name |
| Test 12 no-portfolio | Each page shows a literal `st.info` |
