# ─── Lint ───────────────────────────────────────────────────
lint:
    ruff check src/ tests/
    mypy --strict src/ tests/

# ─── Format ─────────────────────────────────────────────────
format:
    ruff format src/ tests/

# ─── Check Format ───────────────────────────────────────────
check-format:
    ruff format --check src/ tests/

# ─── Test ───────────────────────────────────────────────────
test:
    python -m pytest tests/

# ─── Validate (lint + check format + test) ──────────────────
validate: lint check-format test
