# ─── Lint ───────────────────────────────────────────────────
lint:
    ruff check src/ scripts/ tests/
    mypy --strict src/ scripts/ tests/

# ─── Format ─────────────────────────────────────────────────
format:
    ruff format src/ scripts/ tests/

# ─── Check Format ───────────────────────────────────────────
check-format:
    ruff format --check src/ scripts/ tests/

# ─── Test ───────────────────────────────────────────────────
test:
    python -m pytest tests/

# ─── Validate (lint + check format + test) ──────────────────
validate: lint check-format test
