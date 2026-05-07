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
- For demographic / EEO questions, prefer the candidate's stated preference (often "Decline to self-identify") if no explicit answer is in the profile.
- For "How did you hear about this role?" or similar, use "LinkedIn" if no preference is set.
- Keep prose answers concise unless the question clearly invites depth (e.g. "tell us about a project").
- Never invent employment, education, or credentials not present in the resume or profile.
"""


def answer_fields_batch(
    fields_with_ids: list[tuple[str, FieldSpec]],
    profile_context: str,
    known_qa: list[tuple[str, str]],
) -> dict[str, str]:
    """Ask Gemini to answer many fields in one call. Returns {id: value_string}.
    Multiselect/checkbox values are returned as JSON-encoded lists."""
    if not fields_with_ids:
        return {}

    client = _get_client()
    qa_block = _format_known_qa(known_qa)
    field_blocks = [_format_field_block(fid, spec) for fid, spec in fields_with_ids]

    user_text = f"""{profile_context}

---

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
