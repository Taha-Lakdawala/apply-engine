from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    force: bool = False,
) -> RunReport:
    db.init_db()
    report = RunReport(url=url, company=None, job_title=None)

    if not force:
        with db.connect() as conn:
            prior = db.find_successful_application(conn, url)
        if prior:
            report.company = prior["company"]
            report.job_title = prior["job_title"]
            console.print(
                f"[yellow]Already submitted this URL on {prior['submitted_at']} "
                f"(application #{prior['id']}). Skipping.[/yellow]\n"
                f"[dim]Pass --force to re-apply.[/dim]"
            )
            return report

    pw, browser, page_factory, cleanup = greenhouse.with_browser(headless=headless)
    app_dir: Path | None = None
    pre_submit_path: Path | None = None
    post_submit_path: Path | None = None

    try:
        page, fields, meta = greenhouse.open_application(page_factory, url)
        report.company = meta.company
        report.job_title = meta.title
        app_dir = _make_app_dir(meta.company, meta.title)

        # Re-order fields by their actual DOM Y-position. The extractor produces
        # comboboxes first (DOM-order within that group), then standard inputs
        # (DOM-order within their group), but the two groups are *interleaved* on
        # the page. Without re-sorting, form_order presented to the AI clusters
        # all Education comboboxes together far from the Education year text fields,
        # so the AI can't tell which Start/End year pair belongs to which section.
        fields = greenhouse._sort_fields_by_position(page, fields)

        console.print(f"[bold]Found {len(fields)} fields[/bold] on {url}")

        job_description = greenhouse.extract_job_description(page)

        # ---- Phase 1: deterministic resolution (preset / profile / stored) ----
        job_location = meta.location
        resolved: dict[str, resolver.ResolvedAnswer] = {}
        unknowns: list[tuple[greenhouse.Field, int]] = []  # (field, question_id)
        for f in fields:
            if f.type == "file":
                continue  # handled in fill phase
            qid, answer = resolver.try_known_resolve(f.to_field_spec(), profile, source_url=url, job_location=job_location)
            if answer is not None:
                resolved[f.key] = answer
            else:
                unknowns.append((f, qid))

        # ---- Phase 2: one batch AI call for whatever's left ----
        if unknowns:
            console.print(f"[bold]Calling Gemini for {len(unknowns)} unknown field(s)...[/bold]")
            prior_qa = resolver.get_prior_qa()
            specs = [(f.key, f.to_field_spec()) for f, _ in unknowns]
            unknown_keys = {f.key for f, _ in unknowns}
            form_order = [
                (f.label, resolved[f.key].value if f.key in resolved else None)
                for f in fields
                if f.type != "file" and (f.key in resolved or f.key in unknown_keys)
            ]
            ai_answers = ai.answer_fields_batch(
                specs, profile.as_context(), prior_qa,
                job_location=job_location, job_url=url,
                form_order=form_order or None,
            )
            for f, qid in unknowns:
                value = ai_answers.get(f.key, "")
                if value.strip():
                    resolved[f.key] = resolver.store_ai_answer(qid, value, source_url=url)
                else:
                    # Don't cache empty answers — let the next run try again.
                    resolved[f.key] = resolver.ResolvedAnswer(
                        value="", source="ai", question_id=qid, answer_id=0,
                    )

        # ---- Phase 2b: generate cover letter if any required file field needs one ----
        cover_letter_path = None
        for f in fields:
            if f.type == "file" and f.required and greenhouse._is_cover_letter_field(f):
                console.print("[bold]Generating cover letter (required field)...[/bold]")
                cl_text = ai.generate_cover_letter(
                    profile.as_context(), meta.title, meta.company, job_description
                )
                cover_letter_path = config.DATA_DIR / f"cover_letter_{int(time.time())}.pdf"
                _write_cover_letter_pdf(cl_text, cover_letter_path)
                console.print(f"  [cyan]cover_letter[/cyan] generated -> {cover_letter_path.name}")
                break

        # ---- Phase 3: fill the form (slowed down to look human to reCAPTCHA) ----
        resume_uploaded = False
        for f in fields:
            page.wait_for_timeout(random.randint(150, 450))
            try:
                if f.type == "file":
                    # Pass resume_path only until we've uploaded it once; subsequent generic
                    # file fields (e.g. optional attachments) will be skipped automatically.
                    resume_path_arg = profile.resume_path if not resume_uploaded else None
                    outcome = greenhouse.fill_field(
                        page, f, "", resume_path=resume_path_arg, cover_letter_path=cover_letter_path
                    )
                    if outcome == "uploaded":
                        if greenhouse._is_cover_letter_field(f) and cover_letter_path:
                            uploaded_name = cover_letter_path.name
                        else:
                            uploaded_name = profile.resume_path.name
                            resume_uploaded = True
                        report.fields_filled.append(
                            FillResult(label=f.label, type=f.type, value=uploaded_name, source="preset")
                        )
                        console.print(f"  [cyan]file[/cyan]    {f.label} -> {uploaded_name}")
                    else:
                        report.fields_filled.append(
                            FillResult(label=f.label, type=f.type, value="", source="skipped",
                                       error="no resume-like label match")
                        )
                        console.print(f"  [dim]file[/dim]    {f.label} -> [dim]skipped (not a resume slot)[/dim]")
                    continue

                ans = resolved[f.key]
                greenhouse.fill_field(page, f, ans.value, resume_path=profile.resume_path, cover_letter_path=cover_letter_path)
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

        # Re-verify "yes" checkboxes — React re-renders can silently uncheck them
        for f in fields:
            if f.type != "checkbox":
                continue
            ans = resolved.get(f.key)
            if ans is None or not greenhouse._is_truthy(ans.value):
                continue
            try:
                greenhouse._force_check(page, page.locator(f'[data-ae-key="{f.key}"]').first, True)
            except Exception:
                pass

        # Late discovery: EEO / custom select sections that load after field interaction.
        # Run the custom-select extractor again now that the page has had time to fully render.
        # Pass existing labels so already-handled fields (e.g. "First Name") aren't re-detected.
        existing_labels = {f.label.strip().lower() for f in fields}
        late_fields = greenhouse._extract_custom_selects(page, existing_labels=existing_labels)
        if late_fields:
            console.print(f"[bold]Found {len(late_fields)} late-loading field(s)...[/bold]")
            late_unknowns: list[tuple[greenhouse.Field, int]] = []
            for f in late_fields:
                qid, ans = resolver.try_known_resolve(
                    f.to_field_spec(), profile, source_url=url, job_location=job_location
                )
                if ans is not None:
                    resolved[f.key] = ans
                else:
                    late_unknowns.append((f, qid))

            if late_unknowns:
                late_ai = ai.answer_fields_batch(
                    [(f.key, f.to_field_spec()) for f, _ in late_unknowns],
                    profile.as_context(), resolver.get_prior_qa(),
                    job_location=job_location, job_url=url,
                )
                for f, qid in late_unknowns:
                    value = late_ai.get(f.key, "")
                    if value.strip():
                        resolved[f.key] = resolver.store_ai_answer(qid, value, source_url=url)
                    else:
                        resolved[f.key] = resolver.ResolvedAnswer(
                            value="", source="ai", question_id=qid, answer_id=0,
                        )

            for f in late_fields:
                page.wait_for_timeout(random.randint(150, 300))
                ans = resolved.get(f.key)
                if not ans or not ans.value.strip():
                    console.print(f"  [dim]custom[/dim]  {f.label} -> [dim]skipped (no answer)[/dim]")
                    continue
                try:
                    greenhouse.fill_field(page, f, ans.value)
                    tag = {"ai": "yellow", "stored": "green", "preset": "blue", "profile": "magenta"}.get(ans.source, "white")
                    console.print(f"  [{tag}]{ans.source:7}[/{tag}] {f.label} -> {_truncate(ans.value)}")
                except Exception as e:
                    console.print(f"  [red]skip[/red]    {f.label} ({e})")

        # Conditional-field pass: native selects/inputs AND custom select widgets that only
        # appear after earlier answers are filled (e.g. work-auth follow-up dropdowns).
        # Both EXTRACT_JS and _CUSTOM_SELECT_JS skip already-tagged elements, so re-running is safe.
        all_seen_labels = {f.label.strip().lower() for f in fields}
        all_seen_labels.update(f.label.strip().lower() for f in late_fields)
        page.wait_for_timeout(800)
        conditional_fields = greenhouse.extract_new_standard_fields(page, existing_labels=all_seen_labels)
        conditional_fields += greenhouse._extract_custom_selects(page, existing_labels=all_seen_labels)
        if conditional_fields:
            console.print(f"[bold]Found {len(conditional_fields)} conditional field(s)...[/bold]")
            cond_unknowns: list[tuple[greenhouse.Field, int]] = []
            for f in conditional_fields:
                if f.type == "file":
                    continue
                qid, ans = resolver.try_known_resolve(
                    f.to_field_spec(), profile, source_url=url, job_location=job_location
                )
                if ans is not None:
                    resolved[f.key] = ans
                else:
                    cond_unknowns.append((f, qid))

            if cond_unknowns:
                cond_ai = ai.answer_fields_batch(
                    [(f.key, f.to_field_spec()) for f, _ in cond_unknowns],
                    profile.as_context(), resolver.get_prior_qa(),
                    job_location=job_location, job_url=url,
                )
                for f, qid in cond_unknowns:
                    value = cond_ai.get(f.key, "")
                    if value.strip():
                        resolved[f.key] = resolver.store_ai_answer(qid, value, source_url=url)
                    else:
                        resolved[f.key] = resolver.ResolvedAnswer(
                            value="", source="ai", question_id=qid, answer_id=0,
                        )

            for f in conditional_fields:
                if f.type == "file":
                    continue
                page.wait_for_timeout(random.randint(150, 300))
                ans = resolved.get(f.key)
                if not ans or not ans.value.strip():
                    console.print(f"  [dim]cond[/dim]    {f.label} -> [dim]skipped (no answer)[/dim]")
                    continue
                try:
                    greenhouse.fill_field(page, f, ans.value)
                    tag = {"ai": "yellow", "stored": "green", "preset": "blue", "profile": "magenta"}.get(ans.source, "white")
                    console.print(f"  [{tag}]{ans.source:7}[/{tag}] {f.label} -> {_truncate(ans.value)}")
                except Exception as e:
                    console.print(f"  [red]skip[/red]    {f.label} ({e})")

        if submit and not fields:
            console.print("\n[red]No application fields detected — cannot submit.[/red]")
            report.error = "no application fields detected"
        elif submit:
            # Pre-submit screenshot: helps diagnose unfilled fields or validation state
            pre_shot = app_dir / "pre_submit.png"
            try:
                page.screenshot(path=str(pre_shot), full_page=True)
                pre_submit_path = pre_shot
                console.print(f"[dim]Pre-submit screenshot: {pre_shot}[/dim]")
            except Exception:
                pass

            console.print("\n[bold]Submitting application...[/bold]")
            page.wait_for_timeout(800)
            status = greenhouse.submit(page)

            if status == "code_required":
                sec_selector = greenhouse.find_security_code_field(page, wait_ms=3000)
                if sec_selector:
                    submit_started_at = datetime.now(timezone.utc) - timedelta(seconds=30)
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

                        console.print("[bold]Re-submitting with code...[/bold]")
                        status = greenhouse.submit(page)

            # Post-submit screenshot — taken after wait_for_function settles
            shot_path = app_dir / "post_submit.png"
            try:
                page.screenshot(path=str(shot_path), full_page=True)
                post_submit_path = shot_path
                console.print(f"[dim]Post-submit screenshot: {shot_path}[/dim]")
            except Exception:
                pass

            # Log URL + first 300 chars of visible text to help debug unknown states
            try:
                _dbg_url = page.url
                _dbg_txt = (page.evaluate("() => document.body.innerText") or "")[:300].replace("\n", " ")
                console.print(f"[dim]Post-submit URL: {_dbg_url}[/dim]")
                console.print(f"[dim]Post-submit text: {_dbg_txt}[/dim]")
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
            elif status == "invalid":
                errors = greenhouse.find_form_errors(page)
                console.print("[red]Submit failed — form has validation errors:[/red]")
                for e in errors:
                    console.print(f"  [red]✗ {e['field']}: {e['message']}[/red]")
                report.error = "; ".join(f"{e['field']}: {e['message']}" for e in errors) or "form invalid"
            else:
                # No success marker, no obvious errors. Still uncertain — but check once more
                # in case errors only appeared after the initial wait.
                errors = greenhouse.find_form_errors(page)
                if errors:
                    console.print("[red]Submit failed — form has validation errors:[/red]")
                    for e in errors:
                        console.print(f"  [red]✗ {e['field']}: {e['message']}[/red]")
                    report.error = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
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
            if report.submitted:
                final_status = "submitted"
            elif report.error:
                final_status = "failed"
            else:
                final_status = "filled"
            db.record_application(
                conn,
                url=url,
                company=meta.company,
                job_title=meta.title,
                status=final_status,
                error=report.error,
                screenshots_dir=_rel(app_dir),
                pre_submit_screenshot=_rel(pre_submit_path),
                post_submit_screenshot=_rel(post_submit_path),
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
                screenshots_dir=_rel(app_dir),
                pre_submit_screenshot=_rel(pre_submit_path),
                post_submit_screenshot=_rel(post_submit_path),
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
            gmail_addr, app_password, started_at=started_at, timeout_seconds=90
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


def _slug(s: str | None) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").lower()).strip("-")
    return (cleaned[:50] or "unknown")


def _make_app_dir(company: str | None, title: str | None) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{ts}_{_slug(company or title)}"
    d = config.DATA_DIR / "applications" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rel(p: Path | None) -> str | None:
    """Store paths relative to the repo root so the DB stays portable."""
    if p is None:
        return None
    try:
        return str(p.relative_to(config.ROOT))
    except ValueError:
        return str(p)


def _truncate(s: str, n: int = 70) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"



def _write_cover_letter_pdf(text: str, path: "Path") -> None:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_margins(left=25, top=25, right=25)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_font("Helvetica", size=11)

    for paragraph in text.split("\n"):
        stripped = paragraph.strip()
        if not stripped:
            pdf.ln(4)
        else:
            pdf.multi_cell(0, 6, stripped)
            pdf.ln(1)

    pdf.output(str(path))
