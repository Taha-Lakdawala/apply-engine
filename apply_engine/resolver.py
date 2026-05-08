from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import ai, db
from .profile import Profile


@dataclass
class ResolvedAnswer:
    value: str
    source: str  # "preset", "profile", "stored", "ai"
    question_id: int
    answer_id: int


_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]+")
_SALARY_RE = re.compile(r"\b(salary|compensation|pay|remuneration|ctc|package|stipend|wage)\b")


def normalize_question(text: str) -> str:
    text = text.lower().strip()
    text = _NORMALIZE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Map common form-field questions directly to profile.yaml paths so we don't burn
# AI calls on deterministic data. Tuples are (regex, dotted profile path).
PROFILE_MAPPINGS: list[tuple[str, str]] = [
    (r"^(first|given)\s*name$", "personal.first_name"),
    (r"^(last|family)\s*name$|^surname$", "personal.last_name"),
    (r"preferred.*(first\s*)?name|nickname", "personal.preferred_name"),
    (r"^full\s*name$|^name$", "personal.full_name"),  # synthesized below
    (r"^e[- ]?mail( address)?$", "personal.email"),
    (r"^(phone|telephone|mobile|cell)( number)?$", "personal.phone"),
    (r"pronoun", "personal.pronouns"),
    (r"^country$", "location.country"),
    (r"\bcity\b", "location.city"),
    (r"^(state|province|region)$", "location.state"),
    (r"(postal|zip)\s*code|postcode|\bpincode\b|\bpin\s*code\b", "location.postal_code"),
    (r"\bstreet\s*address\b|\baddress\s*(line\s*)?1\b", "location.address_line1"),
    (r"linkedin", "links.linkedin"),
    (r"github", "links.github"),
    (r"^(personal\s*)?website$|portfolio", "links.website"),
    (r"twitter|x\.com", "links.twitter"),
    (r"\bschool\b|\buniversity\b|\bcollege\b|\binstitution\b", "education.institution"),
    (r"\bdegree\b|\bfield of study\b", "education.degree_level"),
    (r"\bdiscipline\b|\bfield\s+of\s+study\b|\bacademic\s+major\b|\byour\s+major\b|^major\s*$|\bconcentration\b", "education.major"),
    (r"\bstart\s*(date\s*)?year\b|\beducation\s*start\s*year\b", "education.start_year"),
    (r"\bend\s*(date\s*)?year\b|\beducation\s*end\s*year\b|\bgraduation year\b|\bgrad year\b|\byear of graduation\b", "education.graduation_year"),
    (r"\bgpa\b|\bgrade point\b|\bcgpa\b", "education.gpa"),
    (r"\b(current\s+)?ctc\b|\bcost\s+to\s+company\b", "compensation.current_ctc"),
    (r"\bectc\b|\bexpected\s+(ctc|cost\s+to\s+company)\b", "compensation.expected_ctc"),
]


def _matches_option(value: str, options: list[str]) -> bool:
    """True if value case-insensitively matches any option exactly."""
    v = value.strip().lower()
    return any(o.strip().lower() == v for o in options)


def _profile_lookup(question: str, profile: Profile) -> str | None:
    norm = normalize_question(question)
    for pattern, path in PROFILE_MAPPINGS:
        if not re.search(pattern, norm):
            continue
        value = _resolve_path(profile.data, path)
        if value:
            return str(value).strip()
    # Synthesize "Full Name" from first + last
    if re.search(r"^full\s*name$|^name$", norm):
        first = _resolve_path(profile.data, "personal.first_name") or ""
        last = _resolve_path(profile.data, "personal.last_name") or ""
        full = f"{first} {last}".strip()
        return full or None
    return None


def _resolve_path(data: dict[str, Any], path: str) -> Any:
    parts = path.split(".")
    cur: Any = data
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
        if cur in (None, ""):
            return None
    return cur


def _is_salary_question(question: str) -> bool:
    return bool(_SALARY_RE.search(normalize_question(question)))


def try_known_resolve(
    field: ai.FieldSpec,
    profile: Profile,
    source_url: str,
    job_location: str | None = None,
) -> tuple[int, ResolvedAnswer | None]:
    """Upsert the question. Try preset/profile/stored. Returns (question_id, answer-or-None)."""
    fingerprint = normalize_question(field.question)
    with db.connect() as conn:
        question = db.upsert_question(
            conn,
            fingerprint=fingerprint,
            raw_text=field.question,
            field_type=field.field_type,
            options=field.options,
        )

        # 1. Preset answer in profile.yaml wins.
        preset = profile.preset_answer(field.question)
        if preset is not None:
            answer_id = db.insert_answer(conn, question.id, preset, ai_generated=False, source_url=source_url)
            return question.id, ResolvedAnswer(value=preset, source="preset", question_id=question.id, answer_id=answer_id)

        # 2. Direct profile lookup (name / email / links / location).
        profile_value = _profile_lookup(field.question, profile)
        if profile_value is not None:
            # For constrained select fields, only use the profile value if it
            # exactly matches one of the available options; otherwise let AI pick.
            if field.options and not _matches_option(profile_value, field.options):
                profile_value = None
        if profile_value is not None:
            answer_id = db.insert_answer(conn, question.id, profile_value, ai_generated=False, source_url=source_url)
            return question.id, ResolvedAnswer(value=profile_value, source="profile", question_id=question.id, answer_id=answer_id)

        # 3. Reuse stored answer — but salary questions always go to AI so the
        #    answer can be adapted to the job's country via PPP.
        if _is_salary_question(field.question):
            return question.id, None

        stored = db.latest_answer(conn, question.id)
        if stored is not None:
            return question.id, ResolvedAnswer(
                value=stored.value,
                source="stored",
                question_id=question.id,
                answer_id=stored.id,
            )

    return question.id, None


def get_prior_qa() -> list[tuple[str, str]]:
    with db.connect() as conn:
        return [(q.raw_text, a.value) for q, a in db.all_qa_pairs(conn)]


def store_ai_answer(question_id: int, value: str, source_url: str) -> ResolvedAnswer:
    with db.connect() as conn:
        answer_id = db.insert_answer(conn, question_id, value, ai_generated=True, source_url=source_url)
    return ResolvedAnswer(value=value, source="ai", question_id=question_id, answer_id=answer_id)
