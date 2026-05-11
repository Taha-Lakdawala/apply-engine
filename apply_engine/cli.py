from __future__ import annotations

import shutil

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from . import config, db
from .profile import load_profile
from .runner import apply_to_url

app = typer.Typer(help="Auto-fill and submit Greenhouse job applications.")
console = Console()


@app.command()
def init() -> None:
    """Create profile.yaml from the template if it doesn't exist."""
    if config.PROFILE_PATH.exists():
        console.print(f"[yellow]profile.yaml already exists at {config.PROFILE_PATH}[/yellow]")
        raise typer.Exit(0)
    if not config.PROFILE_EXAMPLE_PATH.exists():
        console.print("[red]profile.example.yaml is missing from the repo.[/red]")
        raise typer.Exit(1)
    shutil.copy(config.PROFILE_EXAMPLE_PATH, config.PROFILE_PATH)
    console.print(f"[green]Created {config.PROFILE_PATH}[/green]. Fill it in, then drop your resume next to it.")


@app.command(name="apply")
def apply_cmd(
    url: str = typer.Argument(..., help="Greenhouse application URL."),
    headless: bool = typer.Option(False, "--headless", help="Run the browser headless."),
    no_submit: bool = typer.Option(False, "--no-submit", help="Fill the form but skip submitting."),
    manual_submit: bool = typer.Option(
        False, "--manual-submit",
        help="Fill the form, then pause for you to click Submit. Recommended for captcha-protected forms.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-apply even if a prior successful submission exists for this URL.",
    ),
) -> None:
    """Fill out and submit a Greenhouse application."""
    profile = load_profile()
    apply_to_url(
        url, profile,
        headless=headless, submit=not no_submit, manual_submit=manual_submit, force=force,
    )


@app.command(name="dry-run")
def dry_run_cmd(
    url: str = typer.Argument(..., help="Greenhouse application URL."),
    headless: bool = typer.Option(True, "--headless/--headed"),
) -> None:
    """Open the page, extract the form fields, and print them. No DB, no AI, no submit."""
    from . import greenhouse

    pw, browser, page_factory, cleanup = greenhouse.with_browser(headless=headless)
    try:
        page, fields, meta = greenhouse.open_application(page_factory, url)
        console.print(f"[bold]Page:[/bold] {meta.title}")
        console.print(f"[bold]Company:[/bold] {meta.company}")
        console.print(f"[bold]Found {len(fields)} fields[/bold]\n")
        table = Table(show_lines=True)
        table.add_column("#", style="dim")
        table.add_column("Type")
        table.add_column("Label")
        table.add_column("Options / max")
        for i, f in enumerate(fields, 1):
            extra = ""
            if f.options:
                extra = ", ".join(f.options[:6]) + ("…" if len(f.options) > 6 else "")
            elif f.max_length:
                extra = f"max {f.max_length}"
            req = " *" if f.required else ""
            table.add_row(str(i), f.type, f.label + req, extra)
        console.print(table)
    finally:
        cleanup()


@app.command()
def review() -> None:
    """List AI-generated answers that haven't been reviewed yet."""
    db.init_db()
    with db.connect() as conn:
        items = db.unreviewed_answers(conn)
    if not items:
        console.print("[green]Nothing to review — all AI answers have been confirmed.[/green]")
        return

    table = Table(title="Unreviewed AI-generated answers", show_lines=True)
    table.add_column("ID", style="dim")
    table.add_column("Question")
    table.add_column("Answer")
    for q, a in items:
        table.add_row(str(q.id), q.raw_text, _truncate(a.value))
    console.print(table)
    console.print("\nUse `apply edit <ID>` to revise an answer, or `apply confirm <ID>` to mark it reviewed.")


@app.command()
def edit(question_id: int = typer.Argument(...)) -> None:
    """Edit the stored answer for a question."""
    db.init_db()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT raw_text, field_type, options_json FROM questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if not row:
            console.print(f"[red]No question with id={question_id}[/red]")
            raise typer.Exit(1)

        current = db.latest_answer(conn, question_id)
        console.print(f"[bold]Question:[/bold] {row['raw_text']}")
        console.print(f"[bold]Type:[/bold]     {row['field_type']}")
        if row["options_json"]:
            console.print(f"[bold]Options:[/bold]  {row['options_json']}")
        console.print(f"[bold]Current:[/bold]  {current.value if current else '(no answer)'}\n")

        new_value = Prompt.ask("New answer")
        if not new_value:
            console.print("[yellow]No change.[/yellow]")
            return
        db.update_answer_value(conn, question_id, new_value)
    console.print("[green]Updated.[/green]")


@app.command()
def confirm(question_id: int = typer.Argument(...)) -> None:
    """Mark the latest AI-generated answer for a question as reviewed."""
    db.init_db()
    with db.connect() as conn:
        a = db.latest_answer(conn, question_id)
        if not a:
            console.print(f"[red]No answer for question id={question_id}[/red]")
            raise typer.Exit(1)
        db.mark_reviewed(conn, a.id)
    console.print("[green]Marked reviewed.[/green]")


@app.command(name="check-gmail")
def check_gmail_cmd() -> None:
    """Smoke-test the Gmail IMAP connection used to pull Greenhouse security codes."""
    import os
    from datetime import datetime, timedelta, timezone
    from . import email_fetcher

    profile = load_profile()
    addr = (profile.data.get("personal") or {}).get("email") or ""
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr.endswith("@gmail.com"):
        console.print(f"[red]Profile email is not a gmail.com address: {addr!r}[/red]")
        raise typer.Exit(1)
    if not pw:
        console.print("[red]GMAIL_APP_PASSWORD is not set.[/red]")
        raise typer.Exit(1)

    console.print(f"Connecting to Gmail as {addr}...")
    started_at = datetime.now(timezone.utc) - timedelta(days=7)
    code = email_fetcher.fetch_security_code(
        addr, pw, started_at=started_at, timeout_seconds=15, poll_interval=5.0,
    )
    if code:
        console.print(f"[green]Found a recent security code: {code[:2]}***{code[-2:]}[/green]")
    else:
        console.print(
            "[yellow]Connected, but no Greenhouse security-code email in the last 7 days.[/yellow]\n"
            "[dim]Check that Greenhouse codes aren't being filtered out of All Mail.[/dim]"
        )


@app.command(name="list")
def list_cmd() -> None:
    """List every stored question and its current answer."""
    db.init_db()
    with db.connect() as conn:
        items = db.all_qa_pairs(conn)
    if not items:
        console.print("[yellow]No stored answers yet.[/yellow]")
        return
    table = Table(show_lines=True)
    table.add_column("ID", style="dim")
    table.add_column("Question")
    table.add_column("Answer")
    table.add_column("Source", style="dim")
    for q, a in items:
        source = "AI" if a.ai_generated and not a.reviewed_at else ("AI ✓" if a.ai_generated else "manual")
        table.add_row(str(q.id), _truncate(q.raw_text, 60), _truncate(a.value), source)
    console.print(table)


def _truncate(s: str, n: int = 80) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def main() -> None:
    """Entry point. If the first arg looks like a URL, route it to the `apply` subcommand
    so users can type `apply <url>` instead of `apply apply <url>`."""
    import sys

    if len(sys.argv) >= 2 and sys.argv[1].startswith(("http://", "https://")):
        sys.argv.insert(1, "apply")
    app()


if __name__ == "__main__":
    main()
