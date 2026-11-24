import json
import os
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import implement
import pytest
from implement import (
    ALLOWED_LABELS,
    MARKER,
    NotTrusted,
    Verdict,
    apply_report,
    check_issue,
    gate,
    has_marker,
    issue_markdown,
    make_report,
    parse_verdict,
    write_issue_file,
)

# --- the issue and who may start a run ---


def _raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 17,
        "title": "slugify leaves a hyphen",
        "body": "It prints -hello-world-.",
        "author_association": "OWNER",
        "user": {"login": "meni", "type": "User"},
    }
    return {**base, **overrides}


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_a_trusted_author_may_start_a_run(association: str) -> None:
    issue = check_issue(_raw(author_association=association))
    assert (issue.number, issue.title) == (17, "slugify leaves a hyphen")


@pytest.mark.parametrize(
    "association",
    ["CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", "NONE", ""],
)
def test_anyone_else_may_not(association: str) -> None:
    with pytest.raises(NotTrusted):
        check_issue(_raw(author_association=association))


def test_a_missing_association_is_not_trusted() -> None:
    raw = _raw()
    del raw["author_association"]
    with pytest.raises(NotTrusted):
        check_issue(raw)


def test_a_pull_request_is_not_an_issue() -> None:
    with pytest.raises(NotTrusted, match="pull request"):
        check_issue(_raw(pull_request={"url": "x"}))


@pytest.mark.parametrize(
    "user",
    [
        {"login": "dependabot[bot]", "type": "User"},
        {"login": "some-app", "type": "Bot"},
    ],
)
def test_a_bot_may_not_even_with_a_trusted_association(user: dict[str, str]) -> None:
    with pytest.raises(NotTrusted, match="bot"):
        check_issue(_raw(user=user))


@pytest.mark.parametrize("permission", ["admin", "maintain", "write"])
def test_a_maintainer_who_labelled_an_outsiders_issue_lets_it_through(
    permission: str,
) -> None:
    check_issue(_raw(author_association="NONE"), actor_permission=permission)


@pytest.mark.parametrize("permission", ["triage", "read", "none", "", None])
def test_a_labeller_without_write_access_does_not(permission: str | None) -> None:
    with pytest.raises(NotTrusted):
        check_issue(_raw(author_association="NONE"), actor_permission=permission)


@pytest.mark.parametrize("permission", ["triage", "read", "none", ""])
def test_a_labeller_without_write_access_is_refused_even_for_a_trusted_author(
    permission: str,
) -> None:
    """Whoever applies the label starts the run, so it is their access that counts."""
    with pytest.raises(NotTrusted, match="label"):
        check_issue(_raw(author_association="OWNER"), actor_permission=permission)


@pytest.mark.parametrize("permission", ["admin", "maintain", "write"])
def test_a_labeller_with_write_access_is_fine_for_a_trusted_author(
    permission: str,
) -> None:
    check_issue(_raw(author_association="OWNER"), actor_permission=permission)


def test_the_labeller_cannot_vouch_for_a_pull_request_or_a_bot() -> None:
    with pytest.raises(NotTrusted):
        check_issue(_raw(pull_request={}), actor_permission="admin")
    with pytest.raises(NotTrusted):
        check_issue(
            _raw(user={"login": "x[bot]", "type": "Bot"}), actor_permission="admin"
        )


@pytest.mark.parametrize(
    "reply", [None, [], "write", {}, {"permission": 5}, {"permission": None}]
)
def test_a_reply_that_is_not_a_permission_means_no_access(reply: Any) -> None:
    with patch("triage.github_request", return_value=reply):
        assert implement.actor_permission("o/r", "someone", "t") == "none"


@pytest.mark.parametrize("login", ["", "a/b", "a b", "../x", "x?y=1", "a\nb", "x;y"])
def test_a_login_that_could_change_the_request_is_not_looked_up(login: str) -> None:
    api = FakeGitHub(permission="admin")
    with patch("triage.github_request", api):
        assert implement.actor_permission("o/r", login, "t") == "none"
    assert api.calls == []


def test_the_permission_of_a_real_login_is_read() -> None:
    api = FakeGitHub(permission="maintain")
    with patch("triage.github_request", api):
        assert implement.actor_permission("o/r", "some-user", "t") == "maintain"
        assert implement.actor_permission("o/r", "app[bot]", "t") == "maintain"


def test_the_reason_never_quotes_the_issue() -> None:
    with pytest.raises(NotTrusted) as excinfo:
        check_issue(_raw(author_association="::error::pwned"))
    assert "pwned" not in str(excinfo.value)


@pytest.mark.parametrize("raw", [{}, {"number": "x"}, {"title": "no number"}])
def test_something_that_is_not_an_issue_is_rejected(raw: dict[str, Any]) -> None:
    with pytest.raises(NotTrusted):
        check_issue(raw)


def test_a_missing_body_is_an_empty_one() -> None:
    assert check_issue(_raw(body=None)).body == ""


# --- the file the model reads ---


def test_the_issue_is_written_as_markdown() -> None:
    text = issue_markdown(check_issue(_raw()))
    assert text.startswith("# slugify leaves a hyphen\n\n")
    assert "It prints -hello-world-." in text


def test_title_and_body_are_cut_to_their_limits() -> None:
    issue = check_issue(_raw(title="t" * 1000, body="b" * 20000))
    text = issue_markdown(issue)
    title, _, body = text.removeprefix("# ").partition("\n\n")
    assert len(title) <= implement.MAX_TITLE_CHARS
    assert len(body) <= implement.MAX_BODY_CHARS


def test_characters_a_reader_cannot_see_are_removed() -> None:
    hidden = "\U000e0049\U000e0047​‮"
    text = issue_markdown(check_issue(_raw(title="a" + hidden, body="b" + hidden)))
    assert not any(ch in text for ch in hidden)


def test_the_description_file_is_a_protected_name_wherever_it_is() -> None:
    for path in (".agent-change.md", "docs/.agent-change.md", ".AGENT-CHANGE.md"):
        assert implement.check_changes([path]), path


def test_the_issue_file_is_excluded_from_commits(tmp_path: Path) -> None:
    target = tmp_path / ".agent-issue.md"
    exclude = tmp_path / ".git" / "info" / "exclude"
    write_issue_file("hello", target, exclude)
    write_issue_file("hello again", target, exclude)  # a second run adds no duplicate
    assert target.read_text() == "hello again"
    assert exclude.read_text().splitlines().count(".agent-issue.md") == 1


def test_an_existing_exclude_file_is_kept(tmp_path: Path) -> None:
    exclude = tmp_path / "exclude"
    exclude.write_text("# my rules\n*.log\n")
    write_issue_file("x", tmp_path / ".agent-issue.md", exclude)
    assert exclude.read_text().splitlines() == [
        "# my rules",
        "*.log",
        ".agent-issue.md",
    ]


# --- the verdict contract ---


def _verdict_json(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "valid": "yes",
        "reproduced": "yes",
        "effort": "small",
        "evidence": "Ran `python3 slug.py x` and it printed -x-.",
        "summary": "Real defect.",
    }
    return json.dumps({**base, **overrides})


def _verdict(**overrides: Any) -> Verdict:
    return parse_verdict(_verdict_json(**overrides))


def test_a_good_verdict_is_read() -> None:
    assert _verdict() == Verdict(
        "yes",
        "yes",
        "small",
        "Ran `python3 slug.py x` and it printed -x-.",
        "Real defect.",
    )


@pytest.mark.parametrize("raw", ["", "not json", "[]", "3", "null", '"yes"', "{"])
def test_a_verdict_that_is_not_an_object_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_verdict(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid", "maybe"),
        ("valid", True),
        ("valid", ["yes"]),
        ("valid", None),
        ("reproduced", "sometimes"),
        ("reproduced", "YES"),
        ("effort", "huge"),
        ("effort", 3),
    ],
)
def test_a_value_outside_the_contract_rejects_the_whole_verdict(
    field: str, value: Any
) -> None:
    with pytest.raises(ValueError):
        _verdict(**{field: value})


@pytest.mark.parametrize("field", ["valid", "reproduced", "effort"])
def test_a_missing_enumerated_field_rejects_the_verdict(field: str) -> None:
    data = json.loads(_verdict_json())
    del data[field]
    with pytest.raises(ValueError):
        parse_verdict(json.dumps(data))


def test_missing_text_is_empty_and_extra_fields_are_ignored() -> None:
    data = {"valid": "no", "reproduced": "no", "effort": "large", "labels": ["spam"]}
    verdict = parse_verdict(json.dumps(data))
    assert (verdict.evidence, verdict.summary) == ("", "")
    assert not hasattr(verdict, "labels")


def test_text_that_is_not_a_string_is_made_into_one() -> None:
    verdict = _verdict(summary=42, evidence=["a", "b"])
    assert verdict.summary == "42" and isinstance(verdict.evidence, str)


def test_text_is_cut_and_stripped_of_invisible_characters() -> None:
    verdict = _verdict(evidence="e" * 5000, summary="s​\U000e0041" * 1000)
    assert len(verdict.evidence) <= implement.MAX_EVIDENCE_CHARS
    assert len(verdict.summary) <= implement.MAX_SUMMARY_CHARS
    assert "​" not in verdict.summary and "\U000e0041" not in verdict.summary


@pytest.mark.parametrize("claim", ["yes", "by_reading"])
@pytest.mark.parametrize("evidence", ["", "   \n", "​‍", "\U000e0049"])
def test_a_claim_of_reproduction_without_evidence_is_not_believed(
    claim: str, evidence: str
) -> None:
    """Both a claim of having run it and a claim of having read the fault need something to
    point at; without it the honest word is not_attempted."""
    assert _verdict(reproduced=claim, evidence=evidence).reproduced == "not_attempted"


def test_a_claim_of_reading_the_fault_keeps_its_word_when_there_is_evidence() -> None:
    verdict = _verdict(
        reproduced="by_reading", evidence="src/main.rs:66 maps every error"
    )
    assert verdict.reproduced == "by_reading"


@pytest.mark.parametrize("reproduced", ["no", "not_applicable", "not_attempted"])
def test_only_a_claim_of_success_needs_evidence(reproduced: str) -> None:
    assert _verdict(reproduced=reproduced, evidence="").reproduced == reproduced


# --- the gate ---


@pytest.mark.parametrize(
    ("valid", "reproduced", "effort", "expected"),
    [
        ("yes", "yes", "small", True),
        ("yes", "yes", "medium", True),
        ("yes", "not_applicable", "small", True),
        ("yes", "by_reading", "small", True),
        ("yes", "by_reading", "medium", True),
        ("yes", "by_reading", "large", False),
        ("no", "by_reading", "small", False),
        ("unclear", "by_reading", "small", False),
        ("yes", "yes", "large", False),
        ("yes", "no", "small", False),
        ("yes", "not_attempted", "small", False),
        ("no", "yes", "small", False),
        ("unclear", "yes", "small", False),
        ("no", "not_applicable", "small", False),
    ],
)
def test_the_gate(valid: str, reproduced: str, effort: str, expected: bool) -> None:
    assert gate(_verdict(valid=valid, reproduced=reproduced, effort=effort)) is expected


def test_a_claim_without_evidence_does_not_pass_the_gate() -> None:
    assert not gate(_verdict(reproduced="yes", evidence=""))


# --- what is written ---

ALL_COMBINATIONS = [
    _verdict(valid=v, reproduced=r, effort=e)
    for v in ("yes", "no", "unclear")
    for r in ("yes", "by_reading", "no", "not_applicable", "not_attempted")
    for e in ("small", "medium", "large")
]


@pytest.mark.parametrize(
    ("reproduced", "label"),
    [
        ("yes", "repro:confirmed"),
        ("no", "repro:failed"),
        ("not_applicable", "repro:not-applicable"),
        ("by_reading", "repro:by-reading"),
    ],
)
def test_reproduction_becomes_a_label(reproduced: str, label: str) -> None:
    assert label in make_report(_verdict(reproduced=reproduced)).labels


def test_nothing_is_claimed_when_nothing_was_attempted() -> None:
    labels = make_report(_verdict(reproduced="not_attempted")).labels
    assert not any(name.startswith("repro:") for name in labels)


@pytest.mark.parametrize("effort", ["small", "medium", "large"])
def test_effort_becomes_a_label_for_a_valid_issue(effort: str) -> None:
    assert f"effort:{effort}" in make_report(_verdict(effort=effort)).labels


@pytest.mark.parametrize("valid", ["no", "unclear"])
def test_no_effort_is_claimed_for_an_issue_that_is_not_valid(valid: str) -> None:
    labels = make_report(_verdict(valid=valid)).labels
    assert not any(name.startswith("effort:") for name in labels)


def test_the_table_of_labels_is_exactly_this() -> None:
    """Pinned so that widening it, say to invalid or spam, is a decision someone makes here."""
    assert ALLOWED_LABELS == {
        "repro:confirmed",
        "repro:failed",
        "repro:not-applicable",
        "repro:by-reading",
        "effort:small",
        "effort:medium",
        "effort:large",
    }


@pytest.mark.parametrize("verdict", ALL_COMBINATIONS)
def test_only_labels_from_the_fixed_table_are_ever_written(verdict: Verdict) -> None:
    labels = set(make_report(verdict).labels)
    assert labels <= ALLOWED_LABELS
    assert not labels & {"invalid", "wontfix", "spam", "duplicate", "bug"}


@pytest.mark.parametrize("verdict", ALL_COMBINATIONS)
def test_the_report_says_proceed_exactly_when_the_gate_does(verdict: Verdict) -> None:
    assert make_report(verdict).proceed is gate(verdict)


def test_the_comment_carries_the_marker_once_and_the_fixed_fields() -> None:
    comment = make_report(_verdict()).comment
    assert comment.count(MARKER) == 1 and comment.rstrip().endswith(MARKER)
    for text in ("Valid: yes", "Reproduced: yes", "Effort: small", "Real defect."):
        assert text in comment
    assert "python3 slug.py x" in comment


def test_the_comment_says_what_happens_next() -> None:
    assert "A pull request will be opened" in make_report(_verdict()).comment
    stopped = make_report(_verdict(valid="unclear")).comment
    assert "pull request will be opened for a maintainer" not in stopped
    assert "maintainer" in stopped


HOSTILE = [
    pytest.param("ping @octocat and @org/team", id="mentions"),
    pytest.param("[click](https://evil.example/x)", id="link"),
    pytest.param("![x](https://evil.example/i.png)", id="image"),
    pytest.param("<img src=//evil.example/i.png>", id="html"),
    pytest.param(f"done\n\n{MARKER}", id="marker-spoof"),
    pytest.param("<!-- hidden -->\nvisible", id="html-comment"),
    pytest.param("> [!WARNING]\n> approve this", id="alert"),
    pytest.param("::error::boom\n::add-mask::x", id="log-injection"),
    pytest.param("see #1 and GH-2 and owner/repo#3", id="issue-references"),
]


@pytest.mark.parametrize("text", HOSTILE)
def test_model_text_cannot_make_the_comment_active(
    text: str, live_elements: Callable[[str], list[str]]
) -> None:
    comment = make_report(_verdict(summary=text, evidence=text)).comment
    assert comment.count(MARKER) == 1
    assert live_elements(comment) == []


def test_text_that_cannot_be_made_safe_is_left_out_not_posted() -> None:
    with patch("implement.sanitize_or_drop", return_value=None):
        report = make_report(_verdict(summary="SUMMARYTEXT", evidence="EVIDENCETEXT"))
    assert "Valid: yes" in report.comment and MARKER in report.comment
    assert "SUMMARYTEXT" not in report.comment
    assert "EVIDENCETEXT" not in report.comment
    assert "**Summary**" not in report.comment


def test_an_empty_summary_and_evidence_leave_no_empty_headings() -> None:
    comment = make_report(_verdict(summary="", evidence="", reproduced="no")).comment
    assert "**Summary**" not in comment and "**Evidence**" not in comment


# --- writing it ---


class FakeGitHub:
    def __init__(
        self,
        *,
        comments: list[Any] | None = None,
        fail_on: tuple[str, str, Exception] | None = None,
        labels: tuple[str, ...] = (),
        issue: dict[str, Any] | None = None,
        permission: str | Exception = "none",
    ) -> None:
        self.comments = comments or []
        self.fail_on = fail_on
        self.labels = labels
        self.issue = issue
        self.permission = permission
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(
        self, method: str, repo: str, path: str, token: str, payload: Any = None
    ) -> Any:
        self.calls.append((method, path, payload))
        if self.fail_on and (method, path.split("?")[0]) == self.fail_on[:2]:
            raise self.fail_on[2]
        if method == "GET" and path.startswith("/issues/") and "/comments" in path:
            return self.comments
        if method == "GET" and path.startswith("/issues/"):
            return self.issue
        if method == "GET" and path.startswith("/labels"):
            return [{"name": name} for name in self.labels]
        if method == "GET" and "/permission" in path:
            if isinstance(self.permission, Exception):
                raise self.permission
            return {"permission": self.permission}
        return {}

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", MagicMock(), MagicMock())


def test_labels_are_written_before_the_comment_so_the_marker_means_done() -> None:
    api = FakeGitHub()
    with patch("triage.github_request", api):
        apply_report(make_report(_verdict()), "o/r", 17, "t", frozenset())
    assert [(m, p) for m, p, _ in api.writes][-2:] == [
        ("POST", "/issues/17/labels"),
        ("POST", "/issues/17/comments"),
    ]


def test_missing_labels_are_created_with_fixed_text() -> None:
    api = FakeGitHub()
    with patch("triage.github_request", api):
        apply_report(make_report(_verdict()), "o/r", 17, "t", frozenset())
    created = {p["name"]: p for m, path, p in api.writes if path == "/labels"}
    assert set(created) == {"repro:confirmed", "effort:small"}
    for payload in created.values():
        assert set(payload) == {"name", "color", "description"}
        assert (payload["color"], payload["description"]) == implement.LABEL_STYLE[
            payload["name"]
        ]


def test_labels_the_repository_has_are_not_created_again() -> None:
    api = FakeGitHub()
    have = frozenset({"repro:confirmed", "effort:small"})
    with patch("triage.github_request", api):
        apply_report(make_report(_verdict()), "o/r", 17, "t", have)
    assert "/labels" not in [path for _, path, _ in api.writes]


def test_a_label_created_in_the_meantime_is_fine() -> None:
    api = FakeGitHub(fail_on=("POST", "/labels", _http_error(422)))
    with patch("triage.github_request", api):
        apply_report(make_report(_verdict()), "o/r", 17, "t", frozenset())
    assert api.writes[-1][1] == "/issues/17/comments"


def test_another_error_while_creating_a_label_is_not_swallowed() -> None:
    api = FakeGitHub(fail_on=("POST", "/labels", _http_error(500)))
    with patch("triage.github_request", api), pytest.raises(urllib.error.HTTPError):
        apply_report(make_report(_verdict()), "o/r", 17, "t", frozenset())
    assert not [p for _, p, _ in api.writes if p.endswith("/comments")]


def test_a_verdict_with_no_labels_still_posts_its_comment() -> None:
    report = make_report(_verdict(valid="no", reproduced="not_attempted"))
    api = FakeGitHub()
    with patch("triage.github_request", api):
        apply_report(report, "o/r", 17, "t", frozenset())
    assert [p for _, p, _ in api.writes] == ["/issues/17/comments"]


# --- the commands ---

ENV = {
    "GITHUB_REPOSITORY": "o/r",
    "GITHUB_TOKEN": "t",
    "ISSUE_NUMBER": "17",
}


def _run(
    command: str | list[str],
    api: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **env: str,
) -> tuple[int, str]:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "github_output"
    output.write_text("")
    full = {**ENV, "GITHUB_OUTPUT": str(output), **env}
    with patch.dict(os.environ, full, clear=True), patch("triage.github_request", api):
        try:
            implement.main([command] if isinstance(command, str) else command)
            code = 0
        except SystemExit as exit_:
            code = int(exit_.code or 0)
    return code, output.read_text()


def test_fetch_writes_the_issue_for_a_trusted_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw())
    code, _ = _run("fetch", api, tmp_path, monkeypatch)
    assert code == 0
    assert "It prints -hello-world-." in (tmp_path / ".agent-issue.md").read_text()
    assert api.writes == []


def test_fetch_keeps_both_agent_files_out_of_any_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The description file does not exist yet when the model starts; it is the model that
    writes it, so it has to be ignored before then."""
    api = FakeGitHub(issue=_raw())
    code, _ = _run("fetch", api, tmp_path, monkeypatch)
    assert code == 0
    ignored = (tmp_path / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".agent-issue.md" in ignored and ".agent-change.md" in ignored
    assert not (tmp_path / ".agent-change.md").exists()


def test_fetch_refuses_an_untrusted_author_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeGitHub(issue=_raw(author_association="NONE"))
    code, _ = _run("fetch", api, tmp_path, monkeypatch)
    assert code == 1
    assert not (tmp_path / ".agent-issue.md").exists()
    assert "::error::" in capsys.readouterr().out


def test_fetch_lets_a_maintainers_label_vouch_for_an_outsiders_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw(author_association="NONE"), permission="write")
    code, _ = _run("fetch", api, tmp_path, monkeypatch, ACTOR_LOGIN="maintainer")
    assert code == 0 and (tmp_path / ".agent-issue.md").exists()
    assert ("GET", "/collaborators/maintainer/permission", None) in api.calls


def test_fetch_asks_about_the_labeller_even_when_the_author_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw(), permission="write")
    code, _ = _run("fetch", api, tmp_path, monkeypatch, ACTOR_LOGIN="someone")
    assert code == 0
    assert ("GET", "/collaborators/someone/permission", None) in api.calls


def test_fetch_refuses_a_labeller_without_write_access_for_a_trusted_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw(), permission="triage")
    code, _ = _run("fetch", api, tmp_path, monkeypatch, ACTOR_LOGIN="someone")
    assert code == 1 and not (tmp_path / ".agent-issue.md").exists()


def test_fetch_asks_about_nobody_when_the_run_was_not_started_by_a_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw())
    _run("fetch", api, tmp_path, monkeypatch)
    assert not [c for c in api.calls if "permission" in c[1]]


def test_fetch_treats_an_actor_it_cannot_look_up_as_having_no_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw(author_association="NONE"), permission=_http_error(404))
    code, _ = _run("fetch", api, tmp_path, monkeypatch, ACTOR_LOGIN="ghost")
    assert code == 1 and not (tmp_path / ".agent-issue.md").exists()


def test_fetch_treats_a_labeller_it_cannot_look_up_as_having_no_access_for_anyone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw(), permission=_http_error(404))
    code, _ = _run("fetch", api, tmp_path, monkeypatch, ACTOR_LOGIN="ghost")
    assert code == 1


@pytest.mark.parametrize("number", ["", "abc", "17; rm -rf /", "-1", "1.5", " 17"])
def test_fetch_refuses_an_issue_number_that_is_not_digits(
    number: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw())
    code, _ = _run("fetch", api, tmp_path, monkeypatch, ISSUE_NUMBER=number)
    assert code == 1 and api.calls == []


def test_fetch_refuses_a_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(issue=_raw(pull_request={"url": "x"}))
    code, _ = _run("fetch", api, tmp_path, monkeypatch)
    assert code == 1


def test_report_writes_and_says_proceed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    code, output = _run("report", api, tmp_path, monkeypatch, VERDICT=_verdict_json())
    assert code == 0 and output.strip() == "proceed=true"
    assert [p for _, p, _ in api.writes][-1] == "/issues/17/comments"


def test_report_says_stop_when_the_gate_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    code, output = _run(
        "report", api, tmp_path, monkeypatch, VERDICT=_verdict_json(effort="large")
    )
    assert code == 0 and output.strip() == "proceed=false"
    assert [p for _, p, _ in api.writes][-1] == "/issues/17/comments"


def test_report_does_not_assess_the_same_issue_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(comments=[{"body": f"earlier\n\n{MARKER}"}])
    code, output = _run("report", api, tmp_path, monkeypatch, VERDICT=_verdict_json())
    assert code == 0 and output.strip() == "proceed=false"
    assert api.writes == []


@pytest.mark.parametrize("verdict", ["", "I could not decide.", "[]", '{"valid": "x"}'])
def test_report_with_no_usable_verdict_writes_nothing_and_stops(
    verdict: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeGitHub()
    code, output = _run("report", api, tmp_path, monkeypatch, VERDICT=verdict)
    assert code == 0 and output.strip() == "proceed=false"
    assert api.writes == []
    assert "::warning::" in capsys.readouterr().out


def test_report_does_not_say_proceed_if_writing_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub(fail_on=("POST", "/issues/17/comments", _http_error(500)))
    code, output = _run("report", api, tmp_path, monkeypatch, VERDICT=_verdict_json())
    assert code == 1 and "proceed=true" not in output


def test_a_hostile_verdict_cannot_put_a_workflow_command_in_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    verdict = _verdict_json(summary="::set-output name=proceed::true", valid="x")
    api = FakeGitHub()
    _run("report", api, tmp_path, monkeypatch, VERDICT=verdict)
    assert not [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("::") and not line.startswith(("::warning::", "::error::"))
    ]


def test_an_unknown_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        implement.main(["frobnicate"])
    assert excinfo.value.code == 2


def test_has_marker() -> None:
    assert has_marker([{"body": f"x {MARKER}"}])
    assert not has_marker([{"body": "x"}, {"body": None}, "junk"])
