"""The limit on how many assessments a repository gets per day, and the two comments the
stage can leave besides its assessment: one when the limit stopped a run, one when a run
failed. The limit counts assessment comments written by the workflow's own bot, since
those are what a run that reached the model leaves behind."""

import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import implement
import pytest
from implement import (
    CAPPED_MARKER,
    FAILED_MARKER,
    MARKER,
    parse_cap,
    recent_assessments,
)
from test_implement import FakeGitHub, _http_error, _run

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=24)


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mine(hours_ago: float = 1, **overrides: Any) -> dict[str, Any]:
    comment: dict[str, Any] = {
        "body": f"Automated assessment.\n\n{MARKER}",
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "created_at": _ts(hours_ago),
    }
    return {**comment, **overrides}


# --- the number ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 5), ("  ", 5), ("7", 7), (" 12 ", 12), ("0", 0), ("9999", 9999)],
)
def test_the_cap_defaults_to_five_and_zero_pauses_everything(
    raw: str, expected: int
) -> None:
    assert parse_cap(raw) == expected


@pytest.mark.parametrize(
    "raw", ["-1", "x", "10000", "1.5", "5 5", "five", "５", "5; id", "+5"]
)
def test_a_cap_that_is_not_a_small_number_is_refused_not_ignored(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_cap(raw)


# --- counting ---


def _count(comments: list[Any], limit: int = 99) -> int:
    api = FakeGitHub(comments=comments)
    with patch("triage.github_request", api):
        return recent_assessments("o/r", "t", SINCE, limit)


def test_only_assessments_in_the_window_are_counted() -> None:
    comments = [_mine(1), _mine(23.9), _mine(24.1), _mine(100)]
    assert _count(comments) == 2


def test_a_person_typing_the_marker_cannot_use_up_the_cap() -> None:
    spoof = _mine(user={"login": "mallory", "type": "User"})
    app = _mine(user={"login": "some-app[bot]", "type": "Bot"})
    assert _count([spoof, app, _mine(1)]) == 1


def test_comments_without_the_marker_or_in_odd_shapes_are_ignored() -> None:
    odd: list[Any] = [
        {
            "body": "hello",
            "user": {"login": "github-actions[bot]"},
            "created_at": _ts(1),
        },
        {"body": MARKER, "user": None, "created_at": _ts(1)},
        {"body": None, "user": {"login": "github-actions[bot]"}, "created_at": _ts(1)},
        {
            "body": MARKER,
            "user": {"login": "github-actions[bot]"},
            "created_at": "yesterday",
        },
        {"body": MARKER, "user": {"login": "github-actions[bot]"}},
        "junk",
        None,
        42,
    ]
    assert _count(odd) == 0


def test_the_other_two_markers_are_not_assessments() -> None:
    assert _count([_mine(body=CAPPED_MARKER), _mine(body=FAILED_MARKER)]) == 0


def test_counting_stops_once_the_limit_is_reached() -> None:
    api = FakeGitHub(comments=[_mine(1)] * 50)
    with patch("triage.github_request", api):
        assert recent_assessments("o/r", "t", SINCE, 3) == 3


def test_every_page_is_read_up_to_a_bound() -> None:
    pages = {1: [_mine(1)] * 100, 2: [_mine(1)] * 100, 3: [_mine(1)] * 7}
    seen: list[int] = []

    def api(method: str, repo: str, path: str, token: str, payload: Any = None) -> Any:
        page = int(path.rsplit("page=", 1)[1])
        seen.append(page)
        return pages.get(page, [])

    with patch("triage.github_request", api):
        assert recent_assessments("o/r", "t", SINCE, 10_000) == 207
    assert seen == [1, 2, 3]


def test_a_reply_that_is_not_a_list_counts_as_nothing_not_as_an_error() -> None:
    with patch("triage.github_request", return_value={"message": "x"}):
        assert recent_assessments("o/r", "t", SINCE, 5) == 0


def test_the_window_is_asked_for_in_the_request() -> None:
    api = FakeGitHub()
    with patch("triage.github_request", api):
        recent_assessments("o/r", "t", SINCE, 5)
    parsed = urllib.parse.urlparse(api.calls[0][1])
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/issues/comments"
    assert query == {
        "since": ["2026-11-23T12:00:00Z"],
        "per_page": ["100"],
        "page": ["1"],
    }


# --- the command ---


def _check(
    comments: list[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: str
) -> tuple[int, str, FakeGitHub]:
    api = FakeGitHub(comments=comments)
    with patch("implement._now", return_value=NOW):
        code, output = _run("check-cap", api, tmp_path, monkeypatch, DAILY_CAP=cap)
    return code, output.strip(), api


def test_below_the_cap_a_run_goes_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output, _ = _check([_mine(1)] * 4, tmp_path, monkeypatch, "5")
    assert (code, output) == (0, "capped=false")


def test_at_the_cap_it_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output, _ = _check([_mine(1)] * 5, tmp_path, monkeypatch, "5")
    assert (code, output) == (0, "capped=true")


def test_the_default_is_five(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _check([_mine(1)] * 4, tmp_path, monkeypatch, "")[1] == "capped=false"
    assert _check([_mine(1)] * 5, tmp_path, monkeypatch, "")[1] == "capped=true"


def test_zero_stops_everything_without_even_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output, api = _check([], tmp_path, monkeypatch, "0")
    assert (code, output) == (0, "capped=true") and api.calls == []


@pytest.mark.parametrize("cap", ["x", "-1", "99999", "1;2"])
def test_a_bad_cap_stops_the_run_with_an_error(
    cap: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, output, api = _check([], tmp_path, monkeypatch, cap)
    assert code == 1 and "capped" not in output and api.calls == []
    assert "::error::" in capsys.readouterr().out


def test_when_it_cannot_count_it_does_not_say_go_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(fail_on=("GET", "/issues/comments", _http_error(500)))
    with patch("implement._now", return_value=NOW):
        code, output = _run("check-cap", api, tmp_path, monkeypatch, DAILY_CAP="5")
    assert code == 1 and "capped=false" not in output


# --- the two comments ---


def test_a_capped_issue_gets_one_comment_saying_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    code, _ = _run(["notify", "capped"], api, tmp_path, monkeypatch, DAILY_CAP="3")
    assert code == 0
    (write,) = api.writes
    assert write[1] == "/issues/17/comments"
    body = write[2]["body"]
    assert "3" in body and "not assessed" in body and "ready-to-implement" in body
    assert body.count(CAPPED_MARKER) == 1 and MARKER not in body


def test_a_capped_issue_is_not_told_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(comments=[{"body": f"x\n\n{CAPPED_MARKER}"}])
    code, _ = _run(["notify", "capped"], api, tmp_path, monkeypatch, DAILY_CAP="3")
    assert code == 0 and api.writes == []


def test_a_failed_run_gets_one_comment_with_a_link_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "4242"}
    code, _ = _run(["notify", "failed"], api, tmp_path, monkeypatch, **env)
    assert code == 0
    body = api.writes[0][2]["body"]
    assert "https://github.com/o/r/actions/runs/4242" in body
    assert body.count(FAILED_MARKER) == 1 and "no pull request" in body.lower()
    # Re-applying the label alone does nothing once an issue has been assessed, so the
    # comment must say what actually has to be undone first.
    assert "repro:" in body and "assessment comment" in body
    assert "`agent/issue-17`" in body and "`ready-to-implement`" in body


def test_a_failed_run_is_not_reported_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(comments=[{"body": f"failed\n\n{FAILED_MARKER}"}])
    code, _ = _run(["notify", "failed"], api, tmp_path, monkeypatch, GITHUB_RUN_ID="1")
    assert code == 0 and api.writes == []


@pytest.mark.parametrize(
    ("server", "run_id"),
    [
        ("http://github.com", "1"),
        ("https://evil.example/x?y=", "1"),
        ("https://github.com", "1; id"),
        ("https://github.com", ""),
        ("javascript:alert(1)", "1"),
        ("https://github.com\n::set-output x::y", "1"),
    ],
)
def test_a_link_is_left_out_unless_it_is_well_formed(
    server: str, run_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    _run(
        ["notify", "failed"],
        api,
        tmp_path,
        monkeypatch,
        GITHUB_SERVER_URL=server,
        GITHUB_RUN_ID=run_id,
    )
    body = api.writes[0][2]["body"]
    assert "http" not in body and "evil" not in body and "javascript" not in body


def test_the_comments_say_nothing_a_model_or_an_issue_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_elements: Any
) -> None:
    for kind in ("capped", "failed"):
        api = FakeGitHub()
        _run(
            ["notify", kind],
            api,
            tmp_path,
            monkeypatch,
            DAILY_CAP="5",
            GITHUB_RUN_ID="9",
        )
        assert live_elements(api.writes[0][2]["body"]) in ([], ["link"])


@pytest.mark.parametrize(
    "argv",
    [["notify"], ["notify", "other"], ["notify", "failed", "x"], ["check-cap", "x"]],
)
def test_the_new_commands_refuse_odd_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        implement.main(argv)
    assert excinfo.value.code == 2
