"""The release step that names the new version inside the shared workflow.

A workflow file cannot name its own commit, so the actions it calls are named by the tag of the
release it ships in. This script is the one place that changes those names, and it must change
all of them, only them, and only to a real version."""

from pathlib import Path

import pin_implement_actions as pin
import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "implement.yml"

SAMPLE = """\
jobs:
  a:
    steps:
      - uses: actions/checkout@v7
      - uses: cachix/install-nix-action@75c75c31ebece974e48ebc36b4ad125bd97bd97b # v31.11.3
      - uses: menil/pr-code-review-action/implement/assess@v1.7.0
      - uses: menil/pr-code-review-action/implement/fix@v1.7.0
      - uses: menil/pr-code-review-action@v1.7.0
      - name: other
        run: echo menil/pr-code-review-action/implement/assess@v1.7.0
"""


def test_every_call_to_this_repositorys_actions_gets_the_new_version() -> None:
    text, count = pin.pin(SAMPLE, "1.8.0")
    assert count == 3
    assert text.count("@v1.8.0") == 3 and "@v1.7.0" in text  # the one in `run:` stays
    assert "uses: menil/pr-code-review-action/implement/assess@v1.8.0" in text
    assert "uses: menil/pr-code-review-action@v1.8.0" in text


def test_other_actions_are_left_alone() -> None:
    text, _ = pin.pin(SAMPLE, "1.8.0")
    assert "actions/checkout@v7" in text
    assert (
        "install-nix-action@75c75c31ebece974e48ebc36b4ad125bd97bd97b # v31.11.3" in text
    )


def test_text_that_only_mentions_an_action_is_left_alone() -> None:
    text, _ = pin.pin(SAMPLE, "1.8.0")
    assert "echo menil/pr-code-review-action/implement/assess@v1.7.0" in text


def test_pinning_twice_changes_nothing_more() -> None:
    once, _ = pin.pin(SAMPLE, "1.8.0")
    twice, count = pin.pin(once, "1.8.0")
    assert twice == once and count == 3


@pytest.mark.parametrize(
    "version",
    [
        "",
        "1.8",
        "v1.8.0",
        "1.8.0-rc1",
        "1.8.0.1",
        "main",
        "1.8.x",
        "1.8.0;",
        " 1.8.0",
        "01.8.0 ",
    ],
)
def test_only_a_plain_three_part_version_is_accepted(version: str) -> None:
    with pytest.raises(ValueError):
        pin.pin(SAMPLE, version)


def test_a_file_with_nothing_to_pin_is_an_error() -> None:
    with pytest.raises(ValueError):
        pin.pin("jobs: {}\n", "1.8.0")


def test_the_real_workflow_is_pinned_all_the_way_through() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    _, count = pin.pin(text, "9.9.9")
    # assess, report (two), fix, review, finish, notify
    assert count == 7


def test_the_command_line_rewrites_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "implement.yml"
    target.write_text(SAMPLE, encoding="utf-8")
    assert pin.main(["1.8.0", str(target)]) == 0
    assert "uses: menil/pr-code-review-action@v1.8.0" in target.read_text()
    assert "3" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["1.8.0", "a", "b"], ["nope"]])
def test_the_command_line_refuses_odd_arguments(argv: list[str]) -> None:
    assert pin.main(argv) == 2


def test_the_command_line_leaves_a_file_alone_when_the_version_is_bad(
    tmp_path: Path,
) -> None:
    target = tmp_path / "implement.yml"
    target.write_text(SAMPLE, encoding="utf-8")
    assert pin.main(["v1.8", str(target)]) == 2
    assert target.read_text() == SAMPLE
