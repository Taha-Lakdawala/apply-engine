from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout
from rich.console import Console

from . import db, email_fetcher, greenhouse
from .profile import Profile
from .runner import apply_to_url

console = Console()

DASHBOARD_URL = "https://my.greenhouse.io/dashboard"

# Used when profile.yaml doesn't define `bulk_apply.skip_titles`. Lower-cased,
# matched as substring against scraped job titles.
DEFAULT_SKIP_TITLES: tuple[str, ...] = (
    "director",
    "manager",
    "business analyst",
    "ios",
    "sdet",
    "test",
)


@dataclass
class JobListing:
    url: str
    title: str
    company: str | None = None


def bulk_apply(
    profile: Profile,
    *,
    title_keyword: str,
    location: str,
    count: int,
    headless: bool = False,
    submit: bool = True,
    manual_submit: bool = False,
    list_only: bool = False,
) -> None:
    """Search the candidate dashboard, then run :func:`apply_to_url` per result."""
    db.init_db()

    skip_titles = _skip_titles_from_profile(profile)
    console.print(
        f"[bold]Bulk apply[/bold] — title=[cyan]{title_keyword}[/cyan] "
        f"location=[cyan]{location}[/cyan] target=[cyan]{count}[/cyan]"
    )
    if skip_titles:
        console.print(f"[dim]Skipping titles containing: {', '.join(skip_titles)}[/dim]")

    listings = _collect_listings(
        profile=profile,
        title_keyword=title_keyword,
        location=location,
        target_count=count,
        skip_titles=skip_titles,
        headless=headless,
    )

    with db.connect() as conn:
        applied = db.submitted_urls(conn)
    applied_norm = {_normalize_url(u) for u in applied}

    queue: list[JobListing] = []
    for j in listings:
        if _normalize_url(j.url) in applied_norm:
            console.print(f"[dim]skip already applied: {j.title}[/dim]")
            continue
        queue.append(j)
        if len(queue) >= count:
            break

    if not queue:
        console.print("[yellow]No new jobs to apply to.[/yellow]")
        return

    console.print(f"\n[bold]Found {len(queue)} candidate job(s)[/bold]")
    for i, job in enumerate(queue, 1):
        console.print(f"  [dim]{i}.[/dim] {job.title}  [dim]{job.url}[/dim]")

    if list_only:
        return

    submitted = 0
    failed = 0
    for i, job in enumerate(queue, 1):
        console.print(f"\n[bold cyan]({i}/{len(queue)})[/bold cyan] {job.title}")
        console.print(f"[dim]{job.url}[/dim]")
        try:
            report = apply_to_url(
                job.url,
                profile,
                headless=headless,
                submit=submit,
                manual_submit=manual_submit,
            )
            if report.submitted:
                submitted += 1
            elif report.error:
                failed += 1
        except Exception as e:  # defensive — apply_to_url already wraps its own try/except
            failed += 1
            console.print(f"[red]Failed: {e}[/red]")

    console.print(
        f"\n[bold]Done.[/bold] Submitted [green]{submitted}[/green], "
        f"failed [red]{failed}[/red] of {len(queue)} attempted."
    )


def _skip_titles_from_profile(profile: Profile) -> tuple[str, ...]:
    cfg = profile.data.get("bulk_apply") or {}
    titles = cfg.get("skip_titles")
    if not titles:
        return DEFAULT_SKIP_TITLES
    return tuple(str(t).strip().lower() for t in titles if str(t).strip())


def _collect_listings(
    *,
    profile: Profile,
    title_keyword: str,
    location: str,
    target_count: int,
    skip_titles: tuple[str, ...],
    headless: bool,
) -> list[JobListing]:
    pw, _, page_factory, cleanup = greenhouse.with_browser(headless=headless)
    try:
        page = page_factory()
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass

        if _is_login_page(page):
            console.print("[yellow]Not signed in to my.greenhouse.io.[/yellow]")
            email_addr = (profile.data.get("personal") or {}).get("email") or ""
            gmail_pw = os.environ.get("GMAIL_APP_PASSWORD") or ""
            if email_addr.endswith("@gmail.com") and gmail_pw:
                console.print(f"[dim]Auto-login as {email_addr} via emailed code...[/dim]")
                _auto_login_via_email(page, email_addr, gmail_pw)
            elif headless:
                raise RuntimeError(
                    "Not logged in to my.greenhouse.io and auto-login not configured. "
                    "Set GMAIL_APP_PASSWORD in .env (matching profile.yaml personal.email), "
                    "or re-run without --headless and sign in manually."
                )
            else:
                console.print(
                    "Sign in using the open browser window — I'll wait up to 5 minutes."
                )
                _wait_for_login(page)
            console.print(
                "[green]Signed in.[/green] [dim]Cookies persisted to "
                "data/browser_profile/ — future runs will reuse this session.[/dim]"
            )
            if "/dashboard" not in page.url:
                page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass

        _apply_search(page, title_keyword=title_keyword, location=location)
        return _scrape_results(
            page,
            target_count=target_count,
            skip_titles=skip_titles,
        )
    finally:
        cleanup()


def _is_login_page(page: Page) -> bool:
    url = (page.url or "").lower()
    if any(tok in url for tok in ("/login", "/sign_in", "/sign-in", "/users/sign_in")):
        return True
    # my.greenhouse.io's email-code login has no password input but does show
    # an email field on the sign-in page. Fall back to that signal too.
    try:
        if page.locator('input[type="password"]:visible').count() > 0:
            return True
        if page.locator('button:has-text("Send security code"):visible').count() > 0:
            return True
    except Exception:
        pass
    return False


def _auto_login_via_email(page: Page, email_addr: str, gmail_app_password: str) -> None:
    """Email-code sign-in for my.greenhouse.io.

    Flow: dismiss cookie banner → fill email → click Send security code →
    poll Gmail (IMAP) for the freshest matching email → type code into the
    8-cell OTP form → click Submit → wait until the URL leaves /sign_in.
    """
    # Cookie banner — non-fatal if missing.
    for sel in ('button:has-text("Ok")', 'button:has-text("Accept")', 'button:has-text("Got it")'):
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(300)
        except Exception:
            pass

    # If we already landed on the OTP step (rare — e.g. retry), skip the email step.
    if not _otp_visible(page):
        try:
            email_input = page.locator(
                'input[type="email"], input#email-address, input[placeholder*="Email" i]'
            ).first
            email_input.click()
            email_input.fill("")
            email_input.type(email_addr, delay=30)
        except Exception as e:
            raise RuntimeError(f"Couldn't fill the sign-in email field: {e}")

        # Greenhouse rate-limits Send-security-code requests; the prior code is
        # still valid for 10 minutes, so look back that far rather than only
        # accepting codes emitted *after* this exact click.
        started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        try:
            page.locator(
                'button:has-text("Send security code"), button:has-text("Continue")'
            ).first.click()
        except Exception as e:
            raise RuntimeError(f"Couldn't click Send security code: {e}")
    else:
        started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(1500)

    code = email_fetcher.fetch_security_code(
        email_addr,
        gmail_app_password,
        started_at=started_at,
        timeout_seconds=120,
        poll_interval=3.0,
    )
    if not code:
        raise RuntimeError(
            "Didn't receive a Greenhouse sign-in code within 120s. "
            "Check Gmail for a code and re-run."
        )
    console.print(f"[dim]Got code {code[:2]}***{code[-2:]} (len={len(code)})[/dim]")

    # The OTP form is N single-character cells (currently 8). Type the whole
    # string into the first focused cell — browsers auto-advance per keystroke.
    if not _otp_visible(page):
        raise RuntimeError("OTP form didn't render after Send security code click.")
    try:
        first_cell = page.locator('input[id="\\:r0\\:-0"]').first
        if first_cell.count() == 0:
            first_cell = page.locator('input[type="text"]').first
        first_cell.click()
        page.keyboard.type(code, delay=80)
    except Exception as e:
        raise RuntimeError(f"Couldn't enter the OTP code: {e}")

    page.wait_for_timeout(800)
    try:
        page.locator('button:has-text("Submit")').first.click()
    except Exception as e:
        raise RuntimeError(f"Couldn't click Submit on OTP form: {e}")

    _wait_for_login(page, timeout_seconds=30)


def _otp_visible(page: Page) -> bool:
    try:
        return page.locator('input[id="\\:r0\\:-0"]').count() > 0
    except Exception:
        return False


def _wait_for_login(page: Page, timeout_seconds: int = 300) -> None:
    """Poll until we're no longer on a login page (or timeout)."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _is_login_page(page):
            return
        page.wait_for_timeout(1500)
    raise RuntimeError(
        f"Login wait timed out after {timeout_seconds}s. "
        "Please sign in to my.greenhouse.io and re-run."
    )


def _apply_search(page: Page, *, title_keyword: str, location: str) -> None:
    """Fill the dashboard's title + location inputs and trigger the Search button.

    Layout (as of 2026): a plain `<input placeholder="Search for a job title">`
    next to a react-select combobox for location, then a "Search" submit button.
    The combobox needs a typeahead pick — pressing Enter selects the first
    suggestion that matches what was typed.
    """
    # Wait for the search bar to actually render — `domcontentloaded` returns
    # before React hydrates on cold loads.
    try:
        page.wait_for_selector(
            'input[placeholder*="job title" i], input[placeholder*="title" i]',
            timeout=10000,
        )
    except PlaywrightTimeout:
        raise RuntimeError(
            "Search bar didn't render on my.greenhouse.io/dashboard within 10s. "
            "If the dashboard layout changed, update _apply_search in apply_engine/bulk.py."
        )

    title_selectors = [
        'input[placeholder*="job title" i]',
        'input[placeholder*="title" i]',
        'input[placeholder*="keyword" i]',
        'input[aria-label*="title" i]',
    ]
    if not _fill_first(page, title_selectors, title_keyword):
        raise RuntimeError(
            "Couldn't find the title/keyword search input on my.greenhouse.io/dashboard."
        )

    if not _fill_location_combobox(page, location):
        raise RuntimeError(
            "Couldn't fill the location combobox on my.greenhouse.io/dashboard."
        )

    # Click the explicit Search button (form has no submit-on-enter behaviour).
    clicked = False
    for sel in (
        'button:has-text("Search")',
        '[role="button"]:has-text("Search")',
        'button[type="submit"]',
    ):
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        # Fallback: submit-on-enter from the title input.
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(1200)


def _fill_location_combobox(page: Page, location: str) -> bool:
    """The dashboard location field is a react-select combobox — type then pick first option."""
    selectors = (
        'input.select__input',
        'input[id^="react-select"][type="text"]',
        '[role="combobox"][aria-controls]',
    )
    target = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count():
                target = loc
                break
        except Exception:
            continue
    if target is None:
        return False
    try:
        target.click()
        target.fill("")
    except Exception:
        pass
    try:
        target.type(location, delay=40)
    except Exception:
        return False
    page.wait_for_timeout(800)  # let suggestions render
    # Pick the first suggestion matching what we typed.
    for sel in (
        f'[role="option"]:has-text("{location}")',
        '[role="option"]',
        '.select__option',
    ):
        try:
            opt = page.locator(sel).first
            if opt.count() and opt.is_visible():
                opt.click()
                return True
        except Exception:
            continue
    # Fallback: press Enter to commit the typed value.
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _fill_first(page: Page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.click()
            try:
                loc.fill("")
            except Exception:
                pass
            loc.type(value, delay=30)
            return True
        except Exception:
            continue
    return False


def _scrape_results(
    page: Page,
    *,
    target_count: int,
    skip_titles: tuple[str, ...],
) -> list[JobListing]:
    """Walk rendered job links, paginating/scrolling until we have enough.

    Over-collects ~4x the target so the dedup + skip-list filter still leaves
    enough survivors for the apply loop."""
    seen: set[str] = set()
    collected: list[JobListing] = []
    cap = max(target_count * 4, 20)

    for _attempt in range(8):
        for item in _read_visible_jobs(page):
            url = _normalize_url(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            title = (item.get("title") or "").strip()
            if not title:
                continue
            if _matches_skip(title, skip_titles):
                console.print(f"[dim]skip ({title}): matches skip-list[/dim]")
                continue
            collected.append(
                JobListing(url=url, title=title, company=item.get("company"))
            )
            if len(collected) >= cap:
                break
        if len(collected) >= cap:
            break
        if not _load_more(page):
            break

    return collected


def _read_visible_jobs(page: Page) -> list[dict[str, str]]:
    return page.evaluate(
        r"""
        () => {
          const out = [];
          const seen = new Set();
          const anchors = document.querySelectorAll(
            'a[href*="greenhouse.io"], '
            + 'a[href*="job-boards.greenhouse.io"], '
            + 'a[href*="boards.greenhouse.io"]'
          );
          for (const a of anchors) {
            const href = a.href || '';
            if (!/\/jobs\/\d+|\/jobs\/[A-Za-z0-9_-]+/.test(href)) continue;
            if (seen.has(href)) continue;
            seen.add(href);

            // The dashboard renders each result as a card where the "View job"
            // anchor sits in a child row, separate from the heading. Walk up
            // until we find an ancestor containing an h1-h4, then read the
            // first heading's text as the title.
            let title = null;
            let company = null;
            let card = a;
            for (let i = 0; i < 8 && card; i++) {
              card = card.parentElement;
              if (!card) break;
              const heading = card.querySelector('h1, h2, h3, h4');
              if (heading) {
                title = (heading.textContent || '').replace(/\s+/g, ' ').trim();
                // Company is usually the line right after the title in the
                // same card. Look for a sibling/nearby element.
                let companyEl =
                  heading.nextElementSibling
                  || (heading.parentElement && heading.parentElement.nextElementSibling)
                  || card.querySelector('[class*="company" i], [class*="employer" i]');
                if (companyEl) {
                  const t = (companyEl.textContent || '').replace(/\s+/g, ' ').trim();
                  if (t && t !== title) company = t;
                }
                break;
              }
            }

            // Last-resort fallbacks for non-standard layouts.
            if (!title) {
              const heading = a.querySelector('h1, h2, h3, h4, [class*="title" i]');
              title = heading
                ? (heading.textContent || '').replace(/\s+/g, ' ').trim()
                : (a.getAttribute('aria-label') || a.textContent || '').replace(/\s+/g, ' ').trim();
            }
            // Drop generic CTA labels that aren't real titles.
            if (title && /^(view job|apply|view)$/i.test(title)) {
              continue;
            }
            out.push({ url: href, title: title || '', company });
          }
          return out;
        }
        """
    )


def _load_more(page: Page) -> bool:
    """Click a Load-more / Next control if present; otherwise scroll. Returns True if anything advanced."""
    selectors = (
        'button:has-text("Load more")',
        'button:has-text("Show more")',
        'button:has-text("More results")',
        'button:has-text("Next")',
        'a:has-text("Next")',
        '[aria-label*="next" i]',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible() and loc.is_enabled():
                loc.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue

    before = page.evaluate("() => document.body.scrollHeight")
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(800)
    after = page.evaluate("() => document.body.scrollHeight")
    return after > before


def _matches_skip(title: str, skip_titles: tuple[str, ...]) -> bool:
    t = (title or "").lower()
    return any(s in t for s in skip_titles)


def _normalize_url(url: str) -> str:
    """Strip query/fragment and trailing slash so the same job page dedupes."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url
