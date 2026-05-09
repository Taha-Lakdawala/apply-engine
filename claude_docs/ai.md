# ai.py — deep-dive

241 lines. Gemini client, system prompt, batch field answerer, cover-letter generator.

> **Self-update reminder:** edit this doc whenever you change `SYSTEM_PROMPT`, the batch-call interface, the cover-letter prompt, or response parsing. The system prompt is the source of truth for AI behavior on edge cases — when you change it, mirror the change here so future-you can find the rule without rereading the whole prompt.

## Provider

Uses `google.genai` (`google-genai` package). Default model `gemini-2.5-flash`, overridable via `APPLY_ENGINE_MODEL` env var (read in [config.py](../apply_engine/config.py)).

**Not Claude.** The `Profile.as_context()` docstring still says "Claude" — that's stale. Don't switch providers unless explicitly asked.

## Public surface

### `FieldSpec` (dataclass)

```python
@dataclass
class FieldSpec:
    question: str
    field_type: str          # text|textarea|select|multiselect|radio|checkbox|file|email|phone|url|number|searchable_select|date
    options: list[str] | None
    required: bool
    max_length: int | None
```

Built from a `greenhouse.Field` via `Field.to_field_spec()`. The AI sees this shape per field.

### `answer_fields_batch(fields_with_ids, profile_context, known_qa, job_location=None, job_url=None, form_order=None) -> dict[str, str]`

Single Gemini call covering many fields. Inputs:

- `fields_with_ids: list[tuple[str, FieldSpec]]` — pairs of opaque field id (from `Field.key`) and spec.
- `profile_context: str` — `Profile.as_context()` blob (yaml-serialised profile + resume text).
- `known_qa: list[tuple[str, str]]` — prior Q&A from `db.all_qa_pairs(...)` for tone/voice reference. Capped at 40 items, each answer truncated to 300 chars.
- `job_location: str | None` — passed in the prompt; informs salary PPP and work-auth logic.
- `job_url: str | None` — passed in the prompt; the AI uses it to infer country when location is missing.
- `form_order: list[tuple[str, str | None]] | None` — every form field in page order as `(label, resolved_value_or_None)`. Lets the AI infer section context for ambiguous fields (e.g. "Start year" → education vs work experience).

**Returns `{field_id: value_string}`.** Multiselect/checkbox values come back JSON-encoded (`'["A","B"]'`); single-string values are plain strings. Unknown / null answers become `""`.

Empty input → returns `{}` immediately (no API call).

Internal flow:
1. Build the system instruction (constant `SYSTEM_PROMPT`).
2. Construct `user_text` with profile context, location/URL, optional form_order block, field blocks, and the prior-QA block.
3. Call `client.models.generate_content(model=GEMINI_MODEL, contents=user_text, config=GenerateContentConfig(system_instruction=SYSTEM_PROMPT, response_mime_type="application/json", temperature=0.3))`.
4. Parse via `_parse_batch_response`.

Called up to **three times per application** in `runner.py`: phase 2 (initial fields), late-discovery, conditional-fields.

### `generate_cover_letter(profile_context, job_title, company, job_description) -> str`

Generates a plain-text cover letter. Used by `runner.py` only when the form has a *required* file field that matches `_is_cover_letter_field` (regex `cover.?letter` on the label). Optional cover-letter fields are skipped.

Prompt:
- Profile context (yaml + resume)
- Job title, company, first 3000 chars of job description
- Strict requirements: 3-4 paragraphs, <350 words, opening hook → 2-3 relevant experiences → closing CTA, "Hiring Manager" salutation, candidate's full name signoff, plain text only.

Config: `temperature=0.4`, system instruction asks for first-person plain text.

Output is sanitised through `_sanitize` (replaces curly quotes, em/en dashes, ellipses, NBSPs with ASCII equivalents — keeps the FPDF Helvetica font happy).

### `SYSTEM_PROMPT` (constant string)

The biggest piece of behavior in this file. Source of truth for AI edge cases. Sections (paraphrased):

- **Output format:** JSON only, match schema, answer in candidate voice.
- **Constraints:** stay under `max_length`, pick from `options` verbatim, return list for multiselect.
- **EEO defaults:** Hispanic/Latino → "No" (not "Decline") unless profile says otherwise. Race → from profile or "Decline to self-identify" if truly unknown.
- **"How did you hear about this role?":** "LinkedIn" if no preference set.
- **Conciseness:** keep prose answers short unless the question invites depth.
- **No fabrication:** never invent jobs/education/credentials not in resume/profile.
- **Optional website / portfolio:** only fill when `required: True`. If the field is optional, return "" even if the profile has a website value. Belt-and-braces with the resolver's optional-website short-circuit ([resolver.md](resolver.md)) — that handles the deterministic path; this rule covers the AI path.
- **Work authorization:** the `work_authorization` keys describe the candidate's status in *foreign* countries. If the job country == candidate's `location.country`, always answer "Yes" eligible / "No" sponsorship regardless of those keys.
- **Notice period:** exactly **60 days = 2 months = 8 weeks**. Prefer literal/exact-match options. Avoid open-ended options like "60+ days", "More than 60 days" — they overstate. If only open-ended remains and bounded options understate, pick the next-lower bounded ("45 days") rather than the open-ended one. Slight understatement > signaling longer than actual.
- **Salary:** India target is **₹30,00,000–₹40,00,000** (30–40 LPA). For select fields, pick the option whose range best covers the target after PPP. For free-text fields, convert via PPP — divide ₹ by 25 to get $120,000–$160,000 international, multiply by target country PPP factor, apply 10% discount. Reference factors: US ~1.0 USD, UK ~0.70 GBP, EU ~0.75 EUR, CA ~1.30 CAD, AU ~1.50 AUD, SG ~1.30 SGD. Infer country only from explicit URL/location signals — never from company name. No-signal fallback: ₹30,00,000 to ₹40,00,000.
- **Years-of-experience (any skill/domain):** anchor on **4 years exactly**. For bucketed selects, pick the option that *contains* 4 (e.g. "3-5", "4-6", "2-5"). Never return a label like "4+ years" that isn't in `options` verbatim. For free-text/number, return "4". This rule was added because the AI was returning "4+ years" literally, which never matched dropdown buckets like "3-5 years".

**Edits to AI behavior go here**, not into post-processing in `runner.py`.

## Private helpers

### `_get_client() -> genai.Client`

Lazy-init singleton. Reads `GEMINI_API_KEY` from env (constant `config.GEMINI_API_KEY_ENV`). Raises `RuntimeError` with a friendly message if unset.

### `_sanitize(text) -> str`

Translates curly quotes / em dashes / ellipsis / NBSP / bullet to ASCII via a `str.maketrans` table (`_AI_CHAR_MAP`). Applied to:
- Cover-letter output.
- Each value in the batch response.

This exists primarily because FPDF's built-in fonts can't render non-ASCII glyphs.

### `_format_field_block(fid, spec) -> str`

Renders one field as:
```
- id=<fid>
  question: <text>
  type: <field_type>
  required: <bool>
  max_length: <int or 'no limit'>
  options: <json.dumps(options) or 'none'>
```

### `_parse_batch_response(raw) -> dict[str, str]`

Defensive JSON parser:
1. Strip `\`\`\`` fences if present (some models wrap responses despite `response_mime_type="application/json"`).
2. Strip a leading `json` token after the fence.
3. `json.loads`. On failure, return `{}` (the caller treats `""` answers as "skip and try again next run").
4. For each key/value: lists become `json.dumps(list)`, `None` becomes `""`, strings get `_sanitize`'d.

### `_format_known_qa(known_qa) -> str`

Renders prior Q&A as `Q: ...\nA: ...\n\n` blocks. Caps at 40 items, truncates each answer at 300 chars with `…`.

## Common edits

- **Tweak AI behavior on an edge case:** edit `SYSTEM_PROMPT`. Mirror the rule in this doc.
- **Change the model:** set `APPLY_ENGINE_MODEL` env var. No code change needed.
- **Change temperature or response format:** edit `GenerateContentConfig` in `answer_fields_batch` / `generate_cover_letter`.
- **Add a new structured field in the prompt:** add to `_format_field_block` and update the schema description in the user-text template inside `answer_fields_batch`.
- **Adjust prior-QA cap:** edit the `[:40]` slice or `300` truncation in `_format_known_qa`.
- **Add another sanitiser char:** add to `_AI_CHAR_MAP`.

## Gotchas

- **Empty `fields_with_ids` short-circuits.** Don't add side effects above the `if not fields_with_ids: return {}` guard.
- **`response_mime_type="application/json"`** is set, but `_parse_batch_response` still strips fences defensively. Don't remove the fence-stripping unless you're sure the SDK guarantees clean JSON.
- **Multiselect values come back as JSON-encoded strings**, not Python lists. The fill code in `greenhouse.fill_field` parses `value.strip().startswith("[")` to detect this.
- **No retry/backoff** is added on rate limits — the user's standing preference is to switch model/provider rather than back off. Don't add retry layers.
- **`form_order` is optional** but powerful — passing it improves accuracy for ambiguous label collisions ("Start year" appearing in both education and work-experience sections). `runner.py` passes it on the initial batch but **not** on the late-discovery / conditional batches.
- **`SYSTEM_PROMPT` references `location.country`** in the profile — that's a `profile.yaml` key. Adding/renaming it would break the work-auth logic.
- **`_AI_CHAR_MAP` exists primarily for the cover-letter PDF.** Removing sanitisation would still produce correct text but FPDF would fail when writing certain glyphs.
