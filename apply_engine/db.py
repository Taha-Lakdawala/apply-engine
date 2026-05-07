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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    company         TEXT,
    job_title       TEXT,
    status          TEXT NOT NULL,
    submitted_at    TEXT,
    created_at      TEXT NOT NULL,
    error           TEXT
);
"""


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


def record_application(
    conn: sqlite3.Connection,
    url: str,
    company: str | None,
    job_title: str | None,
    status: str,
    error: str | None = None,
) -> int:
    submitted_at = _now() if status == "submitted" else None
    cur = conn.execute(
        """INSERT INTO applications (url, company, job_title, status, submitted_at, created_at, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (url, company, job_title, status, submitted_at, _now(), error),
    )
    return cur.lastrowid
