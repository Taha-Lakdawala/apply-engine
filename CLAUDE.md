# apply-engine

CLI that auto-fills and submits Greenhouse job applications. Detects fields with Playwright, resolves answers deterministically where possible, falls back to Gemini for the rest, and caches answers in SQLite so the next application reuses them.

Entry point: `apply <url>` (Typer CLI in [apply_engine/cli.py](apply_engine/cli.py)).

---

## How to use this documentation (read this first)

- **This file is loaded into every session.** Keep it short — it's an index, not a manual.
- **Per-file deep-dives live in [claude_docs/](claude_docs/).** Each documents every function, flow, and gotcha for one source file. Read the deep-dive **before editing** that file — it will save reads/greps.
- **Working on multiple files? Read both deep-dives upfront.** Doing it on-demand mid-task is fine when scope is uncertain.
- `.claude/` is harness config (permissions, settings) — not for documentation. Don't put docs there.

## Self-update directive (mandatory)

When you change code, update the docs in the same patch. Specifically:

- **Edit [claude_docs/<file>.md](claude_docs/) whenever you change [apply_engine/<file>.py](apply_engine/).** Add new functions, update flow descriptions, adjust gotchas, refresh data shapes. Stale deep-dives are worse than missing ones because they mislead future-you.
- **Edit this `CLAUDE.md` only when something *cross-cutting* changes** — a new module, a renamed file, a changed env var, a different LLM provider, a new top-level CLI command, or a shift in the resolution pipeline. Don't bloat this file with details that belong in a deep-dive.
- **Never duplicate** the same fact in CLAUDE.md and a deep-dive. If both exist, one will drift. Index here, detail there.
- If you add a new module, create [claude_docs/<file>.md](claude_docs/) for it. If you remove a module, delete its deep-dive too.

If a deep-dive's line numbers look stale, grep by function name — every anchor in those files cites a named symbol so it's recoverable.

---

## LLM provider

The app uses **Gemini** (`google-genai`). Default model `gemini-2.5-flash`, overridable via `APPLY_ENGINE_MODEL`. **Do not swap to Claude or another provider unless explicitly asked.** All Gemini calls go through [apply_engine/ai.py](apply_engine/ai.py); the system prompt (work-auth, notice-period, salary/PPP, EEO defaults) is `SYSTEM_PROMPT` near the top of that file. **Edit AI behavior there, not in callers.**

## Module map → deep-dives

| File | Lines | Role | Deep-dive |
|---|---|---|---|
| [cli.py](apply_engine/cli.py) | 207 | Typer CLI commands. | [claude_docs/cli.md](claude_docs/cli.md) |
| [runner.py](apply_engine/runner.py) | 503 | Orchestrates one application end-to-end. | [claude_docs/runner.md](claude_docs/runner.md) |
| [resolver.py](apply_engine/resolver.py) | 211 | Deterministic answer resolution (preset/profile/salary/stored). | [claude_docs/resolver.md](claude_docs/resolver.md) |
| [ai.py](apply_engine/ai.py) | 241 | Gemini client, system prompt, batch field answerer, cover letters. | [claude_docs/ai.md](claude_docs/ai.md) |
| [greenhouse.py](apply_engine/greenhouse.py) | 1864 | All Playwright/DOM logic. The big one. | [claude_docs/greenhouse.md](claude_docs/greenhouse.md) |
| [db.py](apply_engine/db.py) | 255 | SQLite schema and accessors. | [claude_docs/db.md](claude_docs/db.md) |
| [profile.py](apply_engine/profile.py) | 73 | Loads `profile.yaml`, extracts resume text. | [claude_docs/profile.md](claude_docs/profile.md) |
| [email_fetcher.py](apply_engine/email_fetcher.py) | 194 | IMAP poll for Greenhouse security codes. | [claude_docs/email_fetcher.md](claude_docs/email_fetcher.md) |
| [config.py](apply_engine/config.py) | 16 | Paths, env vars, model name. | [claude_docs/config.md](claude_docs/config.md) |

## Cross-cutting facts

- **Resolution order (in `runner.py` via `resolver.try_known_resolve`):** preset → profile lookup → salary PPP → stored answer → AI batch. Adding a deterministic short-circuit goes in `resolver.py`, never in `runner.py`. Full details: [resolver.md](claude_docs/resolver.md).
- **DOM tagging contract:** every detected field gets `data-ae-key="<key>"` from the extractor JS in `greenhouse.py`; the fill phase locates fields by that attribute. Both extractors skip already-tagged elements, so re-running is safe. Details: [greenhouse.md](claude_docs/greenhouse.md).
- **`Field.type` set:** `text`, `textarea`, `email`, `phone`, `url`, `number`, `date`, `select`, `multiselect`, `radio`, `checkbox`, `file`, `searchable_select`. Adding a type means extending both `EXTRACT_JS` and `fill_field`.
- **Submit status enum (`greenhouse.submit`):** `verified` | `code_required` | `blocked` | `invalid` | `unverified`. Handled in [runner.py](apply_engine/runner.py).
- **Persistent browser profile:** `data/browser_profile/`. Warms reCAPTCHA over time — don't replace `launch_persistent_context` casually.

## Env vars

| Var | Purpose |
|---|---|
| `GEMINI_API_KEY` | Required for any AI call. |
| `APPLY_ENGINE_MODEL` | Overrides Gemini model (default `gemini-2.5-flash`). |
| `GMAIL_APP_PASSWORD` | Enables IMAP auto-pull of Greenhouse security codes. Must match a Gmail address in `profile.yaml` `personal.email`. |

Loaded from `.env` at repo root via `python-dotenv` in [config.py](apply_engine/config.py).

## CLI commands

`apply init` · `apply <url>` (positional URL is auto-rewritten as `apply <url>`) · `apply dry-run <url>` · `apply review` · `apply edit <id>` · `apply confirm <id>` · `apply list` · `apply check-gmail`. Flags on `apply <url>`: `--headless`, `--no-submit`, `--manual-submit`. Details: [cli.md](claude_docs/cli.md).

## Conventions

- **Extraction → `greenhouse.py`.** Inline JS via `page.evaluate(...)`; tag elements with `data-ae-key`; the fill phase re-locates them by attribute.
- **Deterministic answers → `resolver.py`.** Add to `PROFILE_MAPPINGS`, extend `_PPP_TABLE`, or write a `_compute_<thing>` helper. Do not branch on field labels in `runner.py`.
- **AI behavior → `ai.SYSTEM_PROMPT`.** Edge cases like work-auth, notice-period, salary, EEO defaults all live there. No post-processing in `runner.py`.
- **AI answers cache on first run.** To force a re-ask: delete the row, `apply edit <id>`, or `apply review` + `confirm`.
- **No retry/backoff layers.** If you hit rate limits, switch model or provider — don't add exponential backoff (durable user preference).

## Testing

There's no test suite. Closest smoke tests:
- `apply dry-run <url>` — extract + print fields, no DB/AI/submit. Use after extraction changes.
- `apply <url> --no-submit` — full pipeline without the final click.
- `apply check-gmail` — verifies `GMAIL_APP_PASSWORD` and IMAP connectivity.
