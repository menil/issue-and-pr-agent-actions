"""Name a release inside the shared implementation workflow.

    python scripts/pin_implement_actions.py 1.8.0 [path]

A workflow file cannot name its own commit, so the actions it calls are named by the tag of
the release it ships in (`@v1.8.0`), and a release's tag is locked ("immutable releases"),
which makes it as fixed as a commit. This rewrites every `uses:` of an action of this
repository in the file to the given version, and nothing else. It is run in the pull request
that is about to be released, before the tag exists; the tests check the pins agree."""

import re
import sys
from pathlib import Path

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "implement.yml"
)
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
# `uses:` at the start of a step (optionally `- uses:`), not text that merely mentions it.
USES = re.compile(
    r"(?m)^(?P<head>\s*(?:-\s+)?uses:\s+menil/pr-code-review-action(?:/[\w./-]+)?@)\S+"
)


def pin(text: str, version: str) -> tuple[str, int]:
    """The text with every call of this repository's actions on `version`, and how many."""
    if not VERSION.fullmatch(version):
        raise ValueError("the version must be three plain numbers, like 1.8.0")
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group('head')}v{version}"

    result = USES.sub(replace, text)
    if not count:
        raise ValueError("no call of an action of this repository was found")
    return result, count


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print("usage: pin_implement_actions.py X.Y.Z [path]", file=sys.stderr)
        return 2
    path = Path(argv[1]) if len(argv) == 2 else DEFAULT_PATH
    try:
        text, count = pin(path.read_text(encoding="utf-8"), argv[0])
    except ValueError as reason:
        print(f"error: {reason}", file=sys.stderr)
        return 2
    path.write_text(text, encoding="utf-8")
    print(f"{count} calls in {path.name} now name v{argv[0]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
