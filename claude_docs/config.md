# config.py — deep-dive

16 lines. Path constants + env-var loading. **Read this only when adding a new env var, path, or LLM model knob.**

> **Self-update reminder:** edit this doc whenever you add a constant, env var, or path. Update [CLAUDE.md](../CLAUDE.md)'s env-var table only if you're adding a *user-visible* env var.

## Contents

```python
ROOT = Path(__file__).resolve().parent.parent          # repo root
load_dotenv(ROOT / ".env")                             # reads .env at repo root
DATA_DIR = ROOT / "data"                               # all artifacts land here
DB_PATH = DATA_DIR / "answers.db"                      # SQLite cache
PROFILE_PATH = ROOT / "profile.yaml"                   # user profile
PROFILE_EXAMPLE_PATH = ROOT / "profile.example.yaml"   # template

GEMINI_MODEL = os.environ.get("APPLY_ENGINE_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"

DATA_DIR.mkdir(exist_ok=True)                          # ensure data dir on import
```

## Notes

- `.env` is loaded at module import via `python-dotenv`. Anything `os.environ.get(...)` reads after this line will see those values. The `.env` file is gitignored.
- `DATA_DIR.mkdir(exist_ok=True)` runs **at import time** — every command implicitly creates `data/` if absent.
- `GEMINI_API_KEY_ENV` is the name of the env var, not the value. `ai.py` reads it via `os.environ.get(config.GEMINI_API_KEY_ENV)`.
- There's no `.env.example`. If you need one, document the required keys here when you add it.

## Common edits

- **New env var:** add a default + read with `os.environ.get(...)`. If user-facing, also document in [CLAUDE.md](../CLAUDE.md).
- **New artifact path:** add a `Path` constant; reference it from the producing module.
- **Different LLM model env:** change `GEMINI_MODEL` line. The model is read by [ai.py](../apply_engine/ai.py) — no rewiring needed.
