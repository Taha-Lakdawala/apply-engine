from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL UNIQUE,
    raw_text        TEXT NOT NULL,
    field_type      TEXT NOT NULL,
    options_json    TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id     INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    value           TEXT NOT NULL,
    ai_generated    INTEGER NOT NULL DEFAULT 0,
    reviewed_at     TEXT,
    source_url      TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answers_question_created
    ON answers(question_id, created_at DESC);

CREATE TABLE IF NOT EXISTS applications (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    url                     TEXT NOT NULL,
    company                 TEXT,
    job_title               TEXT,
    status                  TEXT NOT NULL,
    submitted_at            TEXT,
    created_at              TEXT NOT NULL,
    error                   TEXT,
    screenshots_dir         TEXT,
    pre_submit_screenshot   TEXT,
    post_submit_screenshot  TEXT,
    prior_attempts          TEXT   -- JSON array of past failed-attempt snapshots
);

CREATE INDEX IF NOT EXISTS idx_applications_url_status
    ON applications(url, status);
"""

# Columns added after the initial schema. `init_db` ALTERs existing DBs to add
# any that are missing — sqlite has no IF NOT EXISTS for ADD COLUMN, so we
# swallow OperationalError when the column is already present.
_APPLICATIONS_MIGRATIONS = [
    ("screenshots_dir", "TEXT"),
    ("pre_submit_screenshot", "TEXT"),
    ("post_submit_screenshot", "TEXT"),
    ("prior_attempts", "TEXT"),
]


@dataclass
class Question:
    id: int
    fingerprint: str
    raw_text: str
    field_type: str
    options: list[str] | None


@dataclass
class Answer:
    id: int
    question_id: int
    value: str
    ai_generated: bool
    reviewed_at: str | None
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for col, decl in _APPLICATIONS_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass


def upsert_question(
    conn: sqlite3.Connection,
    fingerprint: str,
    raw_text: str,
    field_type: str,
    options: list[str] | None,
) -> Question:
    options_json = json.dumps(options) if options else None
    now = _now()
    row = conn.execute(
        "SELECT id, fingerprint, raw_text, field_type, options_json FROM questions WHERE fingerprint = ?",
        (fingerprint,),
    ).fetchone()
    if row:
        conn.execute("UPDATE questions SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
        return Question(
            id=row["id"],
            fingerprint=row["fingerprint"],
            raw_text=row["raw_text"],
            field_type=row["field_type"],
            options=json.loads(row["options_json"]) if row["options_json"] else None,
        )
    cur = conn.execute(
        """INSERT INTO questions (fingerprint, raw_text, field_type, options_json, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fingerprint, raw_text, field_type, options_json, now, now),
    )
    return Question(
        id=cur.lastrowid,
        fingerprint=fingerprint,
        raw_text=raw_text,
        field_type=field_type,
        options=options,
    )


def latest_answer(conn: sqlite3.Connection, question_id: int) -> Answer | None:
    row = conn.execute(
        """SELECT id, question_id, value, ai_generated, reviewed_at, created_at
           FROM answers WHERE question_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (question_id,),
    ).fetchone()
    if not row:
        return None
    return Answer(
        id=row["id"],
        question_id=row["question_id"],
        value=row["value"],
        ai_generated=bool(row["ai_generated"]),
        reviewed_at=row["reviewed_at"],
        created_at=row["created_at"],
    )


def insert_answer(
    conn: sqlite3.Connection,
    question_id: int,
    value: str,
    ai_generated: bool,
    source_url: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO answers (question_id, value, ai_generated, source_url, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (question_id, value, 1 if ai_generated else 0, source_url, _now()),
    )
    return cur.lastrowid


def all_qa_pairs(conn: sqlite3.Connection) -> list[tuple[Question, Answer]]:
    """Every question that has at least one answer, paired with its latest answer."""
    rows = conn.execute(
        """SELECT q.id AS qid, q.fingerprint, q.raw_text, q.field_type, q.options_json,
                  a.id AS aid, a.value, a.ai_generated, a.reviewed_at, a.created_at
             FROM questions q
             JOIN answers a ON a.id = (
                 SELECT id FROM answers
                  WHERE question_id = q.id
                  ORDER BY created_at DESC LIMIT 1
             )"""
    ).fetchall()
    out: list[tuple[Question, Answer]] = []
    for r in rows:
        out.append(
            (
                Question(
                    id=r["qid"],
                    fingerprint=r["fingerprint"],
                    raw_text=r["raw_text"],
                    field_type=r["field_type"],
                    options=json.loads(r["options_json"]) if r["options_json"] else None,
                ),
                Answer(
                    id=r["aid"],
                    question_id=r["qid"],
                    value=r["value"],
                    ai_generated=bool(r["ai_generated"]),
                    reviewed_at=r["reviewed_at"],
                    created_at=r["created_at"],
                ),
            )
        )
    return out


def unreviewed_answers(conn: sqlite3.Connection) -> list[tuple[Question, Answer]]:
    rows = conn.execute(
        """SELECT q.id AS qid, q.fingerprint, q.raw_text, q.field_type, q.options_json,
                  a.id AS aid, a.value, a.ai_generated, a.reviewed_at, a.created_at
             FROM answers a
             JOIN questions q ON q.id = a.question_id
            WHERE a.ai_generated = 1 AND a.reviewed_at IS NULL
              AND a.id = (SELECT id FROM answers WHERE question_id = q.id ORDER BY created_at DESC LIMIT 1)
            ORDER BY a.created_at DESC"""
    ).fetchall()
    return [
        (
            Question(
                id=r["qid"],
                fingerprint=r["fingerprint"],
                raw_text=r["raw_text"],
                field_type=r["field_type"],
                options=json.loads(r["options_json"]) if r["options_json"] else None,
            ),
            Answer(
                id=r["aid"],
                question_id=r["qid"],
                value=r["value"],
                ai_generated=bool(r["ai_generated"]),
                reviewed_at=r["reviewed_at"],
                created_at=r["created_at"],
            ),
        )
        for r in rows
    ]


def mark_reviewed(conn: sqlite3.Connection, answer_id: int) -> None:
    conn.execute("UPDATE answers SET reviewed_at = ? WHERE id = ?", (_now(), answer_id))


def update_answer_value(conn: sqlite3.Connection, question_id: int, value: str) -> int:
    """Insert a new answer row marked as user-edited (not AI, already reviewed)."""
    cur = conn.execute(
        """INSERT INTO answers (question_id, value, ai_generated, reviewed_at, created_at)
           VALUES (?, ?, 0, ?, ?)""",
        (question_id, value, _now(), _now()),
    )
    return cur.lastrowid


def delete_question(conn: sqlite3.Connection, question_id: int) -> bool:
    """Delete a cached question. Answers cascade via FK. Returns True if a row was deleted."""
    cur = conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    return cur.rowcount > 0


def record_application(
    conn: sqlite3.Connection,
    url: str,
    company: str | None,
    job_title: str | None,
    status: str,
    error: str | None = None,
    screenshots_dir: str | None = None,
    pre_submit_screenshot: str | None = None,
    post_submit_screenshot: str | None = None,
) -> int:
    """Insert a new applications row, or — if the prior most-recent row for this URL
    is a non-success (``filled`` / ``failed``) — upgrade that row in place and archive
    its previous state into ``prior_attempts``. This keeps one row per URL across
    retry cycles while preserving the failure history.

    Behaviour:
    - No prior row for this URL → INSERT new row.
    - Prior row exists with status ``submitted`` → INSERT new row (don't clobber the
      successful submission; downstream `find_successful_application` will still hit
      the older successful row, but the new row records the redundant retry).
    - Prior row exists with status ``filled`` or ``failed`` → UPDATE in place. The
      pre-update {status, error, screenshots, created_at} is appended to
      ``prior_attempts`` (JSON list) so the original failure detail isn't lost.
    """
    submitted_at = _now() if status == "submitted" else None
    now = _now()

    prior = conn.execute(
        """SELECT id, status, error, screenshots_dir, pre_submit_screenshot,
                  post_submit_screenshot, created_at, prior_attempts
             FROM applications
            WHERE url = ?
            ORDER BY id DESC LIMIT 1""",
        (url,),
    ).fetchone()

    if prior and prior["status"] in ("filled", "failed"):
        history: list[dict] = []
        existing = prior["prior_attempts"]
        if existing:
            try:
                parsed = json.loads(existing)
                if isinstance(parsed, list):
                    history = parsed
            except json.JSONDecodeError:
                history = []
        history.append({
            "status": prior["status"],
            "error": prior["error"],
            "screenshots_dir": prior["screenshots_dir"],
            "pre_submit_screenshot": prior["pre_submit_screenshot"],
            "post_submit_screenshot": prior["post_submit_screenshot"],
            "created_at": prior["created_at"],
            "archived_at": now,
        })

        conn.execute(
            """UPDATE applications
                  SET company = ?,
                      job_title = ?,
                      status = ?,
                      submitted_at = ?,
                      created_at = ?,
                      error = ?,
                      screenshots_dir = ?,
                      pre_submit_screenshot = ?,
                      post_submit_screenshot = ?,
                      prior_attempts = ?
                WHERE id = ?""",
            (
                company, job_title, status, submitted_at, now, error,
                screenshots_dir, pre_submit_screenshot, post_submit_screenshot,
                json.dumps(history),
                prior["id"],
            ),
        )
        return prior["id"]

    cur = conn.execute(
        """INSERT INTO applications (
                url, company, job_title, status, submitted_at, created_at, error,
                screenshots_dir, pre_submit_screenshot, post_submit_screenshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            url, company, job_title, status, submitted_at, now, error,
            screenshots_dir, pre_submit_screenshot, post_submit_screenshot,
        ),
    )
    return cur.lastrowid


def submitted_urls(conn: sqlite3.Connection) -> set[str]:
    """Every URL that has at least one ``status='submitted'`` row.

    Used by ``bulk`` to pre-filter candidate jobs so we don't waste a
    bulk-count slot on a URL the runner would skip anyway."""
    rows = conn.execute(
        "SELECT DISTINCT url FROM applications WHERE status = 'submitted'"
    ).fetchall()
    return {r["url"] for r in rows}


def find_successful_application(
    conn: sqlite3.Connection, url: str
) -> sqlite3.Row | None:
    """Most recent submitted application for `url`, or None.

    Used by the runner to short-circuit re-applies. Exact URL match; no
    normalisation — query strings/anchors matter."""
    return conn.execute(
        """SELECT id, url, company, job_title, submitted_at,
                  screenshots_dir, pre_submit_screenshot, post_submit_screenshot
             FROM applications
            WHERE url = ? AND status = 'submitted'
            ORDER BY id DESC LIMIT 1""",
        (url,),
    ).fetchone()
