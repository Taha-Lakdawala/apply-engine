# profile.py — deep-dive

73 lines. Loads `profile.yaml`, extracts resume text, exposes preset lookups, builds the AI context blob.

> **Self-update reminder:** edit this doc whenever you change `Profile`'s methods, the resume-extraction logic, or supported resume formats. If you change profile schema, also update the schema section in [resolver.md](resolver.md) and the env-var table in [CLAUDE.md](../CLAUDE.md) only if a new external dependency is introduced.

## Public surface

### `Profile` (dataclass)

```python
@dataclass
class Profile:
    data: dict[str, Any]   # raw profile.yaml contents
    resume_path: Path      # absolute path to the resume file
    resume_text: str       # extracted text content of the resume
```

#### `Profile.as_context() -> str`

Returns a stable text blob shipped to the LLM in every batch call:

```
# Candidate profile
<yaml.safe_dump(self.data, sort_keys=False)>

# Resume
<resume_text>
```

**Note:** the docstring still says *"sent to Claude"* — stale comment from before the Gemini swap. Data now goes to Gemini. Fix when next touching the file.

The blob preserves YAML key order via `sort_keys=False`. Adding a new top-level key in `profile.yaml` automatically reaches the model — no wiring needed.

#### `Profile.preset_answer(question) -> str | None`

Looks up `data["preset_answers"]` (a dict). Compares each key to the input via `resolver.normalize_question` (lowercase, strip non-alphanumerics). Returns the first matching value as `str(value)`, else `None`. Lazy-imports `normalize_question` to avoid a circular import.

### `load_profile() -> Profile`

1. Errors with a friendly message + `apply init` hint if `config.PROFILE_PATH` is missing.
2. Parses the YAML. `yaml.safe_load(f) or {}` (empty file → empty dict).
3. Resolves `resume_path` from `data["resume_path"]` (default `"resume.pdf"`). Relative paths are resolved against `config.ROOT`.
4. Errors if the resume file doesn't exist.
5. Calls `_extract_resume_text(resume_path)` and packages everything into `Profile`.

## Private helpers

### `_extract_resume_text(path) -> str`

Format-dispatched on the file's `suffix.lower()`:

- **`.pdf`** — `pypdf.PdfReader`, joins `page.extract_text()` per page with double newlines. Empty pages contribute `""`.
- **`.docx`** — `docx.Document`, joins paragraph texts with `\n`.
- **`.txt` / `.md`** — `path.read_text()`.
- **anything else** — `ValueError(f"Unsupported resume format: {suffix}")`.

`pypdf` and `python-docx` are imported lazily so they don't cost startup time when the resume is plaintext.

## Profile YAML schema

Top-level keys consumed by other modules:

```yaml
personal:
  first_name, last_name, preferred_name, email, phone, pronouns

location:
  city, state, country, postal_code, address_line1

links:
  linkedin, github, website, portfolio, twitter

education:
  institution, degree_level, major, gpa, start_year, graduation_year

work_authorization:
  authorized_to_work: bool       # for FOREIGN countries only — see ai.SYSTEM_PROMPT
  requires_sponsorship: bool     # for FOREIGN countries only
  visa_status: str

demographics:
  gender, race_ethnicity, veteran_status, disability_status, hispanic_or_latino

compensation:
  current_ctc

employment:                      # most-recent first; reverse-chronological
  - company, title, location, start_month, start_year, end_month, end_year, current (bool), summary

bio: |
  Free-form paragraphs. Used by the AI for cover-letter-style answers.

preset_answers:                  # exact-question overrides; beats everything else
  "How did you hear about this job?": "LinkedIn"
  "Years of experience with Python": "5"

resume_path: "resume.pdf"        # relative to repo root or absolute
```

`resolver.PROFILE_MAPPINGS` regex-matches form labels and maps them to dotted paths into `data` (e.g. `personal.first_name`). See [resolver.md](resolver.md).

## Common edits

- **Support a new resume format:** add a branch in `_extract_resume_text`. Update the format list above.
- **Add a new top-level YAML key:** it ships to Gemini automatically. If you also want deterministic resolution, add a `(regex, "dotted.path")` tuple to `resolver.PROFILE_MAPPINGS`. Update the schema above.
- **Change the AI context format:** edit `as_context()`. Keep the section headers stable — the system prompt assumes `# Candidate profile` / `# Resume` markers.

## Gotchas

- **`as_context()` docstring is stale.** Mentions "Claude" — should say Gemini. Fix opportunistically.
- **`preset_answer` lazy-imports `normalize_question`** to dodge a circular import (resolver imports profile via runner). Don't move that import to the top of the file.
- **PDF text extraction is best-effort** — pypdf can return empty strings for image-based PDFs. If the resume is scanned, the AI sees no resume content.
- **`docx` is the `python-docx` package**, not `docx2txt`. Watch out for typing the wrong dependency.
- **`employment:` is consumed by the AI, not by `resolver.PROFILE_MAPPINGS`.** There are no deterministic regex mappings for company/title/employment-year fields because Greenhouse forms reuse the labels "Start date year" / "End date year" in BOTH education and employment sections, and the resolver sees one field at a time so it can't tell which section any given field belongs to. Instead, the full `employment:` list ships in `Profile.as_context()` and the AI uses `form_order` (the page-order list of all field labels) to pick the right entry. If you add a deterministic mapping like `employment.0.company`, every employer field across multi-row forms will get the same value.
- **The engine does NOT auto-click "+ Add Employer" buttons.** A form that allows multiple employer entries will only have its first entry filled. The remaining entries in `employment:` are still useful — they appear in `Profile.as_context()`, so any free-text "describe your work history" field can list them. Adding multi-row support is a known gap.
