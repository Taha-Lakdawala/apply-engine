from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from rich.console import Console

from . import config

from . import ai, db, email_fetcher, greenhouse, resolver
from .profile import Profile

console = Console()


@dataclass
class FillResult:
    label: str
    type: str
    value: str
    source: str  # "preset", "profile", "stored", "ai", "skipped"
    error: str | None = None


@dataclass
class RunReport:
    url: str
    company: str | None
    job_title: str | None
    fields_filled: list[FillResult] = field(default_factory=list)
    submitted: bool = False
    error: str | None = None


def apply_to_url(
    url: str,
    profile: Profile,
    *,
    headless: bool = False,
    submit: bool = True,
    manual_submit: bool = False,
) -> RunReport:
    db.init_db()
    pw, browser, page_factory, cleanup = greenhouse.with_browser(headless=headless)
    report = RunReport(url=url, company=None, job_title=None)

    try:
        page, fields, meta = greenhouse.open_application(page_factory, url)
        report.company = meta.company
        report.job_title = meta.title

        console.print(f"[bold]Found {len(fields)} fields[/bold] on {url}")

        # ---- Phase 1: deterministic resolution (preset / profile / stored) ----
        resolved: dict[str, resolver.ResolvedAnswer] = {}
        unknowns: list[tuple[greenhouse.Field, int]] = []  # (field, question_id)
        for f in fields:
            if f.type == "file":
                continue  # handled in fill phase
            qid, answer = resolver.try_known_resolve(f.to_field_spec(), profile, source_url=url)
            if answer is not None:
                resolved[f.key] = answer
            else:
                unknowns.append((f, qid))

        # ---- Phase 2: one batch AI call for whatever's left ----
        if unknowns:
            console.print(f"[bold]Calling Gemini for {len(unknowns)} unknown field(s)...[/bold]")
            prior_qa = resolver.get_prior_qa()
            specs = [(f.key, f.to_field_spec()) for f, _ in unknowns]
            ai_answers = ai.answer_fields_batch(specs, profile.as_context(), prior_qa)
            for f, qid in unknowns:
                value = ai_answers.get(f.key, "")
                if value.strip():
                    resolved[f.key] = resolver.store_ai_answer(qid, value, source_url=url)
                else:
                    # Don't cache empty answers — let the next run try again.
                    resolved[f.key] = resolver.ResolvedAnswer(
                        value="", source="ai", question_id=qid, answer_id=0,
                    )

        # ---- Phase 3: fill the form (slowed down to look human to reCAPTCHA) ----
        for f in fields:
            page.wait_for_timeout(random.randint(150, 450))
            try:
                if f.type == "file":
                    outcome = greenhouse.fill_field(page, f, "", resume_path=profile.resume_path)
                    if outcome == "uploaded":
                        report.fields_filled.append(
                            FillResult(label=f.label, type=f.type, value=str(profile.resume_path), source="preset")
                        )
                        console.print(f"  [cyan]file[/cyan]    {f.label} -> {profile.resume_path.name}")
                    else:
                        report.fields_filled.append(
                            FillResult(label=f.label, type=f.type, value="", source="skipped",
                                       error="no resume-like label match")
                        )
                        console.print(f"  [dim]file[/dim]    {f.label} -> [dim]skipped (not a resume slot)[/dim]")
                    continue

                ans = resolved[f.key]
                greenhouse.fill_field(page, f, ans.value, resume_path=profile.resume_path)
                report.fields_filled.append(
                    FillResult(label=f.label, type=f.type, value=ans.value, source=ans.source)
                )
                tag = {"ai": "yellow", "stored": "green", "preset": "blue", "profile": "magenta"}[ans.source]
                console.print(f"  [{tag}]{ans.source:7}[/{tag}] {f.label} -> {_truncate(ans.value)}")
            except Exception as e:
                report.fields_filled.append(
                    FillResult(label=f.label, type=f.type, value="", source="skipped", error=str(e))
                )
                console.print(f"  [red]skip[/red]    {f.label} ({e})")

        if submit:
            console.print("\n[bold]Submitting application...[/bold]")
            page.wait_for_timeout(800)
            status = greenhouse.submit(page)

            if status == "code_required":
                sec_selector = greenhouse.find_security_code_field(page, wait_ms=3000)
                if sec_selector:
                    submit_started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
                    code = _wait_for_security_code(profile, submit_started_at)
                    if code:
                        # Type (don't fill) so React's onChange fires per keystroke and the
                        # submit button transitions out of its disabled state.
                        loc = page.locator(sec_selector).first
                        loc.click()
                        loc.fill("")
                        loc.type(code, delay=70)
                        loc.press("Tab")  # blur to trigger any onBlur validation
                        page.wait_for_timeout(800)

                        # Wait for the submit button to become enabled.
                        try:
                            page.wait_for_function(
                                r"""() => {
                                    const btns = Array.from(document.querySelectorAll(
                                        'form button[type="submit"], form input[type="submit"]'
                                    ));
                                    return btns.some(b => !b.disabled && b.getAttribute('aria-disabled') !== 'true');
                                }""",
                                timeout=10000,
                            )
                        except Exception:
                            console.print(
                                "[red]Submit button still disabled after entering code. "
                                "Code may have been rejected.[/red]"
                            )

                        # Snapshot before clicking, in case it errors again
                        snap = config.DATA_DIR / f"submit_pre_{int(time.time())}.png"
                        try:
                            page.screenshot(path=str(snap), full_page=True)
                            console.print(f"[dim]Pre-final-submit screenshot: {snap}[/dim]")
                        except Exception:
                            pass

                        console.print("[bold]Re-submitting with code...[/bold]")
                        status = greenhouse.submit(page)

            # Post-submit screenshot — taken after wait_for_function settles
            shot_path = config.DATA_DIR / f"submit_{int(time.time())}.png"
            try:
                page.screenshot(path=str(shot_path), full_page=True)
                console.print(f"[dim]Post-submit screenshot: {shot_path}[/dim]")
            except Exception:
                pass

            if status == "verified":
                report.submitted = True
                console.print("[green]Submitted (verified).[/green]")
            elif status == "blocked":
                console.print(
                    "[red]Submit blocked — captcha error visible on page.[/red]\n"
                    "[yellow]Try re-running. Persistent profile gets warmer with each visit.[/yellow]"
                )
            elif status == "code_required":
                console.print(
                    "[yellow]Security-code step detected but didn't complete. "
                    "Check the screenshot — code field may have different markup.[/yellow]"
                )
            else:
                console.print(
                    "[yellow]Submit clicked, no confirmation marker detected.[/yellow]\n"
                    "[yellow]Check the screenshot above OR your email to verify.[/yellow]"
                )
                report.submitted = True

            page.wait_for_timeout(3000)
        else:
            console.print("\n[yellow]Skipping submit (--no-submit).[/yellow]")

        with db.connect() as conn:
            db.record_application(
                conn,
                url=url,
                company=meta.company,
                job_title=meta.title,
                status="submitted" if report.submitted else "filled",
            )
    except Exception as e:
        report.error = str(e)
        console.print(f"[red]Error: {e}[/red]")
        with db.connect() as conn:
            db.record_application(
                conn,
                url=url,
                company=report.company,
                job_title=report.job_title,
                status="failed",
                error=str(e),
            )
    finally:
        cleanup()

    return report


def _wait_for_security_code(
    profile: Profile,
    started_at: datetime,
    timeout_seconds: int = 600,
) -> str | None:
    """Wait for a Greenhouse email-delivered security code.

    Three ways to provide it, tried in order:
      1. IMAP poll of the candidate's Gmail (requires GMAIL_APP_PASSWORD env var).
      2. `data/security_code.txt` file (script polls).
      3. Interactive stdin prompt (if a TTY is attached).
    Whichever comes first wins. Returns the code, or None on timeout.
    """
    import sys

    console.print("\n[yellow]Greenhouse sent a security code to your email.[/yellow]")

    # 1. Try Gmail IMAP if configured.
    gmail_addr = (profile.data.get("personal") or {}).get("email") or ""
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if gmail_addr and app_password and gmail_addr.endswith("@gmail.com"):
        console.print(f"[dim]Polling Gmail ({gmail_addr}) for the code...[/dim]")
        code = email_fetcher.fetch_security_code(
            gmail_addr, app_password, started_at=started_at, timeout_seconds=120
        )
        if code:
            console.print(f"[green]Pulled code from Gmail: {_redact(code)}[/green]")
            return code
        console.print("[yellow]Gmail poll timed out, falling back to file/stdin.[/yellow]")

    # 2/3. File or stdin fallback.
    code_file = config.DATA_DIR / "security_code.txt"
    code_file.unlink(missing_ok=True)
    console.print(
        f"  - Write the code to: {code_file}\n"
        "  - Or paste interactively below (if running in a terminal).\n"
    )

    deadline = time.time() + timeout_seconds
    is_tty = sys.stdin.isatty()
    while time.time() < deadline:
        if code_file.exists():
            code = code_file.read_text().strip()
            try:
                code_file.unlink()
            except OSError:
                pass
            if code:
                console.print(f"[green]Picked up code from {code_file.name}.[/green]")
                return code
        if is_tty:
            try:
                code = console.input("[bold]Security code: [/bold]").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if code:
                return code
        else:
            time.sleep(2)
    console.print("[red]Timed out waiting for security code.[/red]")
    return None


def _redact(code: str) -> str:
    if len(code) <= 4:
        return code[0] + "***" + code[-1]
    return code[:2] + "***" + code[-2:]


def _truncate(s: str, n: int = 70) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"
