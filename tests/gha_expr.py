"""A small evaluator for the part of GitHub Actions' expression language the workflow
templates use, so a workflow's `if:` can be tested by what it decides, not by its text.

It supports string literals, context paths (with the `.*.` filter), `!`, `==`, `!=`, `&&`,
`||`, parentheses, and the functions contains, join and fromJSON. Anything else raises, so
a template that starts using more cannot be silently mis-evaluated."""

import json
import re
from typing import Any

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<string>'(?:[^']|'')*')
      | (?P<op>&&|\|\||==|!=|!|\(|\)|,)
      | (?P<name>[A-Za-z_][A-Za-z0-9_\-]*(?:\.(?:\*|[A-Za-z_][A-Za-z0-9_\-]*))*)
    )""",
    re.VERBOSE,
)
# Exactly the spellings GitHub documents. Accepting more (say `fromjson`) could let a
# template pass here that GitHub rejects, which is the wrong direction for a test double.
_FUNCTIONS = {"contains", "join", "fromJSON"}
_LITERALS = {"true": True, "false": False, "null": None}


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError(f"cannot read the expression at: {text[pos : pos + 30]!r}")
        kind = match.lastgroup or ""
        out.append((kind, match.group(kind)))
        pos = match.end()
    return out


def truthy(value: Any) -> bool:
    return value not in (False, 0, "", None)


def _lookup(context: Any, parts: list[str]) -> Any:
    for index, part in enumerate(parts):
        if part == "*":
            if isinstance(context, dict):
                context = list(context.values())
            if not isinstance(context, list):
                return []
            return [_lookup(item, parts[index + 1 :]) for item in context]
        if not isinstance(context, dict) or part not in context:
            return None
        context = context[part]
    return context


def _fold(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def _equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        other = right if left is None else left
        return other in (None, "", 0, False) and not isinstance(other, list)
    return bool(_fold(left) == _fold(right))


def _contains(search: Any, item: Any) -> bool:
    if isinstance(search, list):
        return any(_equal(element, item) for element in search)
    if isinstance(search, str) and isinstance(item, str):
        return item.casefold() in search.casefold()
    return False


def _join(values: Any, separator: Any = ",") -> str:
    if isinstance(values, list):
        return str(separator).join(str(v) for v in values if v is not None)
    return str(values)


class _Parser:
    def __init__(self, text: str, context: dict[str, Any]) -> None:
        self.tokens = _tokens(text)
        self.pos = 0
        self.context = context

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, value: str | None = None) -> tuple[str, str]:
        token = self._peek()
        if token is None or (value is not None and token[1] != value):
            raise ValueError(f"expected {value!r}, found {token!r}")
        self.pos += 1
        return token

    def parse(self) -> Any:
        value = self._or()
        if self._peek() is not None:
            raise ValueError(f"unexpected {self._peek()!r}")
        return value

    def _or(self) -> Any:
        left = self._and()
        while self._peek() == ("op", "||"):
            self._take()
            right = self._and()
            left = left if truthy(left) else right
        return left

    def _and(self) -> Any:
        left = self._equality()
        while self._peek() == ("op", "&&"):
            self._take()
            right = self._equality()
            left = right if truthy(left) else left
        return left

    def _equality(self) -> Any:
        left = self._unary()
        while self._peek() in (("op", "=="), ("op", "!=")):
            op = self._take()[1]
            right = self._unary()
            same = _equal(left, right)
            left = same if op == "==" else not same
        return left

    def _unary(self) -> Any:
        if self._peek() == ("op", "!"):
            self._take()
            return not truthy(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        kind, value = self._take()
        if kind == "string":
            return value[1:-1].replace("''", "'")
        if kind == "op" and value == "(":
            inner = self._or()
            self._take(")")
            return inner
        if kind == "name":
            if self._peek() == ("op", "("):
                if value not in _FUNCTIONS:
                    raise ValueError(f"unsupported function {value}")
                self._take("(")
                args = [self._or()]
                while self._peek() == ("op", ","):
                    self._take()
                    args.append(self._or())
                self._take(")")
                if value == "contains":
                    return _contains(*args)
                if value == "join":
                    return _join(*args)
                return json.loads(args[0])
            if value in _LITERALS:
                return _LITERALS[value]
            return _lookup(self.context, value.split("."))
        raise ValueError(f"unexpected {value!r}")


def evaluate(expression: str, context: dict[str, Any]) -> Any:
    """The value of a GitHub Actions expression, without the `${{ }}`."""
    return _Parser(expression, context).parse()
