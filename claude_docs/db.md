# db.py — deep-dive

255 lines. SQLite schema + accessor functions. **Read this when changing the schema, adding a query, or debugging answer-cache behavior.**

> **Self-update reminder:** edit this doc whenever you change the schema, add a new accessor, or change the fingerprint contract. The schema summary in [CLAUDE.md](../CLAUDE.md) is intentionally brief — update it only when you add/remove a *table*.

## Schema

DB lives at `config.DB_PATH` = `data/answers.db`. Three tables, all via `IF NOT EXISTS` so `init_db()` is idempotent.

```sql
CREATE TABLE questions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL UNIQUE,    -- normalize_question(label)
    raw_text        TEXT NOT NULL,           -- original label
    field_type      TEXT NOT NULL,           -- 'text', 'select', etc.
    options_json    TEXT,                    -- json.dumps(options) or NULL
    first_seen_at   TEXT NOT NULL,           -- ISO-8601 UTC
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE answers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id     INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    value           TEXT NOT NULL,
    ai_generated    INTEGER NOT NULL DEFAULT 0,    -- 0 = manual/preset/profile, 1 = Gemini
    reviewed_at     TEXT,                          -- ISO-8601 UTC, NULL until confirmed
    source_url      TEXT,                          -- the application URL
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_answers_question_created
    ON answers(question_id, created_at DESC);

CREATE TABLE applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    company         TEXT,
    job_title       TEXT,
    status          TEXT NOT NULL,           -- 'submitted', 'filled', 'failed'
    submitted_at    TEXT,                    -- only set when status = 'submitted'
    created_at      TEXT NOT NULL,
    error           TEXT
);
```

### Key contract: `fingerprint`

`questions.fingerprint = normalize_question(raw_text)` — lowercased, non-alphanumerics collapsed to single spaces, trimmed. UNIQUE constraint means **same normalized question = same row across all applications**, which is how answer reuse works. If you change `normalize_question` in [resolver.py](../apply_engine/resolver.py), every existing fingerprint becomes potentially mismatched. Don't change it lightly.

### Answer history is append-only

`update_answer_value` and `insert_answer` always insert a new row. `latest_answer` returns the most recent. There's no UPDATE on existing answer rows except `mark_reviewed` (which only sets `reviewed_at`).

## Dataclasses

```python
@dataclass
class Question:
    id: int
    fingerprint: str
    raw_text: str
    field_type: str
    options: list[str] | None      # parsed from options_json

@dataclass
class Answer:
    id: int
    question_id: int
    value: str
    ai_generated: bool             # parsed from 0/1
    reviewed_at: str | None
    created_at: str
```

`source_url` is on the row but **not** exposed on the `Answer` dataclass. If you need it, query directly.

## Helpers

```python
def _now() -> str
```
ISO-8601 UTC timestamp at second precision: `datetime.now(timezone.utc).isoformat(timespec="seconds")`. All timestamp columns use this.

```python
@contextmanager
def connect() -> Iterator[sqlite3.Connection]
```
Yields a connection with `row_factory = sqlite3.Row` and `PRAGMA foreign_keys = ON`. **Auto-commits** on clean exit, **does not rollback** on exception (the connection just closes — uncommitted writes are lost). Always use as `with db.connect() as conn:`.

## Public functions

### `init_db()`

Runs `executescript(SCHEMA)`. Idempotent. Called by every CLI command that touches the DB.

### `upsert_question(conn, fingerprint, raw_text, field_type, options) -> Question`

If a row with this fingerprint exists, updates `last_seen_at` and returns the existing `Question` (with the **stored** `raw_text`/`field_type`/`options` — incoming values are ignored on conflict). Otherwise inserts and returns the new row. Returns the dataclass either way.

### `latest_answer(conn, question_id) -> Answer | None`

Most recent `answers` row by `created_at DESC`, limit 1. Returns `None` if no answers exist for that question. Index `idx_answers_question_created` makes this fast.

### `insert_answer(conn, question_id, value, ai_generated, source_url) -> int`

Plain insert. Returns the new row id. `created_at` set to `_now()`. `reviewed_at` left NULL.

### `all_qa_pairs(conn) -> list[tuple[Question, Answer]]`

Every question that has at least one answer, paired with its latest answer (subquery `id = (SELECT id FROM answers ... LIMIT 1)`). Used by `cli.py` `list` and `resolver.get_prior_qa()`.

### `unreviewed_answers(conn) -> list[tuple[Question, Answer]]`

Same shape as `all_qa_pairs` but filtered to `ai_generated = 1 AND reviewed_at IS NULL` AND the row is the latest answer for that question. Ordered by `created_at DESC`. Used by `cli.review`.

### `mark_reviewed(conn, answer_id) -> None`

Sets `reviewed_at = _now()` on the given answer.

### `update_answer_value(conn, question_id, value) -> int`

**Inserts a new row** marked `ai_generated=0` and `reviewed_at=<now>`. This is "the user manually fixed this answer" — it supersedes any AI answer for this question on next read because `latest_answer` orders by `created_at DESC`.

### `record_application(conn, url, company, job_title, status, error=None) -> int`

Inserts into `applications`. `submitted_at` only set when `status == 'submitted'`. Status values used elsewhere: `submitted` | `filled` | `failed` (set by `runner.apply_to_url`).

## Common edits

- **Add a table:** put the `CREATE TABLE IF NOT EXISTS` in `SCHEMA`. Add accessors below the existing ones. Update the schema section above.
- **Add a column:** add it to `SCHEMA`. **`init_db()` won't migrate existing DBs** — there's no migration system. If users have existing `data/answers.db` files, the column will be missing. Either ALTER explicitly in `init_db()` or accept that old DBs need manual fix.
- **Add a query:** new function in this file. Stick with the dataclass return convention. Always take `conn` as the first parameter.
- **Change `status` values:** `runner.apply_to_url` sets the three current values. Update both the runner and this doc.

## Gotchas

- **No migration system.** Schema changes only apply to fresh DBs. If you add a column, document the manual ALTER in this section.
- **`connect()` doesn't rollback on exceptions.** A function that does multiple writes and raises will leave partial state. Most accessors do single writes so this rarely matters in practice.
- **`upsert_question` ignores incoming `field_type`/`options` on conflict.** If a question's options change between applications (rare — Greenhouse forms tend to be stable), the stored options stay outdated. Probably not worth fixing unless a real bug surfaces.
- **`raw_text` on the Question row is whatever was first inserted.** If two applications phrase the same fingerprint slightly differently, you'll keep the first phrasing.
- **Foreign keys:** `ON DELETE CASCADE` on `answers.question_id`. Deleting a question wipes its answers. There's no UI for either.
