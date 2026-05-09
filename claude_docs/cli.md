# cli.py — deep-dive

Typer CLI entry point. 207 lines. **Read this when adding/changing a command, or before touching `main()` or any `@app.command()`.**

> **Self-update reminder:** if you add/rename a command, change a flag, or alter command behavior, update this file in the same patch. Update the CLI commands list in [CLAUDE.md](../CLAUDE.md) only if a *new top-level command* is added.

## Imports

`shutil` (file copy for `init`), `typer`, `rich.console.Console`, `rich.prompt.Prompt`, `rich.table.Table`. Local: `config`, `db`, `profile.load_profile`, `runner.apply_to_url`.

## Top-level objects

```python
app = typer.Typer(help="Auto-fill and submit Greenhouse job applications.")
console = Console()
```

## Commands

### `init`

```python
@app.command()
def init() -> None
```

Copies `profile.example.yaml` → `profile.yaml`. Bails if the destination exists or the source is missing. Tells the user to drop the resume next to it.

### `apply` (a.k.a. `apply <url>`)

```python
@app.command(name="apply")
def apply_cmd(
    url: str,
    headless: bool = False,           # --headless
    no_submit: bool = False,          # --no-submit
    manual_submit: bool = False,      # --manual-submit
)
```

Loads the profile, then calls `runner.apply_to_url(url, profile, headless=..., submit=not no_submit, manual_submit=...)`. The positional-URL shortcut is implemented in `main()` (see below).

### `dry-run`

```python
@app.command(name="dry-run")
def dry_run_cmd(url: str, headless: bool = True)  # --headless/--headed
```

Opens the page via `greenhouse.with_browser` + `greenhouse.open_application`, prints company/title and a Rich table of every detected field (`#`, `Type`, `Label *`, `Options/max`). No DB writes, no AI calls, no submit. Truncates options at 6 per row. **Use this after changing extraction logic.**

### `review`

Lists every AI-generated answer not yet `reviewed_at`. Prints id + question + truncated answer. Empty case prints a green "all confirmed" message. Tells the user to use `apply edit <id>` or `apply confirm <id>`.

### `edit <question_id>`

Prints the current question (raw_text, field_type, options_json, current value). Prompts for a new answer via `Prompt.ask`. Empty input → no change. Otherwise calls `db.update_answer_value(...)` which inserts a new row marked `ai_generated=0` and `reviewed_at=<now>` — i.e., a fresh manual answer that supersedes any AI one.

### `confirm <question_id>`

Marks the latest answer for the question as reviewed via `db.mark_reviewed(...)`. Errors if no answer exists.

### `check-gmail`

Smoke-tests the IMAP connection. Reads `personal.email` from profile, requires it to end in `@gmail.com`. Reads `GMAIL_APP_PASSWORD` from env. Calls `email_fetcher.fetch_security_code(...)` with a 7-day lookback and a 15s timeout. Prints redacted code on success, a "no recent code" message otherwise. **Doesn't actually need a code to exist** — it's checking the connection works.

### `list`

Prints every stored question with its current answer, the source (`AI` / `AI ✓` / `manual`), and id. Truncates question to 60 chars, answer to 80.

## Helpers

```python
def _truncate(s: str, n: int = 80) -> str
```
Replaces newlines with spaces, then truncates with `…`.

## `main()` — URL shortcut

```python
def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1].startswith(("http://", "https://")):
        sys.argv.insert(1, "apply")
    app()
```

This rewrites `apply https://...` → `apply apply https://...` so users don't have to type the subcommand. **Don't break this** — it's the most-used invocation. Defined as the project script in `pyproject.toml` (`apply = "apply_engine.cli:main"`).

## Common edits

- **Add a new command:** new `@app.command()` function. Add it to the CLI commands line in [CLAUDE.md](../CLAUDE.md) too.
- **Add a new flag to `apply`:** add a `typer.Option(...)` arg, thread it through `runner.apply_to_url`, document the flag in [runner.md](runner.md) too.
- **Change `init`'s template behavior:** edit the `init()` function. The example file path comes from `config.PROFILE_EXAMPLE_PATH`.

## Gotchas

- `apply review` and `apply list` both call `db.init_db()` defensively — the DB might not exist yet on first invocation.
- `Prompt.ask` is interactive; `apply edit` is unusable in non-TTY contexts. There's no scripted equivalent — `db.update_answer_value` is the API if you need one.
- The URL-shortcut in `main()` assumes Typer's standard arg layout. If you add subcommands that take URLs directly, the rewrite logic still inserts `apply` and may break them.
