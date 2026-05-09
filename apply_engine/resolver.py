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

_INDIA_LOC_RE = re.compile(
    r"\b(india|bengaluru|bangalore|mumbai|delhi|hyderabad|chennai|pune|kolkata|noida|gurugram|gurgaon)\b",
    re.I,
)

# PPP base: 25 INR = 1 international dollar; India target = ₹30L–₹40L.
# "Slightly less than PPP" = 90% of the straight PPP conversion.
# Tuple: (location pattern, currency prefix for f"{prefix}{amount:,}", PPP factor, round_to)
_PPP_TABLE: list[tuple[re.Pattern, str, float, int]] = [
    (re.compile(r"\b(usa?|united states|u\.s\.?a?\.?)\b", re.I), "$",    1.00, 5_000),
    (re.compile(r"\b(uk|united kingdom|england|britain|london)\b", re.I), "£",    0.70, 2_000),
    (re.compile(r"\b(canada|toronto|vancouver|montreal)\b", re.I),        "CAD ", 1.30, 5_000),
    (re.compile(r"\b(australia|sydney|melbourne)\b", re.I),               "AUD ", 1.50, 5_000),
    (re.compile(r"\b(singapore)\b", re.I),                                "SGD ", 1.30, 5_000),
    (re.compile(r"\b(germany|france|netherlands|spain|italy|amsterdam|berlin|paris|europe|eurozone)\b", re.I), "€", 0.75, 5_000),
    (re.compile(r"\b(uae|dubai|abu dhabi|united arab emirates)\b", re.I), "AED ", 3.67, 5_000),
    (re.compile(r"\b(japan|tokyo|osaka)\b", re.I),                        "¥",   160.0, 500_000),
]

_INR_PPP = 25
_INDIA_LOWER = 3_000_000
_INDIA_UPPER = 4_000_000
_PPP_DISCOUNT = 0.90
_INTL_LOWER = int(_INDIA_LOWER / _INR_PPP)  # 120_000 international dollars
_INTL_UPPER = int(_INDIA_UPPER / _INR_PPP)  # 160_000 international dollars


def _compute_salary(job_location: str | None) -> str | None:
    """Return a deterministic salary range string for the given job location, or None for unknown countries."""
    loc = (job_location or "").strip()
    if not loc or _INDIA_LOC_RE.search(loc):
        return "₹30,00,000 to ₹40,00,000 per annum"
    for pattern, prefix, factor, step in _PPP_TABLE:
        if pattern.search(loc):
            lo = round((_INTL_LOWER * factor * _PPP_DISCOUNT) / step) * step
            hi = round((_INTL_UPPER * factor * _PPP_DISCOUNT) / step) * step
            return f"{prefix}{lo:,} to {prefix}{hi:,} per annum"
    return None


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
    (r"\bpronoun(s)?\b", "personal.pronouns"),
    (r"pronounce.*(name|it)|how.*pronounce", "personal.pronunciation"),
    (r"^country$", "location.country"),
    (r"\bcity\b", "location.city"),
    (r"^(state|province|region)$", "location.state"),
    (r"(postal|zip)\s*code|postcode|\bpincode\b|\bpin\s*code\b", "location.postal_code"),
    (r"\bstreet\s*address\b|\baddress\s*(line\s*)?1\b", "location.address_line1"),
    (r"linkedin", "links.linkedin"),
    (r"github", "links.github"),
    (r"^(personal\s*)?website$|portfolio", "links.website"),
    (r"twitter|x\.com", "links.twitter"),
    (r"\bgpa\b|\bgrade point\b|\bcgpa\b", "education.gpa"),
    (r"\bschool\b|\buniversity\b|\bcollege\b|\binstitution\b", "education.institution"),
    (r"\bdegree\b|\bfield of study\b", "education.degree_level"),
    (r"\bdiscipline\b|\bfield\s+of\s+study\b|\bacademic\s+major\b|\byour\s+major\b|^major\s*$|\bconcentration\b", "education.major"),
    (r"\bstart\s*(date\s*)?year\b|\beducation\s*start\s*year\b", "education.start_year"),
    (r"\bend\s*(date\s*)?year\b|\beducation\s*end\s*year\b|\bgraduation year\b|\bgrad year\b|\byear of graduation\b", "education.graduation_year"),
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

        # 3. Salary: compute deterministically from job location (PPP-based).
        # Select/searchable_select salary fields need AI to match a dropdown option —
        # skip both deterministic computation AND stored answers so AI always picks fresh.
        if _is_salary_question(field.question):
            is_select_type = field.field_type in ("select", "searchable_select") or bool(field.options)
            if not is_select_type:
                salary = _compute_salary(job_location)
                if salary is not None:
                    answer_id = db.insert_answer(conn, question.id, salary, ai_generated=False, source_url=source_url)
                    return question.id, ResolvedAnswer(value=salary, source="preset", question_id=question.id, answer_id=answer_id)
                # Unknown country, free-text field — fall through to stored/AI below
            else:
                return question.id, None  # AI will pick from dropdown options

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
