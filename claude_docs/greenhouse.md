# greenhouse.py — deep-dive

1864 lines. All Playwright/DOM logic — extraction, filling, submit, security-code detection, browser launching. The single biggest file in the project. **Read this when changing extraction, fill behavior, the `data-ae-key` contract, submit handling, or browser stealth.**

> **Self-update reminder:** this file changes more than any other. Edit this doc whenever you add/remove a function, add a `Field.type`, change extraction strategies, modify the submit status enum, or alter the `data-ae-key` contract. Cross-cutting changes (new field type, new submit status, broken tagging contract) should also be reflected in [../CLAUDE.md](../CLAUDE.md).

---

## Table of contents

- [DOM tagging contract](#dom-tagging-contract) — the `data-ae-key` invariant. Read first.
- [Dataclasses](#dataclasses) — `Field`, `PageMeta`.
- [Constants](#constants) — `SEARCHABLE_THRESHOLD`.
- [Embedded JS strings](#embedded-js-strings) — what each one does and why.
- [Extraction pipeline](#extraction-pipeline) — `open_application` and friends.
- [Custom select widgets](#custom-select-widgets) — three-strategy detection.
- [Fill dispatch](#fill-dispatch) — `fill_field` per type.
- [Combobox / dropdown helpers](#combobox--dropdown-helpers).
- [Location handling](#location-handling) — autocomplete typeaheads.
- [Security code detection](#security-code-detection).
- [Submit + verify](#submit--verify).
- [Form errors](#form-errors).
- [Conditional re-extraction](#conditional-re-extraction).
- [Browser launcher + stealth](#browser-launcher--stealth).
- [Common edits](#common-edits) and [Gotchas](#gotchas).

---

## DOM tagging contract

**This is the load-bearing invariant of the file.** Every detected form element gets `data-ae-key="<key>"` written via `setAttribute` from the extractor JS. The Python `Field.key` attribute matches that string. Every fill operation locates elements via `[data-ae-key="..."]` selectors.

Auxiliary tags:
- `data-ae-skip="1"` — sibling/shadow elements that the standard extractor should ignore (e.g. aria-hidden inputs that hold the value of a combobox).
- `data-ae-combobox="1"` — element is a custom combobox; `fill_field` routes through `_pick_combobox_option` instead of native `select_option`.
- `data-ae-radio-value="<option_text>"` — written on each radio input so radio fill can match by value.
- `data-ae-checkbox-value="<option_text>"` — same idea for grouped checkboxes acting as multiselect.
- `data-ae-click-target="1"` — transient marker added by `_TAG_OPTION_JS` to identify the right dropdown option to click; cleared after each click.

**Extractors skip already-tagged elements**, so `_extract_custom_selects`, `extract_new_standard_fields`, and `_extract_comboboxes` are all safe to call multiple times. That's how late-loading and conditional fields are picked up.

If you change the attribute name, **change both halves** (extractor JS + every `f'[data-ae-key="..."]'` selector in Python). Or things break silently.

---

## Dataclasses

### `Field`

```python
@dataclass
class Field:
    key: str           # also written as data-ae-key on the DOM element
    type: str          # see Field.type set below
    label: str
    name: str | None   # input's name attribute, when present
    required: bool
    options: list[str] | None
    max_length: int | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Field"

    def to_field_spec(self) -> ai.FieldSpec
```

`Field.type` set: `text`, `textarea`, `email`, `phone`, `url`, `number`, `date`, `select`, `multiselect`, `radio`, `checkbox`, `file`, `searchable_select`. Adding a type means extending the JS `EXTRACT_JS` switch AND the `fill_field` dispatch.

`from_dict(d)` parses a dict produced by `EXTRACT_JS`. Keys: `key`, `type`, `label`, `name`, `required`, `options`, `maxLength`.

`to_field_spec()` shrinks down to the AI-facing `ai.FieldSpec` (drops `key` and `name`).

### `PageMeta`

```python
@dataclass
class PageMeta:
    title: str | None        # job title from <title>
    company: str | None      # extracted via heuristics
    location: str | None     # job location string; used by resolver._compute_salary
```

Built by `_extract_meta(page)`.

## Constants

### `SEARCHABLE_THRESHOLD = 25`

Comboboxes with more than 25 options are treated as `searchable_select` (free-text typeahead) rather than `select` (enumerated dropdown). Reason: country pickers with 200+ options would blow the AI prompt.

Used in `_extract_custom_selects` and `_extract_comboboxes`.

---

## Embedded JS strings

Most of this file's volume is inline JavaScript run via `page.evaluate(...)`. Listed in extraction order:

### `COMBOBOX_TAG_JS`

Runs before `EXTRACT_JS`. Tags every visible `[role="combobox"]` (skipping ones inside listboxes — those are search-inside-dropdown helpers). For each:
- Resolves a label via `aria-labelledby` → `aria-label` → wrapper `legend|label|h*|[class*=label]`.
- Sets `data-ae-key="aecb_<i>"` and `data-ae-combobox="1"`.
- Marks sibling `aria-hidden="true"` inputs (the shadow value-holding input) as `data-ae-skip="1"` so the standard pass ignores them.
- Returns `{key, label, required}` per detected combobox.

Output shape consumed by `_extract_comboboxes`.

### `EXTRACT_JS`

The main extractor. Lines 71-272. Picks the application form (`#grnhse_app form` / `form#application_form` / `form[action*="greenhouse.io"]` first; falls back to the form with the most non-hidden inputs). Then detects, in order:

1. **Radio groups** by shared `name` → `type: 'radio'` with `options` list. Each radio gets `data-ae-key="<group_key>"` and `data-ae-radio-value="<option_text>"`.
2. **Checkbox groups** — single checkbox = `type: 'checkbox'` with `options: ['Yes','No']`. Multiple checkboxes sharing a `name` = `type: 'multiselect'` with each box getting `data-ae-checkbox-value`.
3. **Standard inputs/selects/textareas** — visible, non-disabled, non-hidden, non-submit. Type derives from `tagName` and `input.type`:
   - `select` (single or multiple) → `select` / `multiselect`, options stripped of "select…" / "please select" / "choose…" / "--" placeholders.
   - `file` → `file`.
   - `textarea` → `textarea`.
   - `tel` → `phone`. Non-`text|email|tel|url|number|date` falls back to `text`.
4. **Skips** already-tagged elements (`[data-ae-key]` from `COMBOBOX_TAG_JS`) and `[data-ae-skip="1"]`. Also skips `[type=search]` (search-inside-dropdown helpers) and `aria-hidden="true"` elements.

Label resolution priority (`labelFor`): `<label for=id>` → `<label>` ancestor → `aria-label` → `aria-labelledby` → wrapper `legend|h*|label|[class*=label]`. "attach"/"upload"/"choose file"/"browse" labels are rejected (they're button labels, not field labels).

For radio groups, `groupLabel(radio)` walks up to the `<fieldset><legend>` first, then to a wrapper element.

Output is a list of dicts with `{key, type, label, name, required, options, maxLength}`. Python wraps each via `Field.from_dict`.

### `_DISMISS_OVERLAYS_JS`

Defensive: closes/neutralises common cookie/consent/sticky banners that intercept clicks. Targets known IDs (`#relyance-banner-container`, `#gtmStickyBanner`) plus class/id substrings (`cookie`, `consent`, `gdpr`, `privacy-banner`, `sticky-banner`). For each:
- If a button inside matches an "accept|close|dismiss|got it|okay|i agree|continue|confirm|allow" regex, click it.
- Otherwise, set `pointer-events: none !important` so the banner doesn't intercept anything.

Also catches any `[role="dialog"][aria-modal="true"]` that's `position: fixed|sticky` and not inside a `<form>`.

Wrapped in Python by `_dismiss_overlays(page)` which evaluates the JS and returns immediately — the JS is synchronous DOM mutation (button clicks + `pointer-events: none`), so no settle wait is needed.

### `_CUSTOM_SELECT_JS`

Runs after the standard extractor. Detects custom React/JS dropdown widgets the standard extractor misses (Duolingo EEO, Greenhouse "ingestion form" question groups, etc.). Three strategies:

- **Strategy 0 (preferred):** Greenhouse "ingestion form" structure — `[id^="question_"][role="group"]` with sibling `<label id="question_<id>--label">`. The actual click target is `<button aria-haspopup="listbox">`, **not** the display span (clicking the span doesn't open the dropdown). Targets the button explicitly. Marks the whole group + every "Select..." display span as `data-ae-skip` so the other strategies don't re-tag.
- **Strategy 1:** Proximity search around any text node matching `/please select.*applies to you/i`. Walks up to 6 ancestors, scans siblings before the label for the question text, scans siblings after for a `Select.../Select` trigger.
- **Strategy 2:** Catch-all for remaining untagged "Select..." or "Select" triggers anywhere in the form. Filters to leaves only (no nested triggers) and skips unlabelled triggers.

Returns `[{key, label}]` list. Each tagged element gets `data-ae-key="ae_custom_<i>"` and `data-ae-combobox="1"`.

### `_VISIBLE_OPTIONS_JS` / `_READ_DROPDOWN_OPTIONS_JS`

Read text from open dropdowns.

- `_VISIBLE_OPTIONS_JS`: scans only `[role="listbox"]` that are `offsetParent != null` (excludes the international-tel-input hidden country listbox). Returns option texts. Used by `_read_visible_options`.
- `_READ_DROPDOWN_OPTIONS_JS`: more comprehensive, falls through five strategies (ARIA listbox → ARIA roles → `li[tabindex≥0]` → absolutely-positioned containers → any visible `ul>li`). Used by `_extract_custom_selects` after clicking a custom trigger.

### `_TAG_OPTION_JS`

Tags the best-matching visible dropdown option with `data-ae-click-target="1"`. Same five-strategy fallthrough as `_READ_DROPDOWN_OPTIONS_JS`. Match ranking: exact text > startsWith > includes. Caller (`_click_visible_option`) then clicks the tagged element via Playwright (which dispatches synthetic events React handles).

### `_AUTOCOMPLETE_PICK_JS` / `_AUTOCOMPLETE_PICK_FIRST_JS`

Used only for location autocomplete. Cast a wide net (`[role="listbox"] [role="option"]`, `pac-container .pac-item`, `geosuggest__item`, `[id*="downshift"] li`, etc.). `_AUTOCOMPLETE_PICK_JS` matches by content (exact → starts → includes); `_AUTOCOMPLETE_PICK_FIRST_JS` clicks the first visible candidate (used as a final fallback after typing).

### `_FIND_FORM_ERRORS_JS`

Two-strategy error scrape:
1. Every `aria-invalid="true"` input/select/textarea/combobox. Pulls error text from `aria-describedby` or a sibling `[class*="error"]:not([class*="grecaptcha"])`.
2. Every visible element with class containing `error` or `role="alert"`, excluding captcha errors, label/legend tags, elements wrapping labels, and elements whose text equals the field label (Greenhouse adds `error` class to the label itself sometimes).

Deduplicates by `field||message`. Returns `[{field, message}]`.

### `_STEALTH_INIT_JS`

Runs before every navigation via `context.add_init_script`. Patches:
- `navigator.webdriver` → `undefined`.
- Fake `navigator.plugins` (Chrome PDF Plugin etc.).
- `navigator.languages` → `['en-US','en']`.
- `window.chrome.runtime` → `{}`.
- `navigator.permissions.query` returns `Notification.permission` for the `notifications` query (rather than `denied`).
- `WebGLRenderingContext.getParameter` returns `Intel Inc.` / `Intel Iris OpenGL Engine` for vendor/renderer params (37445/37446).

These are common reCAPTCHA fingerprinting checkpoints.

---

## Extraction pipeline

### `open_application(page_factory, url) -> tuple[Page, list[Field], PageMeta]`

The high-level entry point used by `runner.py`. Flow:

1. `page = page_factory(); page.goto(url, wait_until="domcontentloaded")`.
2. **Embed handling:** if the host page is not a `greenhouse.io` URL, race `wait_for_selector` for the first useful signal — either an `iframe[src*="greenhouse.io"]` (embed pattern) or the application form selectors (inline render) — with a 5s cap. Returning on the first hit avoids the multi-second tail of `networkidle` on bloated job-board hosts. Then check for an iframe via `_find_greenhouse_iframe_url`; if found, `page.goto(iframe_url)`.
3. **Wait for form:** wait for Greenhouse-specific selectors (`form#application_form`, `#grnhse_app form`, `input[name='first_name']`, `input[name='email']`) up to 12s. Fall back to any `form input/select/textarea/[role='combobox']` for 5s.
4. `_dismiss_overlays(page)` (synchronous, no wait).
5. **Extract:** `combobox_fields = _extract_comboboxes(page)`, then `standard = page.evaluate(EXTRACT_JS)`.
6. **Apply-link fallback:** if no fields detected, walk up to 2 hops following an "Apply" link/button via `_try_follow_apply_link`. Re-extract after each hop.
7. **Location fallback:** if no `_is_location_field(f)` hit, call `_greenhouse_location_fallback(page)`. The standard extractor sometimes misses the location input due to async rendering races.
8. **Custom selects:** `_extract_custom_selects(page, existing_labels=...)` — passing existing labels prevents mis-detection of triggers near already-handled fields.
9. Return `(page, all_fields, _extract_meta(page))`.

### `_find_greenhouse_iframe_url(page) -> str | None`

Two-step lookup:
1. Walk `page.frames` (Playwright's native frame registry — most reliable; catches dynamic frames before their src attribute is set in DOM).
2. JS scan of `<iframe>` elements for `src` matching `/greenhouse\.io/i`.

### `_try_follow_apply_link(page) -> Page | None`

Three strategies for navigating from a job-description page to the application form:
1. **Direct link:** scan all `<a>` tags whose `href` matches `greenhouse.io` AND text/href contains "apply". Or any `a[href*="greenhouse.io"]` as fallback. `page.goto(href)`.
2. **Click "Apply" button:** locator with `:has-text("Apply for this job")|"Apply Now"|"Apply"`. Try with `expect_page` first (handles new-tab navigations). Fall back to same-tab navigation by polling `page.wait_for_url(... != prev_url, timeout=5000)`.
3. **Iframe injection / inline modal:** if URL didn't change, wait 2.5s for JS to inject the form. `_find_greenhouse_iframe_url` again — if found, `page.goto`. Otherwise wait for application-form selectors up to 4s.

Returns the new page (could be a new tab or the same page after navigation), or `None` if nothing happened.

### `_extract_comboboxes(page) -> list[Field]`

For each combobox tagged by `COMBOBOX_TAG_JS`:
1. Save `scroll_y` so the page returns to its original scroll position after extraction.
2. Click the trigger (4s timeout — short, so misidentified triggers fail fast).
3. Wait 250ms, read `_read_visible_options(page)`, press Escape.
4. Dedupe options preserving order.
5. **Empty option list OR > `SEARCHABLE_THRESHOLD`** → `searchable_select` (city pickers, country pickers — typing required to filter). Otherwise `select`.
6. Restore scroll position.

Returns `Field` list.

### `_extract_custom_selects(page, existing_labels=None) -> list[Field]`

Similar flow but with two extra safeguards:

1. **Skip-before-click check:** if `existing_labels` contains the detected label (lowercased, stripped), un-tag the element and skip without clicking. Prevents scroll/click churn on misidentified triggers near already-handled fields.
2. **Empty-options recovery:** if clicking a tagged element produces zero options (genuine misidentification), un-tag the element so a later conditional pass can re-check it once dependent fields are filled.

Otherwise same as `_extract_comboboxes`: open, read options, dedupe, classify by `SEARCHABLE_THRESHOLD`, restore scroll.

### `_extract_meta(page) -> PageMeta`

- **Title:** `page.title()`.
- **Company:** `.company-name`, `#header h1`, `header h1`, `[class*="company"]` (first match wins).
- **Location:**
  1. Dedicated location selectors (`.location`, `[class*="location"]`, `[class*="job-location"]`, `[data-qa="job-location"]`, `.posting-location`, `.job__location`, `.header__location`, `.job-header__location`, `[itemprop="addressLocality"]`, `[itemtype*="JobPosting"] [itemprop="jobLocation"]`).
  2. `<meta name="description">` / `og:description` — best-effort hint.
  3. **Title parsing:** Greenhouse titles often follow `"Role at Company in City, Country"`. Regex `\bin\s+([A-Z][^|–—·]+)`.

### `extract_job_description(page) -> str`

`_EXTRACT_JOB_DESC_JS` tries `.job-description`, `#job-description`, `[class*="job-description"]`, `#content`, `.content`, `article`, `main section` in order. First match with >100 chars wins. Falls back to `body.innerText`. Caps at 5000 chars. Used for cover-letter generation.

### `extract_new_standard_fields(page, existing_labels=None) -> list[Field]`

Re-runs `EXTRACT_JS` (already-tagged elements are skipped by it). Filters out fields whose label (lowercased, stripped) is in `existing_labels`. Used for the conditional-field pass in `runner.py`.

---

## Custom select widgets

See `_CUSTOM_SELECT_JS` above for the three strategies. Key facts:

- **Strategy 0 specifically targets `<button aria-haspopup="listbox">`** — clicking the display `<span>` doesn't open the dropdown.
- **Read-options uses `_READ_DROPDOWN_OPTIONS_JS`** which falls through five DOM strategies. If none hit, options come back empty and the field gets un-tagged for retry.
- **Empty-options + retry:** a custom select that needs prior fields filled to populate (e.g. work-auth follow-up) returns empty initially. The conditional pass in `runner.py` re-runs `_extract_custom_selects` after fills, and the empty-options un-tag from the first pass means the retry can re-detect it.

---

## Fill dispatch

### `fill_field(page, field, value, resume_path=None, cover_letter_path=None) -> str | None`

Switches on `field.type`:

#### `file`
- `_is_resume_label = bool(re.search(r"resume|cv\b|curriculum", field.label, re.I))`.
- `_is_generic_label = field.label.strip().lower() == "file upload"` (the fallback label when extraction couldn't find a real label).
- If `resume_path` AND (resume label OR generic label) → `set_input_files(resume_path)`, return `"uploaded"`.
- Else if `cover_letter_path` AND `_is_cover_letter_field(field)` → upload cover letter, return `"uploaded"`.
- Else return `"skipped"`.

#### `text|email|phone|url|number|date|textarea`
- Click (with overlay-dismiss + force-click fallback).
- `loc.fill("")` to clear pre-filled value.
- **Location fields** (matched by `_is_location_field`): try `_fill_location(page, loc, value)` first. If it returns False (no autocomplete suggestion), clear and fall through to plain typing.
- **Long values (>80 chars) or textarea:** `loc.fill(value)`.
- **Short values:** `loc.type(value, delay=40)`. The slow type cadence helps reCAPTCHA scoring.

#### `select`
- Check `data-ae-combobox="1"` attribute. If yes → `_pick_combobox_option`. Otherwise native `select_option(label=value)` (fall back to `select_option(value=value)` on failure).

#### `searchable_select`
- **Location field:** click, clear, `_fill_location(page, loc, value)`. Same path as text-field location handling.
- **Otherwise:** `_type_and_pick(page, selector, value)`.

#### `multiselect`
- Parse `value` — JSON array if it starts with `[`, otherwise a single-item list.
- **Native `<select multiple>`:** `select_option(label=values)` with `value=values` fallback.
- **Checkbox group:** iterate `data-ae-checkbox-value` attributes, `_force_check(box, True)` for matches, `_force_check(box, False)` for non-matches. Idempotent — already-correct boxes are left alone.

#### `radio`
- Find the radio with `data-ae-radio-value="<value>"` (escaped via `_escape_attr`). Fall back to a case-insensitive scan if exact match fails. `_force_check(target, True)`.

#### `checkbox`
- `_is_truthy(value)` (matches `yes|true|1|on`). `_force_check(loc, True)` if truthy and not already checked, else `_force_check(loc, False)`.

#### Default
- `raise ValueError(f"Unhandled field type: {field.type}")`.

### Helpers around fill

#### `_force_check(page, loc, checked)`

Idempotent check/uncheck with progressively more forceful strategies. Each step verifies state via `loc.is_checked() == checked` before trying the next:

1. `loc.check(timeout=5000)` / `loc.uncheck(...)`.
2. `_dismiss_overlays(page); loc.scroll_into_view; loc.focus(); loc.press("Space")`. Keyboard space bypasses pointer-events CSS.
3. `loc.click(force=True)` — bypasses Playwright actionability checks.
4. Click the `<label for=id>` if one exists.
5. **DOM-level:** native prototype setter via `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked').set` to bypass React's instance property override, then dispatch `click`/`input`/`change` events to trigger React's synthetic event pipeline.

Used for radio, checkbox, and post-fill checkbox re-verify in `runner.py`.

#### `_is_truthy(value) -> bool`

`value.strip().lower() in {"yes","true","1","on"}`.

#### `_is_cover_letter_field(field) -> bool`

`bool(re.search(r"cover.?letter", field.label, re.I))`.

---

## Combobox / dropdown helpers

### `_read_visible_options(page) -> list[str]`

Wraps `_VISIBLE_OPTIONS_JS`. Dedupes preserving order.

### `_click_visible_option(page, value) -> bool`

Uses `_TAG_OPTION_JS` to tag the best-matching option, then Playwright clicks the tagged element (dispatches synthetic events React handles, unlike pure DOM `.click()`). Always clears `data-ae-click-target` afterwards in `finally`.

### `_pick_combobox_option(page, selector, value) -> None`

1. Click the trigger.
2. `wait_for_timeout(500)` — dropdown animation + React state settle.
3. `_click_visible_option(page, value)`.
4. **Fallback:** if the value isn't found, try `"Other"` (custom EEO dropdowns commonly include it).
5. **Final fallback:** Escape + raise `ValueError`.

### `_type_and_pick(page, selector, value) -> None`

For long-list comboboxes (countries, cities). Click trigger, clear, `press_sequentially(value, delay=60)`. Polls `_click_visible_option` for up to 3s while the dropdown populates. On miss, clears and tries `"Other"` for up to 2s. Raises if nothing matched.

---

## Location handling

### `_is_location_field(field) -> bool`

`re.search(r"\b(location|city|town)\b", label)` AND **not** `r"\b(location|city|town)\b\s*:"`. The colon exclusion skips custom questions like "Preferred location: Noida/Bengaluru" — those are plain text fields, not autocomplete typeaheads, and routing them through `_fill_location` would wipe the value (because no suggestion appears).

### `_greenhouse_location_fallback(page) -> Field | None`

Detects Greenhouse's location input when the standard extractor misses it (async rendering races). Two patterns:

1. **Classic:** `input[name="job_application[location]"]`, `input[id="job_application_location"]`, `input[data-testid*="location" i]`, `input[placeholder*="city|location" i]`.
2. **Modern:** visible `input[type="search"]` (no `aria-haspopup`, visible) near a label matching `\b(location|city|town)\b`. Walks up to 8 ancestors looking for a child label.

Tags with `data-ae-key="ae_gh_location"`. Returns `Field(type="text", label="Location (city)" or detected, required=True)`.

### `_fill_location(page, loc, value) -> bool`

Type a location and commit a suggestion. Polls up to ~4s because Greenhouse debounces autocomplete requests.

`_try_type_and_pick(search)`:
1. Clear, `press_sequentially(search, delay=80)`.
2. Loop for 3.5s: `_click_visible_option(page, value)` first (covers ARIA listboxes, React portals, autocomplete containers); fall back to `_AUTOCOMPLETE_PICK_JS` (geosuggest, ul.suggest, etc.) for content-matched click.
3. Final fallback: wait 1s, `_AUTOCOMPLETE_PICK_FIRST_JS` clicks the first visible suggestion regardless of content.

Two passes: full city name first, then a 4-char prefix if the first pass fails (handles slow APIs / alternate spellings).

On success: `loc.press("Tab")` to lock in the React selection, 300ms wait. On failure: `loc.fill("")` (leaving partial text causes form validation errors).

---

## Security code detection

### `find_security_code_field(page, wait_ms=0) -> str | None`

Polls `_find_security_code_field_now` every 500ms up to `wait_ms` total. Returns a CSS selector for the input, or `None`.

### `_find_security_code_field_now(page) -> str | None`

Three-strategy DOM scan:
1. **Attribute scan:** any non-hidden, non-submit, non-disabled input whose combined `name|id|placeholder|aria-label` matches `/security.?code|verification.?code|confirm.?code|one.?time.?code|auth.?code/`.
2. **Label-for relationship:** `<label for=id>` text matches `/security.?code|verification.?code/i`.
3. **Wrapper label:** nearest `.field-wrapper|.field|fieldset` contains a `legend|label|[class*=label]` matching the same regex.

When matched but `inp.id` is empty, generates a random id (`ae_security_code_<random>`) and returns `#<escaped_id>` as the selector. Used by `runner._wait_for_security_code` to type into the right field.

---

## Submit + verify

### `has_recaptcha(page) -> bool`

True if any of: `.grecaptcha-badge`, `.grecaptcha-logo`, `iframe[src*="recaptcha"]`, `iframe[src*="hcaptcha"]`, `[class*="hcaptcha"]`, `[class*="cf-turnstile"]` present. **Currently unused** — `runner.py` doesn't pre-check. Available for hooks.

### `submit(page) -> str`

Returns one of `"verified"` | `"unverified"` | `"blocked"` | `"code_required"`.

Flow:
1. `_dismiss_overlays(page)`.
2. **Find submit button:** locator chain `form button[type="submit"], form input[type="submit"]:not([hidden]), button:has-text("Submit Application"), button:has-text("Submit application"), button:has-text("Submit")`. Walks up to 10 candidates and picks the first **visible** one (DOM order). Raises `ValueError("No submit button found on page")` if none.
3. Click. `wait_for_load_state("networkidle", timeout=30000)` (swallows timeout).
4. **Wait for outcome:** `wait_for_function` polls every frame for one of:
   - Body text matches `/thank you for applying|application (?:has been )?submitted|application received|we've received your application|thanks for applying/`.
   - URL contains `thank-you` or `confirmation`.
   - A non-hidden, non-disabled input has attributes (name/id/placeholder/aria-label) matching the security-code regex → `'code_required'`.
   - Visible `.grecaptcha-error` with non-empty text → `'blocked'`.
   Up to 15s, swallow timeout.
5. **Final security-code check:** `find_security_code_field(page, wait_ms=2000)` — if found, return `"code_required"`.
6. Else `verify_submission(page)`.

### `verify_submission(page) -> str`

Re-evaluates body state without clicking anything. Returns:
- `"verified"` — success regex hit OR URL contains `thank-you`/`confirmation`/`thanks`.
- `"blocked"` — captcha-related text AND `.grecaptcha-error|[class*="captcha-error"]` element present.
- `"invalid"` — `find_form_errors(page)` returns non-empty.
- `"unverified"` — none of the above.

---

## Form errors

### `find_form_errors(page) -> list[dict]`

Wraps `_FIND_FORM_ERRORS_JS`. Returns list of `{"field": <label or '?'>, "message": <error text>}`. Empty list = no errors visible.

Two strategies in the JS:
1. `aria-invalid="true"` inputs — pull text from `aria-describedby` or sibling `[class*="error"]:not([class*="grecaptcha"])` / `[role="alert"]`.
2. Visible elements with class containing `error` or `role="alert"`. Filters out: captcha errors, `<label>`/`<legend>` tags themselves, elements wrapping labels, and elements whose textContent equals the field label (Greenhouse adds `error` class to the label itself sometimes).

Deduplicates `field||message` pairs. Drops `?`-field copies if a real-label version exists for the same message. Length-caps messages at 200 chars.

---

## Conditional re-extraction

`extract_new_standard_fields(page, existing_labels)` and `_extract_custom_selects(page, existing_labels)` are both called multiple times during a single application by `runner.py`. The "skip already-tagged elements" invariant in the JS makes this safe.

`existing_labels` filtering is the second guardrail: even if a re-extraction picks up an already-tagged label (because tags got stripped during empty-options recovery), the runner won't process it twice.

---

## Browser launcher + stealth

### `with_browser(headless=False) -> tuple[pw, None, page_factory, cleanup]`

Launches a stealth-patched Chromium with a **persistent profile**.

- `profile_dir = repo_root / "data" / "browser_profile"`. Created if absent. **Reused across runs** — this is what warms reCAPTCHA scores.
- Viewport: 1366x820. User-agent: Chrome 130 on macOS. Locale: `en-US`. Timezone: `Asia/Kolkata`.
- `ignore_default_args=["--enable-automation"]` plus `--disable-blink-features=AutomationControlled` and `--disable-features=IsolateOrigins,site-per-process`.
- `add_init_script(_STEALTH_INIT_JS)` — runs before every navigation.
- Returns `(pw, None, page_factory, cleanup)`. The `None` is a placeholder where a `Browser` would normally live; persistent contexts don't have a separate browser handle.
- `cleanup()` closes the context and stops Playwright.

**Don't replace `launch_persistent_context` with `launch` casually.** Fresh contexts re-trigger reCAPTCHA's "new browser" heuristics and submit gets blocked.

---

## Common edits

- **Add a new field type:**
  1. Update the `EXTRACT_JS` switch (output `type: "<new>"`).
  2. Add a branch in `fill_field`.
  3. Update `Field.type` doc comment in `Field` dataclass.
  4. Update the field-type list in this doc and the cross-cutting facts in [../CLAUDE.md](../CLAUDE.md).
- **Detect a new submit failure mode:** return a new status string from `submit()` / `verify_submission()`. Add a branch in `runner.py`'s status handling. Update the submit status enum in [../CLAUDE.md](../CLAUDE.md).
- **Improve label extraction:** edit `labelFor` / `groupLabel` in `EXTRACT_JS`.
- **Catch a new banner type:** extend the selectors in `_DISMISS_OVERLAYS_JS`.
- **Handle a new custom-dropdown structure:** add a Strategy 3 to `_CUSTOM_SELECT_JS` AND a new fallthrough branch in `_TAG_OPTION_JS` / `_READ_DROPDOWN_OPTIONS_JS` if the option container has unfamiliar markup.
- **Adjust `SEARCHABLE_THRESHOLD`:** affects when comboboxes/custom-selects are treated as free-text. Higher = more options enumerated to AI = larger prompts.
- **Tune location autocomplete polling:** edit the `deadline` values in `_fill_location._try_type_and_pick`. Longer = more reliable on slow APIs but slower to skip on truly missing.
- **Add a new stealth patch:** edit `_STEALTH_INIT_JS`.

## Gotchas

- **Tagging contract is the load-bearing invariant.** If you change the attribute name, change *every* selector that uses it. Default to keeping `data-ae-key`.
- **Custom-select strategies are ordered.** Strategy 0 must run first because it marks group + display elements as `data-ae-skip` to prevent Strategy 1/2 re-tagging the same widget via an ancestor.
- **`_extract_comboboxes` uses a 4s click timeout, not 30s.** Misidentified triggers fail fast — don't make this longer.
- **Native prototype setter in `_force_check`** (`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked').set`) bypasses React's instance-property override. If React 18+ changes how it manages this, the fallback may stop working.
- **`_TAG_OPTION_JS` clears all existing `data-ae-click-target` attributes before tagging.** Don't rely on the marker persisting between calls.
- **`_AUTOCOMPLETE_PICK_FIRST_JS` clicks the first visible option blindly.** It's a last resort — make sure callers only invoke it after typing a search term, not on a blank dropdown.
- **`_fill_location` clears the field on failure.** If you rely on it succeeding silently, you'll get an empty input. Always check the return value.
- **`_is_location_field` excludes labels containing `:`.** "Preferred location: Bangalore" is a plain text question, not a typeahead. If you add a typeahead-y label that uses a colon, this regex needs updating.
- **`form_order` (in `runner.py`) is NOT passed to AI in the late/conditional batches.** AI accuracy on those fields is slightly lower as a result.
- **Submit button selection prefers visible candidates** — but a visually-hidden submit input can sneak through if it's the first match in DOM order. The `:not([hidden])` only catches the HTML `hidden` attribute, not CSS hiding.
- **`verify_submission` regex is the source of truth for "submitted" detection.** Greenhouse text wording changes have broken this in the past — if you see a false `unverified`, broaden the regex.
- **Persistent profile dir is keyed only by path** — running two `apply` commands in parallel will conflict. Single-process at a time.
- **`with_browser` returns `(pw, None, page_factory, cleanup)`** — the second slot is a leftover from a prior non-persistent design. Don't try to use it as a `Browser` handle.
