"""Dependabot keeps the actions this repository pins current, so it has to look in every
place they are pinned, and leave alone the names the release script owns."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")


def _actions_block() -> str:
    match = re.search(
        r'(?ms)^  - package-ecosystem: "github-actions"\n(.*?)(?=^  - package-ecosystem|\Z)',
        CONFIG,
    )
    assert match, "no github-actions entry"
    return match.group(1)


def test_every_directory_with_an_action_file_is_looked_at() -> None:
    listed = set(
        re.findall(r'(?m)^      - "(/[^"]*)"\s*$', _actions_block().split("ignore:")[0])
    )
    wanted = {"/"}
    for path in ROOT.rglob("action.yml"):
        relative = path.parent.relative_to(ROOT)
        if relative.parts and relative.parts[0] in {"tests", "node_modules", ".git"}:
            continue
        wanted.add("/" + str(relative) if str(relative) != "." else "/")
    assert listed == wanted, listed ^ wanted


def test_its_own_actions_are_not_proposed_for_a_bump() -> None:
    ignore = _actions_block().split("ignore:", 1)[1].split("schedule:")[0]
    assert 'dependency-name: "menil/issue-and-pr-agent-actions"' in ignore
    assert 'dependency-name: "menil/issue-and-pr-agent-actions/*"' in ignore
