"""FastAPI dashboard for apply-engine.

Read-only views over the SQLite DB (`applications`, `questions`, `answers`)
plus profile.yaml editing. Screenshots are served from anywhere under
`config.DATA_DIR`. The frontend is a built React app in `webui/dist/`.

All write paths are scoped tightly:
- profile.yaml writes go through PUT /api/profile (full-document replace,
  with .bak backup written next to the file).
- Answer edits go through PUT /api/answers/{question_id} (inserts a new
  reviewed row, mirroring `apply edit`).

Nothing here triggers Playwright. By design — see CLAUDE.md.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, db


WEBUI_DIST = config.ROOT / "webui" / "dist"


# ---------- response models ----------


class ProfileBody(BaseModel):
    data: dict[str, Any]


class AnswerBody(BaseModel):
    value: str


# ---------- helpers ----------


_INIT_DONE = False


def _ensure_init() -> None:
    global _INIT_DONE
    if not _INIT_DONE:
        db.init_db()
        _INIT_DONE = True


def _company_from_title(title: str | None) -> str | None:
    """`Job Application for Foo at Bar` → `Bar`. Many old rows have no
    company column populated but the title encodes it."""
    if not title:
        return None
    if " at " in title:
        return title.rsplit(" at ", 1)[1].strip()
    return None


def _role_from_title(title: str | None) -> str | None:
    if not title:
        return None
    if title.startswith("Job Application for ") and " at " in title:
        return title[len("Job Application for ") :].rsplit(" at ", 1)[0].strip()
    return title


def _safe_screenshot_path(rel_or_abs: str) -> Path | None:
    """Resolve a screenshot path and confirm it lives under DATA_DIR.

    Both relative paths (from older rows) and absolute ones flow through
    here. Anything that escapes DATA_DIR is rejected — screenshots aren't
    user-supplied but the path traversal guard is cheap insurance against
    a future bug."""
    if not rel_or_abs:
        return None
    p = Path(rel_or_abs)
    if not p.is_absolute():
        # try data dir, then repo root
        for base in (config.DATA_DIR, config.ROOT):
            cand = (base / p).resolve()
            if cand.exists():
                p = cand
                break
        else:
            return None
    else:
        p = p.resolve()
    try:
        p.relative_to(config.DATA_DIR.resolve())
    except ValueError:
        return None
    return p if p.exists() else None


# ---------- routes ----------


def create_app() -> FastAPI:
    app = FastAPI(title="apply-engine dashboard", docs_url=None, redoc_url=None)
    _ensure_init()

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
            ).fetchall()
            by_status = {r["status"]: r["n"] for r in rows}
            total = sum(by_status.values())

            day_rows = conn.execute(
                """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n
                     FROM applications
                    GROUP BY day ORDER BY day"""
            ).fetchall()
            per_day = [{"day": r["day"], "count": r["n"]} for r in day_rows]

            company_rows = conn.execute(
                """SELECT company, job_title, COUNT(*) AS n
                     FROM applications
                    WHERE status = 'submitted'
                    GROUP BY company, job_title"""
            ).fetchall()
        company_counts: Counter[str] = Counter()
        for r in company_rows:
            c = r["company"] or _company_from_title(r["job_title"]) or "Unknown"
            company_counts[c] += r["n"]
        top_companies = [
            {"company": c, "count": n}
            for c, n in company_counts.most_common(10)
        ]

        with db.connect() as conn:
            q_total = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
            a_total = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
            ai_unreviewed = conn.execute(
                """SELECT COUNT(*) FROM answers a
                    WHERE a.ai_generated = 1 AND a.reviewed_at IS NULL
                      AND a.id = (SELECT id FROM answers
                                   WHERE question_id = a.question_id
                                   ORDER BY created_at DESC LIMIT 1)"""
            ).fetchone()[0]

        return {
            "total_applications": total,
            "by_status": by_status,
            "submitted": by_status.get("submitted", 0),
            "failed": by_status.get("failed", 0),
            "filled": by_status.get("filled", 0),
            "per_day": per_day,
            "top_companies": top_companies,
            "questions_total": q_total,
            "answers_total": a_total,
            "ai_unreviewed": ai_unreviewed,
        }

    @app.get("/api/applications")
    def list_applications(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with db.connect() as conn:
            if status:
                rows = conn.execute(
                    """SELECT id, url, company, job_title, status, submitted_at, created_at, error
                         FROM applications WHERE status = ?
                        ORDER BY id DESC LIMIT ?""",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, url, company, job_title, status, submitted_at, created_at, error
                         FROM applications ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "url": r["url"],
                "company": r["company"] or _company_from_title(r["job_title"]),
                "job_title": _role_from_title(r["job_title"]),
                "raw_title": r["job_title"],
                "status": r["status"],
                "submitted_at": r["submitted_at"],
                "created_at": r["created_at"],
                "error": r["error"],
            })
        return out

    @app.get("/api/applications/{app_id}")
    def application_detail(app_id: int) -> dict[str, Any]:
        with db.connect() as conn:
            row = conn.execute(
                """SELECT id, url, company, job_title, status, submitted_at, created_at,
                          error, screenshots_dir, pre_submit_screenshot, post_submit_screenshot
                     FROM applications WHERE id = ?""",
                (app_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="application not found")

            qa_rows = conn.execute(
                """SELECT q.id AS qid, q.raw_text, q.field_type, q.options_json,
                          a.id AS aid, a.value, a.ai_generated, a.reviewed_at, a.created_at
                     FROM answers a
                     JOIN questions q ON q.id = a.question_id
                    WHERE a.source_url = ?
                    ORDER BY a.id ASC""",
                (row["url"],),
            ).fetchall()

        qa = [
            {
                "question_id": r["qid"],
                "answer_id": r["aid"],
                "question": r["raw_text"],
                "field_type": r["field_type"],
                "options": json.loads(r["options_json"]) if r["options_json"] else None,
                "value": r["value"],
                "ai_generated": bool(r["ai_generated"]),
                "reviewed_at": r["reviewed_at"],
                "created_at": r["created_at"],
            }
            for r in qa_rows
        ]

        screenshots: list[dict[str, str]] = []
        # 1) explicit columns
        for label, col in [("pre-submit", "pre_submit_screenshot"), ("post-submit", "post_submit_screenshot")]:
            if row[col]:
                p = _safe_screenshot_path(row[col])
                if p:
                    screenshots.append({"label": label, "path": str(p.relative_to(config.DATA_DIR))})
        # 2) directory glob
        if row["screenshots_dir"]:
            d = _safe_screenshot_path(row["screenshots_dir"])
            if d and d.is_dir():
                for img in sorted(d.glob("*.png")):
                    rel = str(img.relative_to(config.DATA_DIR))
                    if not any(s["path"] == rel for s in screenshots):
                        screenshots.append({"label": img.name, "path": rel})

        return {
            "id": row["id"],
            "url": row["url"],
            "company": row["company"] or _company_from_title(row["job_title"]),
            "job_title": _role_from_title(row["job_title"]),
            "raw_title": row["job_title"],
            "status": row["status"],
            "submitted_at": row["submitted_at"],
            "created_at": row["created_at"],
            "error": row["error"],
            "screenshots": screenshots,
            "qa": qa,
        }

    @app.get("/api/screenshots/{path:path}")
    def screenshot(path: str):
        p = _safe_screenshot_path(path)
        if not p:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(p)

    @app.get("/api/profile")
    def get_profile() -> dict[str, Any]:
        if not config.PROFILE_PATH.exists():
            return {"data": {}, "exists": False, "path": str(config.PROFILE_PATH)}
        with config.PROFILE_PATH.open() as f:
            data = yaml.safe_load(f) or {}
        return {"data": data, "exists": True, "path": str(config.PROFILE_PATH)}

    @app.put("/api/profile")
    def put_profile(body: ProfileBody) -> dict[str, Any]:
        # Backup the previous version with a timestamp so accidental edits
        # via the UI are recoverable from disk.
        if config.PROFILE_PATH.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = config.PROFILE_PATH.with_suffix(f".yaml.bak.{ts}")
            backup.write_bytes(config.PROFILE_PATH.read_bytes())
        text = yaml.safe_dump(body.data, sort_keys=False, allow_unicode=True)
        config.PROFILE_PATH.write_text(text)
        return {"ok": True, "bytes": len(text)}

    @app.get("/api/questions")
    def list_questions() -> list[dict[str, Any]]:
        with db.connect() as conn:
            pairs = db.all_qa_pairs(conn)
        return [
            {
                "question_id": q.id,
                "answer_id": a.id,
                "question": q.raw_text,
                "field_type": q.field_type,
                "options": q.options,
                "value": a.value,
                "ai_generated": a.ai_generated,
                "reviewed_at": a.reviewed_at,
                "created_at": a.created_at,
            }
            for q, a in pairs
        ]

    @app.put("/api/answers/{question_id}")
    def update_answer(question_id: int, body: AnswerBody) -> dict[str, Any]:
        with db.connect() as conn:
            row = conn.execute("SELECT id FROM questions WHERE id = ?", (question_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="question not found")
            new_id = db.update_answer_value(conn, question_id, body.value)
            conn.commit()
        return {"ok": True, "answer_id": new_id}

    # ---------- static frontend ----------

    if WEBUI_DIST.exists():
        # Serve hashed assets first; fall through to index.html for any
        # client-side route. Mounted last so it doesn't shadow /api.
        assets_dir = WEBUI_DIST / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        index_html = WEBUI_DIST / "index.html"

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404)
            target = WEBUI_DIST / full_path
            if full_path and target.is_file():
                return FileResponse(target)
            if index_html.exists():
                return FileResponse(index_html)
            raise HTTPException(status_code=404)
    else:
        @app.get("/")
        def missing_ui() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "frontend not built",
                    "hint": f"Run `cd {config.ROOT}/webui && npm install && npm run build`.",
                },
            )

    return app


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import threading
    import time
    import webbrowser

    import uvicorn

    if open_browser:
        def _open() -> None:
            time.sleep(0.6)
            webbrowser.open(f"http://{host}:{port}")
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(create_app(), host=host, port=port, log_level="info")
