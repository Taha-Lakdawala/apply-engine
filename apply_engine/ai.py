from __future__ import annotations

import json
import os
from dataclasses import dataclass

from google import genai
from google.genai import types

from . import config


@dataclass
class FieldSpec:
    question: str
    field_type: str          # text, textarea, select, multiselect, radio, checkbox, file, email, phone, url, number, searchable_select
    options: list[str] | None
    required: bool
    max_length: int | None


_client: genai.Client | None = None

_AI_CHAR_MAP = str.maketrans({
    "–": "-",    # en dash
    "—": "-",    # em dash
    "‘": "'",    # left single quote
    "’": "'",    # right single quote
    "“": '"',    # left double quote
    "”": '"',    # right double quote
    "…": "...",  # ellipsis
    " ": " ",    # non-breaking space
    "•": "-",    # bullet
})


def _sanitize(text: str) -> str:
    return text.translate(_AI_CHAR_MAP)


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get(config.GEMINI_API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"{config.GEMINI_API_KEY_ENV} is not set. Export your Gemini API key."
            )
        _client = genai.Client(api_key=api_key)
    return _client


SYSTEM_PROMPT = """You fill out job application form fields on behalf of a candidate.

Rules:
- Output JSON only, matching the schema described in the user message.
- Answer in the candidate's voice using the profile and resume provided.
- Match the field constraints exactly: stay under max_length, pick from `options` verbatim when present, return a list for multiselect.
- For demographic / EEO questions: for Hispanic/Latino questions answer "No" (not "Decline to self-identify") unless the profile says otherwise. For race/ethnicity category questions, choose the most accurate option from the profile or "Decline to self-identify" if truly unknown.
- For "How did you hear about this role?" or similar, use "LinkedIn" if no preference is set.
- Keep prose answers concise unless the question clearly invites depth (e.g. "tell us about a project").
- Never invent employment, education, or credentials not present in the resume or profile.
- For personal website / portfolio URL fields: only fill when `required: True`. If the field is optional, return "" even if the profile contains a website value.
- Conditional free-text questions (anything phrased as "if applicable", "if yes / if so", "if any", "if you have / are / were", "If not, please type 'N/A'", or otherwise contingent on a precondition that does not apply to the candidate): return "N/A" rather than "". These forms typically reject empty values even when the underlying input is not flagged as required, and "N/A" is the conventional answer when the conditional doesn't apply. Apply this whenever the question's structure makes it clear the answer is contingent — do NOT use "N/A" for unconditional questions.

Work authorization questions:
- The work_authorization fields in the profile describe the candidate's status for FOREIGN countries (where they lack citizenship or residency). They do NOT apply to the candidate's home country.
- If the job country matches the candidate's home country (location.country in the profile), always answer eligibility/right-to-work questions with "Yes" and sponsorship-required questions with "No", regardless of what work_authorization.authorized_to_work or requires_sponsorship say.
- Only apply work_authorization.authorized_to_work / requires_sponsorship when the job is in a country OTHER than the candidate's home country.

Start date / notice period questions:
- The candidate's notice period is exactly 60 days. Recognize equivalents: 60 days = 2 months = 8 weeks. Always prefer a literal/exact-match option when one is present (e.g., "60 days", "2 months", "8 weeks", "60 Days").
- Open-ended options like "60+ Days", "60 days and above", "More than 60 days" OVERSTATE the notice period — they signal the candidate could take longer than 60 days. Avoid these whenever a literal equivalent is available.
- If no literal-equivalent option exists and the only options that don't understate are open-ended (e.g., options are ["Immediate", "30 days", "45 days", "60 days and above"] for a 60-day notice), pick the next-lower bounded option ("45 days") rather than the open-ended one. Slight understatement is preferable to signaling indefinite/longer-than-actual notice.
- If a literal "60 days" / "2 months" / "8 weeks" option DOES exist, pick that — it is an exact match, not an understatement.

Salary questions (only reached when the field has predefined options, or for countries not handled deterministically):
- The candidate's India target is ₹30,00,000–₹40,00,000 per annum (30–40 LPA).
- When the field is a select/combobox with predefined options: pick the option whose range best covers ₹30–40 LPA (or the equivalent in local currency after PPP conversion). Prefer an option that contains the target rather than one that is below it.
- When the field is free-text: convert the India target using PPP — divide by 25 INR/intl-dollar to get $120,000–$160,000 international, then multiply by the target country's PPP factor and apply a 10% discount. Examples: US ~1.0 USD, UK ~0.70 GBP, Eurozone ~0.75 EUR, Canada ~1.30 CAD, Australia ~1.50 AUD, Singapore ~1.30 SGD. Express as a rounded range in local currency.
- Infer the country only from explicit signals in the job URL or location string. Do NOT infer from company name. If truly no signal, use ₹30,00,000 to ₹40,00,000 per annum.

Employment-section fields (Company name, Job title, Start year, End year, Start month, End month, "Current role?" toggles, etc.):
- Read the `employment:` list from the profile. The first entry is the most recent employer; use it for the most-recent / current-employer slot. Subsequent entries are older, in reverse-chronological order.
- Use the `form_order` context to tell EDUCATION date fields apart from EMPLOYMENT date fields. Education fields appear next to School/Degree/Discipline labels and use `education.start_year`/`graduation_year`. Employment date fields appear next to Company/Title/Employer labels and use the matching entry from `employment[]`.
- "End year" / "End date year" / "End date month" for the current employer: if `employment[0].current` is true:
  - For SELECT/COMBOBOX fields: prefer an option like "Present", "Current", or the current year ("2026") if available; otherwise leave blank.
  - For required TEXT/NUMBER fields (no options): fill with the current year ("2026") for end-year, or the current month for end-month. Do NOT leave blank — the form will reject submission. The "currently work here" toggle elsewhere on the form is the canonical signal; the End-year text field still needs a value when no toggle hides it.
  - For OPTIONAL TEXT/NUMBER fields (no options, required=False): leave blank.
- "Are you currently employed here?" / "Current role?" / similar yes-no checkboxes near the most-recent employer block: answer "Yes" when `employment[0].current` is true.
- Never invent employers or dates not present in the `employment:` list or resume.

Years-of-experience questions (general or skill-specific — backend, distributed systems, Python, cloud, etc.):
- Treat the candidate's experience as exactly **4 years** for any years-of-experience question, regardless of the specific skill or domain asked about. The bio mentions "4+ years" but for option matching always anchor on 4.
- When the field is a select/combobox with predefined ranges: pick the bucket that CONTAINS 4. Examples: options ["0-2", "3-5", "5-10", "10+"] → pick "3-5"; options ["<2 years", "2-4 years", "4-6 years", "6+ years"] → pick "4-6 years"; options ["1-3", "3-5", "5+"] → pick "3-5".
- Never return a range/label that isn't in `options` verbatim (e.g. don't return "4+ years" if it isn't an option). If options are bucketed, you MUST pick one of them.
- When the field is free-text or numeric: return "4".
"""


def generate_cover_letter(
    profile_context: str,
    job_title: str | None,
    company: str | None,
    job_description: str,
) -> str:
    """Generate a plain-text cover letter for the candidate."""
    client = _get_client()
    prompt = f"""{profile_context}

---

Job title: {job_title or "Unknown Role"}
Company: {company or "the company"}
Job description:
{job_description[:3000]}

---

Write a professional cover letter for this job application.
Requirements:
- 3-4 short paragraphs, under 350 words total
- Opening: express genuine interest in the specific role and company
- Middle: highlight 2-3 relevant experiences or skills from the resume that match the job
- Closing: invite them to review the resume and express availability for an interview
- Address to "Hiring Manager"
- Sign with the candidate's full name
- Plain text only, no markdown, no bullet points
"""
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are a professional career coach writing a concise, genuine cover letter "
                "for a job applicant. Write in first person, plain text, no markdown."
            ),
            temperature=0.4,
        ),
    )
    return _sanitize((response.text or "").strip())


def answer_fields_batch(
    fields_with_ids: list[tuple[str, FieldSpec]],
    profile_context: str,
    known_qa: list[tuple[str, str]],
    job_location: str | None = None,
    job_url: str | None = None,
    form_order: list[tuple[str, str | None]] | None = None,
) -> dict[str, str]:
    """Ask Gemini to answer many fields in one call. Returns {id: value_string}.
    Multiselect/checkbox values are returned as JSON-encoded lists.

    form_order: all form fields in page order as (label, resolved_value_or_None).
    Passing this lets the AI determine section context for ambiguous fields like
    "Start date year" (education vs. work experience) from surrounding fields.
    """
    if not fields_with_ids:
        return {}

    client = _get_client()
    qa_block = _format_known_qa(known_qa)
    needs_answer = {fid for fid, _ in fields_with_ids}
    field_blocks = [_format_field_block(fid, spec) for fid, spec in fields_with_ids]
    location_parts = [f"Job location: {job_location}" if job_location else "Job location: unknown (infer from URL if possible)"]
    if job_url:
        location_parts.append(f"Job URL: {job_url}")
    location_line = "\n".join(location_parts)

    form_order_block = ""
    if form_order:
        lines = []
        for label, value in form_order:
            if value is not None:
                lines.append(f"  {label}: {value}")
            else:
                lines.append(f"  {label}: [needs answer — see Fields below]")
        form_order_block = (
            "\nForm fields in page order (use this to determine which section "
            "each field belongs to):\n" + "\n".join(lines) + "\n"
        )

    user_text = f"""{profile_context}

---

{location_line}
{form_order_block}
You will answer {len(fields_with_ids)} job application form field(s).

Output a single JSON object whose keys are the field IDs given below and whose
values are the answers. Each answer must satisfy the field's constraints:
- For select/radio fields: the value must be exactly one of the listed options.
- For multiselect/checkbox fields: the value must be a JSON array of options.
- For free-text fields: the value is a string within max_length (if any).
- If a field is optional and you have nothing meaningful to say, return "".

Fields:
{chr(10).join(field_blocks)}

Previously answered questions for this candidate (for tone/voice reference):
{qa_block or "(none yet)"}
"""

    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=user_text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )
    raw = (response.text or "").strip()
    return _parse_batch_response(raw)


def _format_field_block(fid: str, spec: FieldSpec) -> str:
    return (
        f"- id={fid}\n"
        f"  question: {spec.question}\n"
        f"  type: {spec.field_type}\n"
        f"  required: {spec.required}\n"
        f"  max_length: {spec.max_length or 'no limit'}\n"
        f"  options: {json.dumps(spec.options) if spec.options else 'none'}"
    )


def _parse_batch_response(raw: str) -> dict[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                out[str(k)] = json.dumps(v)
            elif v is None:
                out[str(k)] = ""
            else:
                out[str(k)] = _sanitize(str(v))
    return out


def _format_known_qa(known_qa: list[tuple[str, str]]) -> str:
    if not known_qa:
        return ""
    lines = []
    for q, a in known_qa[:40]:
        a_short = a if len(a) < 300 else a[:300] + "…"
        lines.append(f"Q: {q}\nA: {a_short}")
    return "\n\n".join(lines)
