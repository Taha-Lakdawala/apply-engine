# [apply_engine/web/server.py](../apply_engine/web/server.py)

FastAPI dashboard. Served by `apply dashboard`. Read-only over the SQLite DB except for three writes: `PUT /api/profile` (rewrites `profile.yaml`, with timestamped `.bak` backup), `PUT /api/answers/{question_id}` (inserts a new manual answer row, mirroring `apply edit`), and `DELETE /api/questions/{question_id}` (drops a cached question; answers cascade).

By design this server **never triggers Playwright**. Adding a "run application from the UI" button means subprocess management, log streaming, and browser conflicts — out of scope for now.

## Files

| File | Purpose |
|---|---|
| `apply_engine/web/server.py` | FastAPI app, all routes, SPA fallback. |
| `apply_engine/web/__init__.py` | Re-exports `create_app`, `run`. |
| `webui/` | React + Vite frontend. Built output in `webui/dist/`. |
| `webui/dist/` | Built static assets served by FastAPI. **Required** at runtime — `apply dashboard` errors out if missing. |

## Endpoints

All under `/api/`.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/stats` | Counts by status, per-day timeline, top companies, question/answer totals, AI-unreviewed count. |
| GET | `/api/applications?status=&limit=` | List rows from `applications`. Status filter is exact match. Default limit 200. |
| GET | `/api/applications/{id}` | One row + Q&A (joined via `answers.source_url == applications.url`) + screenshots. |
| GET | `/api/screenshots/{path:path}` | Streams an image file. Path must resolve under `config.DATA_DIR` (traversal guard in `_safe_screenshot_path`). |
| GET | `/api/profile` | Parsed `profile.yaml`. Returns `{exists: false}` if the file is missing instead of 404. |
| PUT | `/api/profile` | Body: `{data: {...}}`. Backs up existing file to `profile.yaml.bak.<timestamp>`, then writes the new YAML via `yaml.safe_dump(sort_keys=False)`. |
| GET | `/api/questions` | All `(question, latest answer)` pairs via `db.all_qa_pairs`. |
| PUT | `/api/answers/{question_id}` | Body: `{value: "..."}`. Inserts a new answer row (`ai_generated=0`, `reviewed_at=now`) via `db.update_answer_value`. |
| DELETE | `/api/questions/{question_id}` | Deletes the question; answers cascade. 404 if the id is unknown. |

## Q&A linkage

The schema has no `application_id` on `answers` — Q&A is correlated to an application **by `answers.source_url == applications.url`**. The runner sets `source_url=url` when it inserts answers (see `runner.py`). If you ever rewrite URLs (canonicalize query strings, strip `gh_src`, etc.) for application matching, do the same for the join here or the per-application Q&A list will go blank. Right now both sides store the raw URL.

## Title parsing

Many older `applications` rows have an empty `company` column but a `job_title` like `"Job Application for <role> at <company>"`. `_company_from_title` and `_role_from_title` split that. Don't rely on the column — always go through the helpers when building list/detail responses.

## Screenshot resolution (`_safe_screenshot_path`)

Three sources, in order:

1. `applications.pre_submit_screenshot` / `post_submit_screenshot` columns (full or relative paths).
2. `applications.screenshots_dir` — globbed for `*.png`.
3. Anything else: 404.

Whatever path comes in, it's resolved against `DATA_DIR` first, then `ROOT`, and finally checked `relative_to(DATA_DIR)`. Anything outside `DATA_DIR` is rejected. Cheap insurance — paths come from our own DB but defense in depth is free.

## Static asset serving

If `webui/dist/` exists:
- `/assets/*` → `webui/dist/assets/*` via `StaticFiles`.
- Anything else → either the file at that path, or `index.html` (SPA fallback). Mounted **last** so it doesn't shadow `/api/*`.
- `full_path.startswith("api/")` is explicitly rejected — without it, an unknown `/api/foo` would return `index.html` and the frontend would try to JSON-parse HTML.

If `webui/dist/` is missing, `/` returns a 503 with a hint to run `npm run build`.

## DB connection

`db.connect()` is a context manager that opens a fresh connection per request, commits on exit, and closes. Reusing it here keeps connection lifecycle consistent with the CLI. `_ensure_init()` runs once on app creation to apply `db.SCHEMA` migrations.

## Frontend (`webui/`)

React 18 + Vite + react-router-dom. No state library (just `useState`/`useEffect`), no UI kit (custom CSS in `src/styles.css`).

| File | Role |
|---|---|
| `src/api.ts` | Typed fetch wrappers + response types. Single `req<T>` helper, throws on non-2xx. |
| `src/App.tsx` | Sidebar layout + routes. |
| `src/pages/Dashboard.tsx` | Cards (totals, status counts, AI unreviewed), 30-day bar chart, recent applications, top companies. Exports `relTime` and `ApplicationsTable` reused by other pages. |
| `src/pages/Applications.tsx` | Filterable table (status dropdown + free-text search). |
| `src/pages/ApplicationDetail.tsx` | Per-application Q&A + screenshots with click-to-zoom lightbox. |
| `src/pages/Profile.tsx` | Section-by-section editor (personal/location/links/work-auth/demographics/bio/resume/preset-answers). Saves the whole document via `PUT /api/profile`. |
| `src/pages/Questions.tsx` | Searchable Q&A table with inline edit (PUT to `/api/answers/{qid}`) and delete (DELETE `/api/questions/{qid}`, guarded by `window.confirm`). |

### Dev mode

`cd webui && npm run dev` starts Vite on `:5173` with `/api` proxied to `127.0.0.1:8765`. Run `apply dashboard` in another terminal to provide the backend.

### Build

`cd webui && npm install && npm run build` writes to `webui/dist/`. The dist is **not** in `.gitignore` here — check it in so `apply dashboard` works without a node toolchain on the user's machine. (If you change that, document the build step in `apply dashboard`'s help.)

## CLI integration

`apply dashboard [--host 127.0.0.1] [--port 8765] [--no-browser]` calls `server.run(...)`, which:

1. Spawns a daemon thread that waits 600ms then opens `http://host:port` in the default browser.
2. Calls `uvicorn.run(create_app(), ...)` — single worker, blocking.

The 600ms is long enough for uvicorn to bind in practice. Don't tune it down without testing on a cold machine.

## Gotchas

- **Q&A goes blank when URL changes.** Re-applies hit the same URL so this is fine in practice, but URL normalization elsewhere would break the join. Search for `answers.source_url` before touching application URL handling.
- **Profile YAML round-trip is lossy for comments.** The example file has explanatory comments; saving via the UI strips them. The form covers every documented section, so the rendered file is still valid — just bare. If a user wants comments preserved, they should hand-edit `profile.yaml` (or we'd need `ruamel.yaml`).
- **`_safe_screenshot_path` enforces `DATA_DIR` containment.** Don't move screenshots outside `data/` without updating the resolver, or the API will start 404'ing them.
- **No auth.** The server binds to `127.0.0.1` by default. If you ever expose it on `0.0.0.0`, add auth — `PUT /api/profile` rewrites a file on disk.
