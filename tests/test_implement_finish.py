"""What the `finish` command does to an issue once the pull request for it is open.

It takes the trigger label off, puts the status label on and leaves one comment with the link.
All three are fixed in code: the only thing it takes from outside is the pull request URL,
and that only in the exact form of a pull request of this repository."""

from pathlib import Path

import implement
import pytest
from implement import (
    OPENED_LABEL,
    PR_OPEN_COLOR,
    PR_OPEN_DESCRIPTION,
    TRIGGER_LABEL,
    opened_comment,
)
from test_implement import FakeGitHub, _http_error, _run

URL = "https://github.com/o/r/pull/99"


def _finish(
    api: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str = URL,
) -> int:
    code, _ = _run("finish", api, tmp_path, monkeypatch, PULL_REQUEST_URL=url)
    return code


def _writes(api: FakeGitHub) -> list[tuple[str, str]]:
    return [(method, path) for method, path, _ in api.writes]


def test_it_takes_the_trigger_label_off_and_puts_the_status_label_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    assert _finish(api, tmp_path, monkeypatch) == 0
    assert ("DELETE", "/issues/17/labels/ready-to-implement") in _writes(api)
    added = [p for m, path, p in api.writes if path == "/issues/17/labels"]
    assert added == [{"labels": ["pr-open"]}]


def test_the_two_labels_are_the_ones_the_docs_name() -> None:
    assert (TRIGGER_LABEL, OPENED_LABEL) == ("ready-to-implement", "pr-open")


def test_the_status_label_is_created_with_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    _finish(api, tmp_path, monkeypatch)
    created = [p for m, path, p in api.writes if path == "/labels"]
    assert created == [
        {
            "name": "pr-open",
            "color": PR_OPEN_COLOR,
            "description": PR_OPEN_DESCRIPTION,
        }
    ]


def test_a_status_label_that_already_exists_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(fail_on=("POST", "/labels", _http_error(422)))
    assert _finish(api, tmp_path, monkeypatch) == 0
    assert ("POST", "/issues/17/labels") in _writes(api)


def test_the_comment_is_last_so_its_marker_means_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    _finish(api, tmp_path, monkeypatch)
    assert _writes(api)[-1] == ("POST", "/issues/17/comments")
    assert _writes(api).index(
        ("DELETE", "/issues/17/labels/ready-to-implement")
    ) < _writes(api).index(("POST", "/issues/17/comments"))


def test_the_comment_links_the_pull_request_and_says_a_person_must_review_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    _finish(api, tmp_path, monkeypatch)
    body = next(p["body"] for m, path, p in api.writes if path.endswith("/comments"))
    assert "#99" in body
    assert "A model wrote it" in body
    assert "nothing has been merged or approved" in body.lower()
    assert body.rstrip().endswith("<!-- agent-implement:opened:99 -->")
    assert body == opened_comment(99)


def test_the_comment_is_left_once_for_each_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    again = FakeGitHub(comments=[{"body": opened_comment(99)}])
    assert _finish(again, tmp_path, monkeypatch) == 0
    assert not [w for w in _writes(again) if w[1].endswith("/comments")]
    # The labels are still put right, so a run that stopped halfway can finish.
    assert ("DELETE", "/issues/17/labels/ready-to-implement") in _writes(again)

    # A different pull request for the same issue gets its own comment.
    other = FakeGitHub(comments=[{"body": opened_comment(98)}])
    _finish(other, tmp_path, monkeypatch)
    assert ("POST", "/issues/17/comments") in _writes(other)


def test_an_issue_without_the_trigger_label_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(
        fail_on=(
            "DELETE",
            "/issues/17/labels/ready-to-implement",
            _http_error(404),
        )
    )
    assert _finish(api, tmp_path, monkeypatch) == 0
    assert ("POST", "/issues/17/labels") in _writes(api)
    assert ("POST", "/issues/17/comments") in _writes(api)


@pytest.mark.parametrize("code", [403, 500])
def test_any_other_failure_to_take_the_label_off_fails_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    api = FakeGitHub(
        fail_on=(
            "DELETE",
            "/issues/17/labels/ready-to-implement",
            _http_error(code),
        )
    )
    assert _finish(api, tmp_path, monkeypatch) != 0
    # No comment: its marker says the issue was tidied, and it was not.
    assert not [w for w in _writes(api) if w[1].endswith("/comments")]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pull/99/files",
        "https://github.com/o/r/pull/99 ",
        "https://github.com/o/r/pull/99\nextra",
        "https://github.com/o/r/pull/99#issuecomment-1",
        "https://github.com/o/r/pull/x",
        "https://github.com/o/r/issues/99",
        "https://github.com/o/other/pull/99",
        "https://github.com/other/r/pull/99",
        "https://evil.example/o/r/pull/99",
        "http://github.com/o/r/pull/99",
        "https://github.com/o/r/pull/99; touch x",
        "99",
    ],
)
def test_only_a_pull_request_of_this_repository_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    api = FakeGitHub()
    assert _finish(api, tmp_path, monkeypatch, url) != 0
    assert api.writes == []


def test_the_repository_name_is_compared_without_regard_to_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GitHub treats owner and repository names as case-insensitive.
    api = FakeGitHub()
    assert _finish(api, tmp_path, monkeypatch, "https://github.com/O/R/pull/99") == 0


def test_nothing_from_the_url_but_its_number_reaches_what_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    _finish(api, tmp_path, monkeypatch)
    for _, path, payload in api.writes:
        assert "github.com" not in path
        assert "github.com" not in str(payload).replace(
            "https://github.com/o/r/pull/99", ""
        )


@pytest.mark.parametrize("args", [["finish", "x"], ["finish", "--force"]])
def test_finish_takes_no_arguments(args: list[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        implement.main(args)
    assert stop.value.code == 2


def test_a_server_error_when_looking_for_the_comment_fails_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(fail_on=("GET", "/issues/17/comments", _http_error(500)))
    assert _finish(api, tmp_path, monkeypatch) != 0
