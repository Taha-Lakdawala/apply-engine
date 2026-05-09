# resolver.py — deep-dive

Deterministic answer resolution: preset → profile lookup → salary PPP → optional-website skip → stored answer. Decides what reaches the AI.

> **Self-update reminder:** edit this doc whenever you change the resolution order, add a new branch in `try_known_resolve`, modify `PROFILE_MAPPINGS`, change the salary table, or alter `normalize_question`. The resolution-order summary in [../CLAUDE.md](../CLAUDE.md) is intentionally one line — only update it if a *new branch* is added or removed.

## Public surface

### `ResolvedAnswer` (dataclass)

```python
@dataclass
class ResolvedAnswer:
    value: str
    source: str        # "preset" | "profile" | "stored" | "ai"
    question_id: int
    answer_id: int     # 0 means "no answer row was inserted" (AI returned empty)
```

### `try_known_resolve(field, profile, source_url, job_location=None) -> tuple[int, ResolvedAnswer | None]`

The main entry point used by `runner.py`. Always upserts the question (so `question_id` is valid even when no answer is found) and returns `(question_id, ResolvedAnswer | None)`. `None` means "fall through to AI".

Resolution order inside the function (each step that matches **also inserts an answer row** unless noted):

1. **Preset** — `profile.preset_answer(field.question)` checks `profile.yaml`'s `preset_answers:` block via `normalize_question` (case-insensitive, non-alphanumerics → spaces). On match, inserts a manual answer (`ai_generated=False`) and returns `source="preset"`.

2. **Profile lookup** — `_profile_lookup(field.question, profile, field.required)` walks `PROFILE_MAPPINGS` (regex → dotted profile path) and synthesises `Full Name` from first+last when needed.
   - **Constraint check for select fields:** if `field.options` is non-empty AND the looked-up value isn't an exact (case-insensitive) match for one of the options, the value is discarded and resolution falls through. This is why "Country" → `location.country` only fires if the country dropdown contains the exact string.
   - **Optional website skip:** if the matched path is `links.website` and `field.required` is False, returns `None` (don't volunteer a personal site on forms that don't ask for it).
   - On accept, inserts answer with `source="profile"`.

3. **Salary** — `_is_salary_question(field.question)` runs `_SALARY_RE` against the normalized question. If matched:
   - **Free-text field** (no options, not a select-type): `_compute_salary(job_location)` returns a deterministic range string. On match, inserts and returns `source="preset"`. If the location doesn't match any country in `_PPP_TABLE`, falls through to stored/AI.
   - **Select / searchable_select / has options:** **returns `(question_id, None)` immediately**, bypassing stored answers. AI must pick from the dropdown options because the deterministic value won't match exactly.

4. **Optional-website short-circuit** — if `not field.required` and the question matches `_WEBSITE_RE` (`^(personal\s*)?website$|portfolio`), returns `(question_id, None)` *before* the stored-answer step. Stops a value previously stored when the same question was required on another form from resurfacing on optional ones. The AI's system prompt also forbids filling website on optional fields, so the field ends up empty.

5. **Stored** — `db.latest_answer(conn, question.id)`. If present, returns `source="stored"`. The answer's `value` is reused as-is.

6. Otherwise returns `(question_id, None)` → AI.

The whole function runs inside one `with db.connect() as conn:` block.

### `get_prior_qa() -> list[tuple[str, str]]`

Returns every stored Q&A pair as `(question_raw_text, answer_value)`. Wraps `db.all_qa_pairs(conn)`. Used by `runner.py` to build the "prior QA for tone reference" block in the AI batch call.

### `store_ai_answer(question_id, value, source_url) -> ResolvedAnswer`

Inserts an `ai_generated=True` answer row. Returns a `ResolvedAnswer(source="ai", ...)`. Called by `runner.py` after the AI batch returns a non-empty value.

### `normalize_question(text) -> str`

Lowercase, non-alphanumeric → space, collapse whitespace, strip. Used as the SQLite fingerprint and in preset matching.

**Critical:** changing this function invalidates every existing `questions.fingerprint` in the DB. Same fingerprint = same row = answer reuse. If you must change it, plan a migration.

## Constants and tables

### `_NORMALIZE_RE`

`re.compile(r"[^a-z0-9 ]+")` — used by `normalize_question`.

### `_SALARY_RE`

```python
re.compile(
    r"\b(salary|compensation|comp|pay|remuneration|ctc|ectc|package|stipend|wage|cost\s+to\s+company)\b"
)
```

Detects salary/compensation questions of any kind. Includes `ectc`, `cost to company` (Indian forms), and bare `comp` (e.g. "Expected Comp" — `\bcomp\b` won't match inside `compensation` because of the trailing word boundary). Note this catches *all* salary-related fields including current and expected — the more specific current-comp mapping in `PROFILE_MAPPINGS` runs first (step 2) and only fires when "expected" isn't in the question (negative lookbehind).

### `_INDIA_LOC_RE`

```python
re.compile(r"\b(india|bengaluru|bangalore|mumbai|delhi|hyderabad|chennai|pune|kolkata|noida|gurugram|gurgaon)\b", re.I)
```

If the job location matches India, salary is a literal `"₹30,00,000 to ₹40,00,000 per annum"` — no PPP conversion.

### `_PPP_TABLE`

List of `(location_regex, currency_prefix, ppp_factor, round_to)` tuples. Order is significant — first match wins.

```
USA → "$"     × 1.00, round to 5_000
UK  → "£"     × 0.70, round to 2_000
CA  → "CAD "  × 1.30, round to 5_000
AU  → "AUD "  × 1.50, round to 5_000
SG  → "SGD "  × 1.30, round to 5_000
EU  → "€"     × 0.75, round to 5_000  (matches "germany|france|netherlands|spain|italy|amsterdam|berlin|paris|europe|eurozone")
UAE → "AED "  × 3.67, round to 5_000
JP  → "¥"     × 160.0, round to 500_000
```

### Conversion math

```
_INTL_LOWER = 30_00_000 / 25 = 120_000   # international dollars
_INTL_UPPER = 40_00_000 / 25 = 160_000
_PPP_DISCOUNT = 0.90
```

Per-country: `lo = round(120_000 * factor * 0.90 / step) * step`; `hi = round(160_000 * factor * 0.90 / step) * step`. Output `f"{prefix}{lo:,} to {prefix}{hi:,} per annum"`.

### `PROFILE_MAPPINGS`

Each tuple is `(regex, "dotted.profile.path")`. Tested against the **normalized** question (lowercased, no special chars). First match wins.

Current entries (paraphrased — see source for exact regexes):
- Names: first/last/preferred/full/email/phone/pronouns/pronunciation
- Location: country, city, state, postal_code/zip/pincode, address_line1
- Links: linkedin, github, website/portfolio, twitter
- Education: gpa/cgpa, school/university, degree, field of study/major, start year, graduation year
- Compensation: `current ctc` / `cost to company` / `current {compensation|salary|comp|package|pay|remuneration|wage}` (each with negative lookbehind for "expected")

The **`Full Name` / `Name`** case is special: there's no profile key for `full_name`, so `_profile_lookup` synthesises it as `f"{first_name} {last_name}".strip()`.

## Private helpers

### `_compute_salary(job_location) -> str | None`

Empty/missing location → India default. India regex match → India default. Otherwise walks `_PPP_TABLE` and applies the math. Returns `None` for "I have no idea what country this is" — caller falls through to stored/AI.

### `_profile_lookup(question, profile, required) -> str | None`

Normalises the question, walks `PROFILE_MAPPINGS`, calls `_resolve_path` to dig into `profile.data` by dotted path. Returns the value as `str(value).strip()` or `None`. Synthesises `Full Name` last. The `required` flag is used to suppress the `links.website` path when the field is optional (returns `None` for that mapping only).

### `_resolve_path(data, path) -> Any`

Splits dotted path, walks dict layers, returns `None` on any missing key or non-dict layer. Treats `""` and `None` equivalently (both → `None`).

### `_matches_option(value, options) -> bool`

Case-insensitive exact match against any option in the list. Used to gate the profile-lookup branch for select fields.

### `_is_salary_question(question) -> bool`

`bool(_SALARY_RE.search(normalize_question(question)))`.

### `_is_website_question(question) -> bool`

`bool(_WEBSITE_RE.search(normalize_question(question)))`. Used to short-circuit resolution for optional website/portfolio fields before the stored-answer step.

## Common edits

- **Map a new question label to a profile field:** add a `(regex, "dotted.path")` tuple to `PROFILE_MAPPINGS`. Match against the **normalized** form (lowercased, alphanumeric+space only).
- **Add a country to PPP:** append `(re.compile(r"\b...", re.I), "<prefix>", <factor>, <round_step>)` to `_PPP_TABLE`. Place narrow patterns before broader ones — first match wins.
- **Add a new deterministic short-circuit:** write a `_compute_<thing>(question, profile, job_location)` helper, then add a branch in `try_known_resolve` between the existing steps. Don't add field-specific logic in `runner.py`.
- **Tweak salary target:** edit `_INDIA_LOWER` / `_INDIA_UPPER` constants. The PPP math derives everything else.
- **Change the "expected" exclusion:** the negative lookbehind on the current-CTC regex is `(?<!expected\s)\b(current\s+)?ctc\b`. To exclude another modifier, extend it.

## Gotchas

- **`normalize_question` changes break the DB.** Same input → different fingerprint → answers don't reuse. Treat changes as a migration.
- **Profile lookup is gated by option matching for select fields.** If the user's `location.country` is `"India"` but the dropdown has `"India (Republic)"`, the lookup falls through. AI sees the dropdown options and picks correctly.
- **Salary on select fields skips the stored cache too.** Logic at `is_select_type` branch returns `None` directly so AI re-picks every time. This is intentional — different forms have different option sets.
- **Optional `website` / `portfolio` fields are intentionally left blank.** Both the profile-lookup branch and the stored-answer branch are gated on `field.required`. The AI system prompt also forbids volunteering a website on optional fields, so the value ends up empty in the form. To force-fill anyway, mark the field required upstream or add an entry to `preset_answers:` (preset wins over the gate).
- **`PROFILE_MAPPINGS` is regex-on-normalized-text.** `[^a-z0-9 ]+` is collapsed to space, so don't put punctuation in your regex (it'll never match).
- **`_compute_salary` returns the literal string with the rupee symbol** — it does not pass through the AI character sanitiser. The form filler types it as-is. Greenhouse text inputs handle this fine.
- **Empty AI responses are not cached.** `runner.py` builds a `ResolvedAnswer(value="", source="ai", question_id=qid, answer_id=0)` for empty responses and never inserts into the DB — so the next run can re-ask.
- **The `compensation.current_ctc` mappings** all use negative lookbehind `(?<!expected\s)`. If a question reads "What is your current CTC and expected CTC?", the mapping fires (because "expected" doesn't immediately precede "ctc"). Probably acceptable — answers two-question prompts with the current value.
- **`comp` alone vs `compensation`.** `_SALARY_RE` lists both. `\bcomp\b` only matches the bare word ("Expected Comp", "Comp Range") — it can't match inside `compensation` because the trailing `e` blocks the word boundary. The current-comp `PROFILE_MAPPINGS` regex covers the `current` cases first, so a bare "comp" question goes through `_compute_salary` (treated as expected).
