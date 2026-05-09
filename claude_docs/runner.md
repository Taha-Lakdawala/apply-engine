# runner.py — deep-dive

503 lines. Orchestrates one application end-to-end: open page → extract fields → resolve deterministically → batch AI → fill → late/conditional re-extraction → submit → handle email security code → record outcome. **Read this when changing the application pipeline, the submit handling, or the security-code flow.**

> **Self-update reminder:** edit this doc whenever you change the phase order, add or remove an extraction pass, alter submit handling, or change how `RunReport` / `FillResult` are populated.

## Public surface

### `apply_to_url(url, profile, *, headless=False, submit=True, manual_submit=False) -> RunReport`

Single application end-to-end. Called by `cli.apply_cmd`. All keyword args after `url`/`profile`. Returns a populated `RunReport`.

### `FillResult` (dataclass)

```python
@dataclass
class FillResult:
    label: str
    type: str
    value: str
    source: str        # "preset" | "profile" | "stored" | "ai" | "skipped"
    error: str | None = None
```

One per non-file field that the runner attempted to fill (or skip). File fields contribute `source="preset"` on upload or `source="skipped"` otherwise.

### `RunReport` (dataclass)

```python
@dataclass
class RunReport:
    url: str
    company: str | None
    job_title: str | None
    fields_filled: list[FillResult] = []
    submitted: bool = False
    error: str | None = None
```

The CLI only prints to console; the report exists for programmatic callers and tests.

## Pipeline (the main flow)

`apply_to_url` is one big function with named phases.

### Init
- `db.init_db()` (idempotent).
- `greenhouse.with_browser(headless=...)` returns `(pw, None, page_factory, cleanup)`. The second slot is `None` because we use a `launch_persistent_context` (no separate browser handle).
- Create `RunReport(url=url, company=None, job_title=None)`.

### Phase 1 — extract + deterministic resolution
- `greenhouse.open_application(page_factory, url)` — see [greenhouse.md](greenhouse.md#open_application). Returns `(page, fields, meta)`. Sets `report.company`, `report.job_title`.
- `greenhouse.extract_job_description(page)` — used later for cover-letter generation.
- For each non-file field, call `resolver.try_known_resolve(...)`:
  - On match (preset/profile/salary/stored): store the `ResolvedAnswer` in the `resolved: dict[key, ResolvedAnswer]` dict.
  - On miss: append `(field, question_id)` to `unknowns`.

### Phase 2 — batch AI for unknowns

Only runs if `unknowns` is non-empty.
- Print `"Calling Gemini for N unknown field(s)..."`.
- Build `prior_qa = resolver.get_prior_qa()` (every stored Q&A as tone reference).
- Build `specs = [(f.key, f.to_field_spec()) for f, _ in unknowns]`.
- Build `form_order` — `(label, resolved_value_or_None)` for every non-file field already resolved or about to be asked. Lets the AI infer section context (e.g. distinguishing education-section "Start year" from work-experience "Start year").
- `ai_answers = ai.answer_fields_batch(specs, profile.as_context(), prior_qa, job_location=meta.location, job_url=url, form_order=form_order)`.
- For each unknown: if `value.strip()` is non-empty, `resolver.store_ai_answer(...)` inserts the answer row and returns a `ResolvedAnswer`. Empty values get a placeholder `ResolvedAnswer(value="", source="ai", question_id=qid, answer_id=0)` — **not persisted**, so next run can re-ask.

### Phase 2b — cover letter (conditional)

Iterates `fields`. If any **required** file field has a label matching `_is_cover_letter_field` (regex `cover.?letter`), generates a cover letter via `ai.generate_cover_letter(profile.as_context(), meta.title, meta.company, job_description)` and writes a PDF to `data/cover_letter_<unix_ts>.pdf` via `_write_cover_letter_pdf`. Skipped entirely if no such field exists. Optional cover-letter file fields are also skipped (only required ones trigger generation).

### Phase 3 — fill the form

Iterates `fields` once, with a randomised `page.wait_for_timeout(150-450)` between each (looks human to reCAPTCHA scorers).

For **file fields:**
- Tracks `resume_uploaded: bool`. Only the first matching file field gets `resume_path`; subsequent generic file fields get `resume_path=None` and are skipped automatically (`fill_field` returns `"skipped"` in that case).
- Cover letter (if generated and the field matches `_is_cover_letter_field`) is uploaded via `cover_letter_path`.
- On `"uploaded"` outcome: append `FillResult(... source="preset", value=<filename>)`. On `"skipped"`: append `FillResult(... source="skipped", error="no resume-like label match")`.

For **non-file fields:**
- `greenhouse.fill_field(page, f, ans.value, resume_path=profile.resume_path, cover_letter_path=cover_letter_path)`.
- Append `FillResult(... source=ans.source, value=ans.value)`.
- Print one console line per field with a coloured tag matching the source.
- On exception: append `FillResult(source="skipped", error=str(e))` and print `[red]skip[/red]`.

### Post-fill checkbox re-verify

After phase 3, walks every checkbox `Field`. For each whose answer is truthy (`greenhouse._is_truthy(ans.value)`), calls `greenhouse._force_check(page, ..., True)` again. Reason: React re-renders during fill can silently uncheck a previously-checked box. Failures are silently swallowed (best-effort).

### Phase 3b — late discovery pass

EEO sections and custom dropdowns sometimes load only after earlier fields are filled.
- Build `existing_labels = {f.label.strip().lower() for f in fields}`.
- `late_fields = greenhouse._extract_custom_selects(page, existing_labels=existing_labels)`.
- If non-empty: re-resolve via `resolver.try_known_resolve`, batch AI for unknowns (no `form_order` passed this time), then call `greenhouse.fill_field` per field.

### Phase 3c — conditional pass

After 800ms wait (lets dependent UIs render):
- `conditional_fields = greenhouse.extract_new_standard_fields(page, existing_labels=...)` — re-runs the standard extractor; tagged elements are skipped.
- `+= greenhouse._extract_custom_selects(page, existing_labels=...)`.
- Same resolve → AI → fill cycle. **File fields are explicitly skipped** in this phase (you don't suddenly get a new resume slot mid-fill).

`existing_labels` for this pass is the union of original `fields` labels + `late_fields` labels.

### Phase 4 — submit (or skip)

Two short-circuits first:
- `if submit and not fields:` → `report.error = "no application fields detected"`. No submit attempt.
- `if not submit:` → print "Skipping submit (--no-submit)".

Otherwise:
1. Take a `pre_submit_<unix>.png` full-page screenshot to `data/`.
2. `page.wait_for_timeout(800)`.
3. `status = greenhouse.submit(page)` → `"verified"` | `"code_required"` | `"blocked"` | `"invalid"` | `"unverified"`.

#### `code_required` flow

Greenhouse can require an emailed security code as a second step.
- `find_security_code_field(page, wait_ms=3000)` to get the input selector.
- If found: `code = _wait_for_security_code(profile, started_at)` — see below.
- If a code arrives:
  - Click the input, clear it, **type** the code with `delay=70` (NOT `fill()`, which doesn't trigger React's per-keystroke `onChange` so the submit button stays disabled).
  - Press `Tab` (blur fires onBlur validation).
  - `page.wait_for_function` polls for the submit button to leave its disabled state, 10s timeout. On timeout: print "Code may have been rejected" but continue.
  - Take another screenshot `submit_pre_<unix>.png`.
  - Re-call `greenhouse.submit(page)` → updated status.

#### Final status handling

After whatever submit attempts, take a `submit_<unix>.png` screenshot and log the post-submit URL + first 300 chars of body text to console (debug output for unknown states).

Branch on status:
- `"verified"` → `report.submitted = True`, green "Submitted (verified)".
- `"blocked"` → red message, suggest re-running (persistent profile warms over time).
- `"code_required"` (still!) → yellow "Security-code step detected but didn't complete".
- `"invalid"` → call `greenhouse.find_form_errors(page)`, print each error, set `report.error` to a "; "-joined string.
- **`"unverified"` (default fallthrough)** → re-check `find_form_errors` (errors might have appeared later). If errors found: same as `"invalid"`. Otherwise: yellow "no confirmation marker" + `report.submitted = True` (treats unverified as optimistic success — see Gotchas).

Then `page.wait_for_timeout(3000)`.

### Phase 5 — record application

Inside `with db.connect() as conn:`:
- Determine `final_status`: `"submitted"` if `report.submitted`, `"failed"` if `report.error`, else `"filled"`.
- `db.record_application(conn, url=url, company=meta.company, job_title=meta.title, status=final_status, error=report.error)`.

### Exception handling

The whole body is wrapped in `try ... except Exception as e:`:
- Sets `report.error = str(e)`, prints "[red]Error: {e}[/red]".
- Records a `status="failed"` application row with the error.

`finally` always calls `cleanup()` to close the browser.

## Helpers

### `_wait_for_security_code(profile, started_at, timeout_seconds=600) -> str | None`

Three sources, tried in order, **first one wins**:

1. **IMAP** — if `profile.data["personal"]["email"]` ends with `@gmail.com` AND `GMAIL_APP_PASSWORD` env var is set: calls `email_fetcher.fetch_security_code(...)` with a 90s window.
2. **File** — `data/security_code.txt`. The file is `unlink(missing_ok=True)`'d at start to avoid stale codes. The loop reads the file if it exists, deletes it, returns the contents.
3. **Stdin** — only if `sys.stdin.isatty()`. Prompts via `console.input("Security code: ")`. EOFError/KeyboardInterrupt return `None`.

Loop polls every 2s until `deadline = time.time() + timeout_seconds` (default 600s = 10 min).

### `_redact(code) -> str`

Logs codes as `XX***YY` (or `X***Y` for short codes). Used in the IMAP success message.

### `_truncate(s, n=70) -> str`

Console-display truncation for `FillResult` value lines (newlines→spaces, ellipsis if longer).

### `_write_cover_letter_pdf(text, path) -> None`

Uses `fpdf2`'s `FPDF` (A4, 25pt margins, Helvetica 11pt). Treats blank lines as paragraph breaks (`pdf.ln(4)`), each non-empty paragraph rendered with `multi_cell(0, 6, ...)` + `pdf.ln(1)`. Plain text only — Helvetica is the default ASCII font, which is why `ai._sanitize` strips curly quotes etc. before this point.

## Console output (Rich)

Source colour map:
- `ai` → yellow
- `stored` → green
- `preset` → blue
- `profile` → magenta
- file uploads → cyan
- skipped → dim
- errors → red

Used in two places: phase 3 fill loop and the late/conditional fill loops. Same map duplicated in three places — if you change colours, update all three (or refactor to a constant).

## Common edits

- **Add another extraction pass:** mimic phase 3b/3c — call extraction, resolve, batch AI (optional), fill. Update `existing_labels` so subsequent passes don't re-detect.
- **Change submit retry:** edit the `code_required` branch. Don't add general retries on `unverified`/`invalid` — those should surface to the user.
- **Add a new submit status:** return it from `greenhouse.submit`. Add a branch in the status handling block. Update the [../CLAUDE.md](../CLAUDE.md) submit-status enum line.
- **Stop treating `unverified` as success:** flip the `report.submitted = True` line in the fallthrough branch. Trade-off: confirms that don't show standard markers will be flagged as failures.
- **Change cover-letter trigger:** edit the loop that sets `cover_letter_path`. Currently fires only on **required** file fields. Removing the `f.required` check generates a cover letter for any cover-letter slot, including optional ones.
- **Adjust security-code timeout:** the default is 600s (10 min). Long because Greenhouse emails can lag. Change in `_wait_for_security_code(timeout_seconds=...)`.

## Gotchas

- **`unverified` counts as submitted.** This is optimistic — some Greenhouse confirmations don't match the success-marker regex. If you discover a real submit failure being reported as success, tighten `verify_submission` in `greenhouse.py` rather than flipping the runner default.
- **Phase 3 fill is sequential, not parallel.** Per-field `wait_for_timeout(150-450)` is intentional for reCAPTCHA scoring. Don't parallelise.
- **Late/conditional passes don't pass `form_order` to AI.** The AI gets less context for these. If accuracy on late EEO fields is poor, consider passing form_order here too.
- **The first matching file field gets the resume.** Greenhouse always lists resume first in the file order, so this is fine in practice. If a form puts cover-letter before resume, it'd misroute — `_is_cover_letter_field` short-circuits to cover letter so this is mostly handled.
- **Three separate places set `resolved[f.key] = ResolvedAnswer(value="", source="ai", question_id=qid, answer_id=0)`** for empty AI responses (phase 2, late, conditional). If you change the empty-cache behavior, update all three.
- **`_wait_for_security_code` deletes `security_code.txt` at start.** Old codes don't bleed across runs.
- **Cover-letter PDF uses Helvetica** which doesn't support non-Latin scripts. If the candidate name is non-ASCII, `ai._sanitize` doesn't catch it — the PDF render will fail.
- **The screenshots all live in `data/`** with `unix_timestamp` in the name. They accumulate over time. There's no cleanup.
- **`greenhouse.has_recaptcha` exists** but isn't called anywhere in `runner.py`. If you want pre-flight reCAPTCHA detection, hook it in before submit.
