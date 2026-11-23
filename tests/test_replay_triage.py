import json
import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import replay_triage
from replay_triage import Row, human_type, render_report, replay_repo
from triage import Decision, Plan

REPO = "owner/repo"
LABELS = [{"name": n} for n in ("bug", "enhancement", "question", "documentation")]


def _issue(
    number: int, title: str = "t", labels: tuple[str, ...] = (), **extra: Any
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": f"body {number}",
        "state": "open",
        "created_at": "2026-01-01T00:00:00Z",
        "user": {"login": "someone", "type": "User"},
        "labels": [{"name": name} for name in labels],
        **extra,
    }


class FakeAPI:
    """Stands in for github_request: pages of issues, and the repository's labels."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.paths: list[str] = []

    def __call__(
        self, method: str, repo: str, path: str, token: str, payload: Any = None
    ) -> Any:
        self.paths.append(path)
        if path.startswith("/labels"):
            return LABELS
        page = int(re.search(r"[?&]page=(\d+)", path).group(1))  # type: ignore[union-attr]
        return self.items[(page - 1) * 100 : page * 100]


def _decision(**fields: Any) -> Decision:
    base: dict[str, Any] = {
        "type": "bug",
        "needs_info": False,
        "missing": "",
        "duplicate_of": None,
        "reason": "",
    }
    return Decision(**{**base, **fields})


def _replay(
    items: list[dict[str, Any]],
    answer: tuple[Decision | None, Exception | None] = (_decision(), None),
    dry_run: bool = False,
    limit: int = 50,
) -> tuple[list[Row], MagicMock]:
    ask = MagicMock(return_value=answer)
    with (
        patch("replay_triage.github_request", FakeAPI(items)),
        patch("replay_triage.ask_model", ask),
    ):
        rows = replay_repo(REPO, "token", "auto", limit, dry_run)
    return rows, ask


# --- reading the issues ---


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (("bug",), "bug"),
        (("help wanted", "enhancement"), "enhancement"),
        (("duplicate", "needs-info"), None),
        ((), None),
    ],
)
def test_human_type(labels: tuple[str, ...], expected: str | None) -> None:
    assert human_type(labels) == expected
    assert human_type(frozenset(labels)) == expected


def test_pull_requests_are_not_issues() -> None:
    items = [_issue(1), _issue(2, pull_request={"url": "x"}), _issue(3)]
    rows, _ = _replay(items)
    assert [row.number for row in rows] == [1, 3]


def test_issues_after_many_pull_requests_are_still_found() -> None:
    """The issues API mixes both, so the first 100 items can hold no issue at all."""
    pulls = [_issue(n, pull_request={"url": "x"}) for n in range(1, 101)]
    api = FakeAPI([*pulls, _issue(101, labels=("bug",))])
    with (
        patch("replay_triage.github_request", api),
        patch("replay_triage.ask_model", return_value=(_decision(), None)),
    ):
        rows = replay_repo(REPO, "token", "auto", 50, False)
    assert [row.number for row in rows] == [101]
    assert len([p for p in api.paths if re.search(r"[?&]page=", p)]) == 2


def test_the_limit_applies_per_repository() -> None:
    rows, _ = _replay([_issue(n) for n in range(1, 11)], limit=3)
    assert [row.number for row in rows] == [1, 2, 3]


def test_the_model_sees_only_issues_that_came_before() -> None:
    _, ask = _replay([_issue(1, "first"), _issue(2, "second"), _issue(3, "third")])
    seen = [
        (call.args[2].number, [n for n, _ in call.args[3]])
        for call in ask.call_args_list
    ]
    assert seen == [(1, []), (2, [1]), (3, [2, 1])]


def test_a_persons_labels_are_recorded_for_the_comparison() -> None:
    rows, _ = _replay([_issue(1, "t", labels=("bug", "help wanted"))])
    assert (rows[0].human_type, rows[0].human_labels) == ("bug", ("bug", "help wanted"))


def test_a_dry_run_never_asks_the_model() -> None:
    rows, ask = _replay([_issue(1)], dry_run=True)
    ask.assert_not_called()
    assert rows[0].decision is None and "dry run" in rows[0].error


def test_no_usable_answer_is_recorded() -> None:
    rows, _ = _replay([_issue(1)], answer=(None, ValueError("boom")))
    assert rows[0].decision is None and rows[0].plan is None
    assert "no usable answer (ValueError)" in rows[0].error


def test_the_plan_follows_the_repositorys_labels() -> None:
    rows, _ = _replay([_issue(1)], answer=(_decision(type="bug"), None))
    assert rows[0].plan == Plan(None, ("bug", "triaged"))


# --- the report ---


def _row(number: int, **overrides: Any) -> Row:
    fields: dict[str, Any] = {
        "repo": REPO,
        "number": number,
        "title": "A title",
        "human_type": "bug",
        "human_labels": ("bug",),
        "decision": _decision(),
        "plan": Plan(None, ("bug", "triaged")),
        "error": "",
    }
    return Row(**{**fields, **overrides})


def test_the_report_counts_matches_and_questions() -> None:
    rows = [
        _row(1),
        _row(2, decision=_decision(type="enhancement"), plan=Plan(None, ("triaged",))),
        _row(
            3,
            decision=_decision(needs_info=True, missing="Which version?"),
            plan=Plan("Thanks.\n\nWhich version?", ("bug", "needs-info")),
        ),
        _row(4, human_type=None, human_labels=()),
        _row(5, decision=None, plan=None, error="no usable answer (ValueError)"),
    ]
    report = render_report(rows)
    assert "5 issues replayed, 4 answered by the model." in report
    assert "matches a person's label on 2 of 3 issues that had one" in report
    assert "ask for more information on 1." in report
    assert "(match)" in report and "(differs)" in report
    assert "no usable answer (ValueError)" in report
    assert "### Comments it would post" in report and "Which version?" in report


def test_a_hostile_title_cannot_put_markup_in_the_report() -> None:
    title = "[click](https://evil.example/x) @octocat <img src=//evil.example/i.png>"
    report = render_report([_row(1, title=title)])
    assert "[click](" not in report and "<img" not in report
    assert "`@octocat`" in report


def test_the_model_cannot_put_markup_in_the_report_either() -> None:
    decision = _decision(reason="see [x](https://evil.example/x) and @octocat")
    report = render_report([_row(1, decision=decision)])
    assert "[x](" not in report and "`@octocat`" in report


def test_a_pipe_in_a_title_does_not_break_the_table() -> None:
    report = render_report([_row(1, title="a | b | c")])
    row = next(
        line
        for line in report.splitlines()
        if "A title" not in line and "a" in line and line.startswith("| `")
    )
    assert len(re.findall(r"(?<!\\)\|", row)) == 8


def test_a_long_title_is_cut_with_an_ellipsis_not_a_marker() -> None:
    report = render_report([_row(1, title="word " * 100)])
    assert "…" in report and "_(truncated)_" not in report


def test_a_report_with_no_rows_is_still_a_report() -> None:
    assert render_report([]).startswith("## Triage replay")


# --- the command ---


def test_a_repository_argument_must_look_like_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        replay_triage.main(["--limit", "3", "not a repo"])
    assert excinfo.value.code == 2
    assert "owner/repo" in capsys.readouterr().err


def test_a_flag_cannot_be_smuggled_in_as_a_repository() -> None:
    with pytest.raises(SystemExit) as excinfo:
        replay_triage.main(["--json=/tmp/x/y", "owner/repo", "-x"])
    assert excinfo.value.code == 2


def test_a_token_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.dict(os.environ, {}, clear=True), pytest.raises(SystemExit) as excinfo:
        replay_triage.main([REPO, "--dry-run"])
    assert excinfo.value.code == 1
    assert "GH_TOKEN" in capsys.readouterr().err


def test_a_real_run_needs_provider_credentials() -> None:
    with (
        patch.dict(os.environ, {"GH_TOKEN": "t"}, clear=True),
        pytest.raises(SystemExit) as excinfo,
    ):
        replay_triage.main([REPO])
    assert excinfo.value.code == 1


def test_a_dry__run_needs_no_provider_credentials(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "rows.json"
    with (
        patch.dict(os.environ, {"GH_TOKEN": "t"}, clear=True),
        patch("replay_triage.github_request", FakeAPI([_issue(1, labels=("bug",))])),
    ):
        replay_triage.main([REPO, "--dry-run", "--json", str(out)])
    assert "## Triage replay" in capsys.readouterr().out
    saved = json.loads(out.read_text())
    assert saved[0]["number"] == 1 and saved[0]["human_type"] == "bug"


def test_only_the_report_goes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    def chatty(*args: Any) -> tuple[Decision | None, Exception | None]:
        print("Requesting triage from provider 'openrouter'...")
        return _decision(), None

    with (
        patch.dict(
            os.environ, {"GH_TOKEN": "t", "OPENROUTER_API_KEY": "k"}, clear=True
        ),
        patch("replay_triage.github_request", FakeAPI([_issue(1)])),
        patch("replay_triage.ask_model", chatty),
    ):
        replay_triage.main([REPO])
    captured = capsys.readouterr()
    assert captured.out.startswith("## Triage replay")
    assert "Requesting triage" not in captured.out
    assert "Requesting triage" in captured.err


def test_after_two_dashes_everything_is_a_repository_name() -> None:
    """The workflow passes its input unquoted, after `--`, so it cannot add an option."""
    with pytest.raises(SystemExit) as excinfo:
        replay_triage.main(["--dry-run", "--", "--json=/tmp/x"])
    assert excinfo.value.code == 2
