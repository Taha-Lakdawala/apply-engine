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

Work authorization questions:
- The work_authorization fields in the profile describe the candidate's status for FOREIGN countries (where they lack citizenship or residency). They do NOT apply to the candidate's home country.
- If the job country matches the candidate's home country (location.country in the profile), always answer eligibility/right-to-work questions with "Yes" and sponsorship-required questions with "No", regardless of what work_authorization.authorized_to_work or requires_sponsorship say.
- Only apply work_authorization.authorized_to_work / requires_sponsorship when the job is in a country OTHER than the candidate's home country.

Salary questions:
- If the job is in India: state ₹30,00,000–₹40,00,000 per annum (express as a range, e.g. "₹30,00,000 to ₹40,00,000 per annum").
- If the job is in any other country: convert using PPP. India's PPP conversion factor is ~25 INR per international dollar. Divide the India midpoint (₹35,00,000) by 25 to get ~$140,000 international dollars, then multiply by the target country's PPP factor (examples: US ~1.0 USD, UK ~0.70 GBP, Eurozone ~0.75 EUR, Canada ~1.30 CAD, Australia ~1.50 AUD, Singapore ~1.30 SGD). Express the result as a range ±15% below the PPP equivalent in the local currency, since the aim is slightly below market.
- If the job location is unknown, infer the country only from explicit signals in the job URL or location string (e.g. city/country name, country code in subdomain). Do NOT infer from the company name — the company HQ country is irrelevant. Only fall back to the India INR range if there is genuinely no location signal at all.
"""


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
                out[str(k)] = str(v)
    return out


def _format_known_qa(known_qa: list[tuple[str, str]]) -> str:
    if not known_qa:
        return ""
    lines = []
    for q, a in known_qa[:40]:
        a_short = a if len(a) < 300 else a[:300] + "…"
        lines.append(f"Q: {q}\nA: {a_short}")
    return "\n\n".join(lines)
