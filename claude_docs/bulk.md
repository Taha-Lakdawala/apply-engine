# bulk.py — deep-dive

371 lines. Implements `apply bulk`: opens the candidate dashboard at `https://my.greenhouse.io/dashboard`, runs a title + location search, scrapes the result links, filters by skip-list and prior submissions, then loops `runner.apply_to_url` over the survivors. **Read this when changing the search/scrape logic, the skip-list semantics, or how candidates are filtered.**

> **Self-update reminder:** edit this doc whenever you change `bulk_apply`'s signature, the `_apply_search` selector chain, the skip semantics, or the deduplication logic. The CLI command surface is in [cli.md](cli.md); the profile schema entry is in [profile.md](profile.md).

## Public surface

### `bulk_apply(profile, *, title_keyword, location, count, headless=False, submit=True, manual_submit=False, list_only=False) -> None`

End-to-end orchestration:

1. `db.init_db()` (defensive).
2. Resolve skip-list via `_skip_titles_from_profile(profile)` — uses `profile.data["bulk_apply"]["skip_titles"]` if present, else falls back to `DEFAULT_SKIP_TITLES`.
3. `_collect_listings(...)` opens the dashboard, applies the search, scrapes results. Skip-list filtering happens *during* scrape so we don't waste oversampling slots on banned titles.
4. Pull `db.submitted_urls(conn)` and drop any candidate whose normalised URL matches an already-submitted URL.
5. Truncate to `count`.
6. Print the candidate list. If `list_only=True`, return here.
7. Loop `runner.apply_to_url(...)` per candidate. Each invocation respects its own re-apply guard (so even the within-run dedup is belt-and-braces).
8. Print a final `Submitted X, failed Y of N attempted.` summary.

The function is intentionally synchronous — each `apply_to_url` call launches its own browser context (`greenhouse.with_browser`) because the persistent profile at `data/browser_profile/` can only host one Chromium process at a time. The search browser is closed before the apply loop starts; each application opens a fresh context.

### `JobListing` (dataclass)

```python
@dataclass
class JobListing:
    url: str          # normalised (no query/fragment, no trailing slash)
    title: str
    company: str | None = None
```

`company` is best-effort — pulled from a sibling element matching `[class*="company" i]` etc. Often `None` because the dashboard's DOM isn't documented anywhere stable.

### `DEFAULT_SKIP_TITLES`

Hardcoded fallback when `profile.yaml` doesn't define a skip-list. Mirrors the user-requested defaults: `director`, `manager`, `business analyst`, `ios`, `sdet`, `test`. All entries are lower-cased and matched as substrings against the scraped title (also lower-cased).

### `DASHBOARD_URL`

`https://my.greenhouse.io/dashboard`. The candidate-side portal — separate from `boards.greenhouse.io` (the public job-board hosting). Requires login.

## Internal flow

### `_collect_listings(...) -> list[JobListing]`

Spins up a Playwright context via `greenhouse.with_browser`, navigates to `DASHBOARD_URL`, then:

- Waits for `networkidle` (8s ceiling, swallowed on timeout).
- `_is_login_page(page)` — true when the URL contains `/login` / `/sign_in` / `/sign-in` / `/users/sign_in`, or a visible `input[type="password"]` exists, or a visible `Send security code` button exists (my.greenhouse.io uses email-code auth, no password). When headed: prints a message and calls `_wait_for_login(page)` to poll until the URL leaves the login route (5min timeout). When `--headless`: raises with a "re-run headed and sign in" message.
- `_apply_search(page, title_keyword, location)` — fills the search inputs and triggers submit.
- `_scrape_results(page, ...)` — collects up to `cap = max(count*4, 20)` listings.
- Always calls `cleanup()` in `finally` to close the browser.

### `_apply_search(page, title_keyword, location)`

The dashboard's DOM isn't versioned anywhere, so this function tries a chain of plausible selectors per input:

- **Title:** `input[name*="title" i]` → `input[placeholder*="title" i]` → `input[aria-label*="title" i]` → `input[name*="keyword" i]` → `input[placeholder*="keyword" i]` → `input[type="search"]`.
- **Location:** `input[name*="location" i]` → `input[placeholder*="location" i]` → `input[aria-label*="location" i]` → `input[name*="city" i]`.

If neither chain hits, raises with a "DOM may have changed — update _apply_search" message. After typing both inputs:
1. Press Enter.
2. Try clicking a `Search` button (covers forms that don't submit-on-enter).
3. Wait for `networkidle` (10s) then a 800ms tail to let lazy results render.

### `_fill_first(page, selectors, value)`

Iterates `selectors`, tries `.click()` + `.fill("")` + `.type(value, delay=30)` on the first one that resolves to a visible element. Returns `True` on first success, `False` if every selector fails. Per-selector exceptions are silently swallowed so the chain keeps walking.

### `_scrape_results(page, *, target_count, skip_titles)`

Loop up to 8 attempts:

1. Call `_read_visible_jobs(page)` (JS that scans anchors).
2. For each `{url, title, company}`:
   - Normalise the URL via `_normalize_url`. Skip if seen.
   - Skip empty titles.
   - Skip titles matching `_matches_skip(title, skip_titles)` (and log to console).
   - Append to `collected` until reaching `cap = max(target_count * 4, 20)`.
3. Once `cap` is hit, break out.
4. Otherwise call `_load_more(page)`. If it returns `False` (nothing changed), break.

The 4× over-collection buffers against the post-collect dedup against `db.submitted_urls`.

### `_read_visible_jobs(page)` — embedded JS

Scans `a[href*="greenhouse.io"]`, `a[href*="job-boards.greenhouse.io"]`, `a[href*="boards.greenhouse.io"]`. For each anchor whose href matches `/jobs/<id>`:
- Extracts title from `aria-label`, falling back to `textContent`. If the anchor wraps a heading element (`h1-h4` or `[class*="title" i]`), the heading wins.
- Tries to find a sibling `[class*="company" i]` etc. inside the closest card-like ancestor (`[class*="job" i]`, `[class*="card" i]`, `li`, `article`, `tr`, `[data-testid]`). Best-effort.
- Returns `[{url, title, company}]`.

The path-regex `/jobs/<id>` is intentionally permissive — Greenhouse uses both numeric IDs (`/jobs/12345`) and slug IDs (`/jobs/abc-def`).

### `_load_more(page)`

Tries (in order): `Load more`, `Show more`, `More results`, `Next` buttons or links, `[aria-label*="next" i]`. Each is checked with `count() && is_visible() && is_enabled()` before clicking. On click: waits for `networkidle` (4s) plus a 500ms tail.

Falls back to scrolling: compares `document.body.scrollHeight` before and after `window.scrollTo(0, scrollHeight)` + 800ms wait. Returns `True` if the page got taller (i.e. infinite scroll loaded more), `False` otherwise.

### `_is_login_page(page)`

URL contains `/login`, `/sign_in`, `/sign-in`, or `/users/sign_in` → `True`. Otherwise checks for a visible `input[type="password"]`. Both are needed because Greenhouse's auth domain may not show up in the URL but the password input always does.

### `_skip_titles_from_profile(profile)`

```python
cfg = profile.data.get("bulk_apply") or {}
titles = cfg.get("skip_titles")
if not titles:
    return DEFAULT_SKIP_TITLES
return tuple(str(t).strip().lower() for t in titles if str(t).strip())
```

Lower-cases every entry and drops blanks. Empty list in profile → falls back to `DEFAULT_SKIP_TITLES`. **To bypass the skip-list entirely**, set `bulk_apply.skip_titles: ["__never_match__"]` (or similar non-substring sentinel).

### `_matches_skip(title, skip_titles)`

Lower-cases the title and returns `True` if any entry in `skip_titles` is a substring. Substring matching means `"manager"` catches `"Engineering Manager"` and `"Product Manager"`; `"ios"` also catches `"BIOS Engineer"` (false positive — accept it, since the alternative is regex complexity).

### `_normalize_url(url)`

Drops query string, fragment, and trailing slash via `urlparse`/`urlunparse`. Used both for in-run dedup (different links to the same job page, same anchor variants) and for matching against `db.submitted_urls` (which also normalises on read in `bulk_apply`). The canonical form passed to `apply_to_url` is the normalised one — the runner's own re-apply guard then sees the same string on subsequent runs.

## Common edits

- **Selectors stop matching after a Greenhouse redesign:** update `_apply_search`'s `title_selectors` / `location_selectors` chains. Keep the chain order from most-specific to most-generic.
- **Pagination control changed:** edit `_load_more`. The fallback scroll catches infinite-scroll layouts even when no button is found.
- **Skip-list logic:** `_matches_skip` is substring-based for simplicity. If you need word-boundary matching, switch to `re.search(rf"\b{re.escape(s)}\b", t)`.
- **Reuse the search browser for the apply loop:** would require threading the existing `page_factory` through `runner.apply_to_url`. Significant surgery (the runner currently owns its own context). Today we eat the per-job context-launch cost (~2-3s).
- **Add a `--days-back` filter:** scrape posting dates from the result cards in `_read_visible_jobs` (add a sibling lookup similar to the `company` extraction), then filter in `_scrape_results`.
- **Show progress bar:** Rich's `Progress` would slot into the `for i, job in enumerate(queue, 1)` loop. Keep `apply_to_url`'s console output uninterrupted — it already prints rich status lines.

## Gotchas

- **Login is assumed.** The function fails fast if the dashboard redirects to login; it does **not** prompt for credentials. The user is expected to have signed in once with the persistent browser profile (`data/browser_profile/`) so the session cookie is cached. If you need an interactive login flow, add a separate `apply login` command rather than embedding it here.
- **The `/jobs/` regex is greedy.** `/jobs/<numeric>` and `/jobs/<slug>` both match. If Greenhouse adds a non-job URL pattern under `/jobs/` (e.g. `/jobs/categories/foo`), it'd slip through. Filter in `_read_visible_jobs` if that becomes a problem.
- **Substring skip matches over-fire.** `"ios"` catches `"BIOS"`, `"test"` catches `"Attestation"`, `"manager"` catches `"Mangroves"`. Accepted trade-off — the user's skip list was given as keywords, not regexes.
- **`_normalize_url` is one-sided.** It only normalises URLs we *collect*. URLs already in `applications.url` may have query strings (if a user passed `apply <url>` with one). When matching against `db.submitted_urls`, we normalise both sides in `bulk_apply`, so dedup works — but `runner.find_successful_application` does NOT normalise. If a stored URL has query params and we pass the normalised version, the runner's own guard misses and we re-attempt. Mitigation: bulk's pre-filter catches it before `apply_to_url` is called.
- **Per-job browser launches are sequential, not concurrent.** Chromium's user-data-dir is single-instance. Don't try to parallelise without solving profile sharding first.
- **Scraping doesn't dismiss overlays.** Cookie banners or modal popups on first dashboard visit may obscure the search inputs. If selector-fill fails consistently after a redesign, check whether an overlay needs dismissing — `greenhouse._dismiss_overlays(page)` exists and could be reused.
- **`list_only` doesn't open the browser any less.** It still runs the full search + scrape; it just stops before the apply loop. Useful for verifying the search returns sane results.
- **The skip list is profile-scoped, not flag-scoped.** No `--skip-titles` CLI flag. To temporarily change the skip set, edit `profile.yaml` for that run.
