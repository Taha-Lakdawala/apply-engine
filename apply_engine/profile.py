from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import config


@dataclass
class Profile:
    data: dict[str, Any]
    resume_path: Path
    resume_text: str

    def as_context(self) -> str:
        """Stable text blob sent to Claude as cached context."""
        parts = ["# Candidate profile", yaml.safe_dump(self.data, sort_keys=False).strip()]
        parts += ["", "# Resume", self.resume_text.strip()]
        return "\n".join(parts)

    def preset_answer(self, question: str) -> str | None:
        presets = self.data.get("preset_answers") or {}
        if not presets:
            return None
        from .resolver import normalize_question

        target = normalize_question(question)
        for key, value in presets.items():
            if normalize_question(key) == target:
                return str(value)
        return None


def load_profile() -> Profile:
    if not config.PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"profile.yaml not found at {config.PROFILE_PATH}. "
            f"Run `apply init` to create one from the template."
        )
    with config.PROFILE_PATH.open() as f:
        data = yaml.safe_load(f) or {}

    resume_path_raw = data.get("resume_path") or "resume.pdf"
    resume_path = Path(resume_path_raw)
    if not resume_path.is_absolute():
        resume_path = config.ROOT / resume_path
    if not resume_path.exists():
        raise FileNotFoundError(
            f"Resume not found at {resume_path}. Set resume_path in profile.yaml."
        )

    resume_text = _extract_resume_text(resume_path)
    return Profile(data=data, resume_path=resume_path, resume_text=resume_text)


def _extract_resume_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        import docx

        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    if suffix in {".txt", ".md"}:
        return path.read_text()
    raise ValueError(f"Unsupported resume format: {suffix}")
