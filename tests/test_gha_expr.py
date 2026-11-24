"""The evaluator is test infrastructure for the workflow templates, so it is tested too:
if it were wrong in the permissive direction the template tests would prove nothing."""

from typing import Any

import pytest
from gha_expr import evaluate

CTX: dict[str, Any] = {
    "a": {"b": "Hello", "empty": "", "n": None, "list": ["X", "y"]},
    "items": [{"name": "one"}, {"name": "Two"}],
    "vars": {},
}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("'x' == 'X'", True),
        ("'x' != 'y'", True),
        ("a.b == 'hello'", True),
        ("a.missing == 'x'", False),
        ("vars.AGENT_AUTO == 'true'", False),
        ("a.missing == ''", True),
        ("a.n == ''", True),
        ("!a.n", True),
        ("!a.b", False),
        ("!!a.b", True),
        ("a.b && 'yes'", "yes"),
        ("a.empty && 'yes'", ""),
        ("a.empty || 'fallback'", "fallback"),
        ("a.b || 'fallback'", "Hello"),
        ("a.missing || a.empty || 'last'", "last"),
        ("true && false", False),
        ("null == null", True),
        ("contains(a.list, 'x')", True),
        ("contains(a.list, 'z')", False),
        ("contains(a.b, 'ELL')", True),
        ("contains(items.*.name, 'two')", True),
        ("!contains(items.*.name, 'three')", True),
        ("join(items.*.name, ',') == 'one,Two'", True),
        ("contains(join(items.*.name, ','), 'repro:')", False),
        ("contains(fromJSON('[\"OWNER\",\"MEMBER\"]'), 'owner')", True),
        ("contains(fromJSON('[\"OWNER\"]'), 'NONE')", False),
        ("(true || false) && !false", True),
        ("true || false && false", True),  # && binds tighter than ||
        ("false && false || true", True),
        ("'it''s' == 'IT''S'", True),
    ],
)
def test_expressions(expression: str, expected: Any) -> None:
    assert evaluate(expression, CTX) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "format('{0}', 1)",
        "fromjson('[\"a\"]')",
        "FromJSON('[\"a\"]')",
        "Contains(a.list, 'x')",
        "JOIN(a.list, ',')",
        "startsWith(a.b, 'H')",
        "a.b === 'x'",
        "a.b > 3",
        "a.b ? 1 : 2",
        "contains(a.list",
        "'unterminated",
        "a.b b",
    ],
)
def test_anything_unsupported_is_an_error_not_a_guess(expression: str) -> None:
    with pytest.raises(ValueError):
        evaluate(expression, CTX)
