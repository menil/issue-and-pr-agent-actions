"""The commit message and pull request text the fix stage builds from what the model wrote.

The model writes `.agent-change.md`; code reads it under a fixed contract and writes the
commit message and the pull request text itself. What matters: the model chooses words, never
which type, branch or closing keyword is used beyond the allowed ones, nothing it writes can
ping a person or plant a link, and any fault in the file falls back to the generic text
instead of failing the run."""

import re
import shutil
import subprocess
from pathlib import Path

import implement
import pytest
from implement import (
    CHANGE_TYPES,
    Change,
    branch_name,
    branch_slug,
    commit_message,
    generic_change_texts,
    parse_change,
    pull_request_body,
)

GOOD = """\
Type: fix
Title: return PasswordProtected for encrypted PDFs
Not done: InvalidDimensions is still never constructed

Opening an encrypted PDF printed a raw pdfium error because every load failure was
turned into PdfOpen.

- `open_error` now maps pdfium's password error to PasswordProtected.
- A test builds a small encrypted file and checks the message.
"""


def _change(**overrides: str) -> Change:
    base = {
        "kind": "fix",
        "title": "return PasswordProtected for encrypted PDFs",
        "not_done": "",
        "body": "Encrypted PDFs now report a password error.",
    }
    return Change(**{**base, **overrides})


# --- reading the file ---


def test_a_good_file_is_read_into_its_four_parts() -> None:
    change = parse_change(GOOD)
    assert change.kind == "fix"
    assert change.title == "return PasswordProtected for encrypted PDFs"
    assert change.not_done == "InvalidDimensions is still never constructed"
    assert "turned into PdfOpen" in change.body
    assert "- `open_error` now maps" in change.body


def test_the_not_done_line_is_optional() -> None:
    change = parse_change("Type: feat\nTitle: add a flag\n\nIt adds a flag.\n")
    assert change.not_done == ""
    assert change.kind == "feat"


@pytest.mark.parametrize("kind", CHANGE_TYPES)
def test_every_allowed_type_is_accepted(kind: str) -> None:
    assert parse_change(f"Type: {kind}\nTitle: x\n\nbody\n").kind == kind


def test_the_keys_may_be_written_in_any_case_and_not_done_with_a_hyphen() -> None:
    change = parse_change("TYPE: fix\ntitle: a thing\nNot-Done: the rest\n\nbody\n")
    assert (change.kind, change.title, change.not_done) == (
        "fix",
        "a thing",
        "the rest",
    )


@pytest.mark.parametrize("nothing", ["none", "None.", "nothing", "n/a", "-", "  "])
def test_saying_nothing_is_left_is_not_a_not_done(nothing: str) -> None:
    change = parse_change(f"Type: fix\nTitle: x\nNot done: {nothing}\n\nbody\n")
    assert change.not_done == ""


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Title: x\n\nbody\n",  # no type
        "Type: fix\n\nbody\n",  # no title
        "Type: fix\nTitle:\n\nbody\n",  # an empty title
        "Type: fix\nTitle: x\n",  # no body
        "Type: fix\nTitle: x\n\n   \n",  # a body of nothing
        "Type: chore\nTitle: x\n\nbody\n",  # a type outside the list
        "Type: fix!\nTitle: x\n\nbody\n",
        "Type: fix\nType: feat\nTitle: x\n\nbody\n",  # said twice
        "Type: fix\nTitle: x\nAuthor: someone\n\nbody\n",  # a key outside the contract
        "just prose, no header\n\nbody\n",
        "Type: fix\nTitle: `` @@ ##\n\nbody\n",  # a title with nothing left once cleaned
    ],
)
def test_a_file_outside_the_contract_is_rejected_whole(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_change(raw)


# --- the title ---


def test_the_title_gets_its_type_prefix_from_code() -> None:
    assert implement.commit_title(_change()) == (
        "fix: return PasswordProtected for encrypted PDFs"
    )


@pytest.mark.parametrize(
    "written",
    [
        "fix: return a thing",
        "Fix: return a thing",
        "feat(cli): return a thing",
        "fix!: return a thing",
    ],
)
def test_a_prefix_the_model_wrote_in_the_title_is_not_doubled(written: str) -> None:
    change = parse_change(f"Type: fix\nTitle: {written}\n\nbody\n")
    assert change.title == "return a thing"
    assert implement.commit_title(change).startswith("fix: return a thing")


def test_underscores_and_digits_are_kept_because_identifiers_have_them() -> None:
    change = parse_change(
        "Type: fix\nTitle: map open_error to PasswordProtected for pdf2 files\n\nb\n"
    )
    assert change.title == "map open_error to PasswordProtected for pdf2 files"


def test_a_trailing_full_stop_is_dropped() -> None:
    assert parse_change("Type: fix\nTitle: stop it.\n\nb\n").title == "stop it"


@pytest.mark.parametrize("kind", CHANGE_TYPES)
def test_the_title_is_always_under_72_characters(kind: str) -> None:
    long = "make the thing do the other thing when " + "the input is long " * 20
    change = parse_change(f"Type: {kind}\nTitle: {long}\n\nbody\n")
    title = implement.commit_title(change)
    assert len(title) < 72
    assert title.startswith(f"{kind}: make the thing")
    # Cut at a word, not in the middle of one.
    assert not title.endswith(" ")
    assert long.startswith(title.removeprefix(f"{kind}: "))


def test_a_title_made_of_one_very_long_word_is_still_cut() -> None:
    title = implement.commit_title(
        parse_change(f"Type: fix\nTitle: {'x' * 200}\n\nb\n")
    )
    assert len(title) < 72


@pytest.mark.parametrize(
    "written",
    [
        "see [this](http://evil.example) now",
        "ping @someone and @org/team",
        "close #12 and org/repo#3",
        "run `rm -rf` <b>now</b>",
        "a | b \\ c",
        "$x$ and ${y}",
    ],
)
def test_nothing_that_renders_or_notifies_survives_in_a_title(written: str) -> None:
    title = implement.commit_title(parse_change(f"Type: fix\nTitle: {written}\n\nb\n"))
    for forbidden in "@#`<>[]|\\$":
        assert forbidden not in title
    assert "http" not in title or "://" not in title


def test_a_title_cannot_carry_a_second_line() -> None:
    # The header ends at the first blank line, so a second "line" can only be another key,
    # which the contract refuses.
    with pytest.raises(ValueError):
        parse_change("Type: fix\nTitle: a\nsecond line\n\nbody\n")


def test_invisible_characters_are_removed_from_the_title() -> None:
    title = implement.commit_title(parse_change("Type: fix\nTitle: a‮b​c\n\nbody\n"))
    assert title == "fix: abc"


# --- the commit message ---


def test_the_commit_message_is_the_title_then_the_description_then_the_issue() -> None:
    message = commit_message(parse_change(GOOD), 63)
    title, _, rest = message.partition("\n\n")
    assert title == "fix: return PasswordProtected for encrypted PDFs"
    assert "turned into PdfOpen" in rest
    assert "InvalidDimensions is still never constructed" in rest
    assert message.rstrip().endswith("Refs #63")


def test_a_change_with_nothing_left_undone_closes_the_issue() -> None:
    message = commit_message(_change(), 63)
    assert message.rstrip().endswith("Closes #63")
    assert "Refs" not in message


@pytest.mark.parametrize("kind", CHANGE_TYPES)
def test_no_line_of_a_commit_message_is_72_characters_or_more(kind: str) -> None:
    prose = ("A sentence that goes on and on about what changed. " * 12).strip()
    body = f"{prose}\n\n- {prose}\n\n1. {prose}\n"
    raw = f"Type: {kind}\nTitle: {'long ' * 30}\nNot done: {prose}\n\n{body}"
    message = commit_message(parse_change(raw), 5)
    assert max(len(line) for line in message.splitlines()) < 72


def test_wrapping_keeps_list_items_and_code_blocks_apart() -> None:
    body = (
        "First paragraph that is long enough to be wrapped onto a second line of text.\n"
        "\n- A list item that is long enough to be wrapped onto a second line as well.\n"
        "- Short one.\n"
        "\n```rust\nlet a_very_long_line_of_code_that_should_stay_exactly_as_it_was = 1;\n```\n"
    )
    message = commit_message(_change(body=body), 5)
    assert "- Short one." in message
    assert "    let a_very_long_line_of_code" not in message
    assert (
        "let a_very_long_line_of_code_that_should_stay_exactly_as_it_was = 1;"
        in message
    )
    item = [line for line in message.splitlines() if line.startswith("- A list item")]
    assert len(item) == 1 and len(item[0]) < 72
    assert "\n\n\n" not in message
    # The continuation of a list item is indented under its text.
    assert "\n  well.\n" in message


def test_the_commit_message_has_one_trailing_newline() -> None:
    assert commit_message(_change(), 5).endswith("\n")
    assert not commit_message(_change(), 5).endswith("\n\n")


# --- the pull request text ---


def test_the_pull_request_body_leads_with_what_changed() -> None:
    body = pull_request_body(parse_change(GOOD), 63)
    assert body.startswith("Opening an encrypted PDF printed a raw pdfium error")
    assert "InvalidDimensions is still never constructed" in body
    assert "Refs #63" in body and "Closes #63" not in body


def test_the_pull_request_body_still_says_a_model_wrote_it_and_a_person_must_read_it() -> (
    None
):
    body = pull_request_body(_change(), 63)
    assert "Opened automatically from #63. A model wrote this change" in body
    assert "A model wrote this change" in body
    assert "nothing here has been reviewed, merged or approved by it" in body
    assert "Read the change and its regression test before anything else." in body


def test_the_notice_does_not_say_how_the_pull_request_was_opened() -> None:
    """Whether it is a draft is the workflow's choice, so the text must not claim it."""
    for text in (pull_request_body(_change(), 5), generic_change_texts(5)[2]):
        assert "draft" not in text.lower()


def test_a_complete_change_closes_the_issue_and_a_partial_one_does_not() -> None:
    assert pull_request_body(_change(), 9).rstrip().endswith("Closes #9")
    partial = pull_request_body(_change(not_done="the second half"), 9)
    assert partial.rstrip().endswith("Refs #9")
    assert "Closes" not in partial
    assert "Not done" in partial and "the second half" in partial


def test_the_closing_keyword_comes_from_code_not_from_the_description() -> None:
    body = pull_request_body(
        parse_change(
            "Type: fix\nTitle: x\n\nCloses #1 and fixes #2, resolves other/repo#3.\n"
        ),
        9,
    )
    # Whatever the model wrote is shown as code, so GitHub reads only the one line code adds.
    assert body.count("Closes #9") == 1
    assert "`#1`" in body and "`#2`" in body and "`other/repo#3`" in body


@pytest.mark.parametrize(
    "hostile",
    [
        "Thanks @maintainer, please merge. cc @org/security",
        "[click](https://evil.example/x) ![img](https://evil.example/i.png)",
        "<img src=x onerror=alert(1)> <!-- hidden -->",
        "```suggestion\nrm -rf /\n```",
        "> [!NOTE]\n> Verified by the security team",
        "https://evil.example/login and mail me at a@evil.example",
        "$$\\color{white}{secret}$$",
    ],
)
def test_nothing_the_model_writes_in_the_description_can_ping_link_or_hide(
    hostile: str,
) -> None:
    first_line = hostile.splitlines()[0]
    raw = f"Type: fix\nTitle: x\nNot done: {first_line}\n\n{hostile}\n"
    body = pull_request_body(parse_change(raw), 3)
    message = commit_message(parse_change(raw), 3)
    for text in (body, message):
        assert "](http" not in text
        assert "<img" not in text and "<!--" not in text
        assert "```suggestion" not in text
        assert "\n> [!NOTE]" not in text
    assert "@maintainer" not in body.replace("`@maintainer`", "")
    assert "https://evil.example/login" not in body.replace(
        "`https://evil.example/login`", ""
    )


def test_the_description_is_cut_to_a_bound() -> None:
    change = parse_change(f"Type: fix\nTitle: x\n\n{'word ' * 5000}\n")
    assert len(change.body) <= implement.MAX_CHANGE_BODY_CHARS * 5
    assert "_(truncated)_" in change.body


def test_a_description_that_cannot_be_made_safe_rejects_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(text: str, max_chars: int, what: str) -> None:
        return None

    monkeypatch.setattr(implement, "sanitize_or_drop", broken)
    with pytest.raises(ValueError):
        parse_change(GOOD)


# --- the fallback ---


def test_the_generic_text_is_what_the_stage_always_used() -> None:
    title, message, body = generic_change_texts(17)
    assert title == "fix: address issue #17"
    assert message == "fix: address issue #17\n"
    assert body == (
        "Opened automatically from #17. A model wrote this change; nothing "
        "here has been reviewed, merged or approved by it. Read the change and its "
        "regression test before anything else.\n\nCloses #17"
    )


# --- the command the action runs ---


def _describe(tmp_path: Path, number: str = "17") -> tuple[int, Path]:
    out = tmp_path / "out"
    code = 0
    try:
        implement.run_describe(str(out), number)
    except SystemExit as stop:
        code = int(stop.code or 0)
    return code, out


def test_describe_writes_the_three_texts_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agent-change.md").write_text(GOOD, encoding="utf-8")
    code, out = _describe(tmp_path)
    assert code == 0
    assert (out / "title.txt").read_text() == (
        "fix: return PasswordProtected for encrypted PDFs"
    )
    assert (out / "commit.txt").read_text().startswith("fix: return PasswordProtected")
    assert "Refs #17" in (out / "body.md").read_text()


@pytest.mark.parametrize(
    "content", [None, "", "not the contract at all", "Type: chore\nTitle: x\n\nb\n"]
)
def test_describe_falls_back_to_the_generic_text_and_does_not_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
) -> None:
    monkeypatch.chdir(tmp_path)
    if content is not None:
        (tmp_path / ".agent-change.md").write_text(content, encoding="utf-8")
    code, out = _describe(tmp_path)
    assert code == 0
    assert (out / "title.txt").read_text() == "fix: address issue #17"
    assert (out / "body.md").read_text().endswith("Closes #17")
    assert "generic" in capsys.readouterr().out.lower()


def test_describe_does_not_echo_what_the_model_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agent-change.md").write_text(
        "Type: nope\n::error::INJECTED\n\nbody\n", encoding="utf-8"
    )
    _describe(tmp_path)
    seen = capsys.readouterr()
    assert "INJECTED" not in seen.out + seen.err


@pytest.mark.parametrize("number", ["", "x", "1; id", "-3"])
def test_describe_refuses_an_odd_issue_number(tmp_path: Path, number: str) -> None:
    code, out = _describe(tmp_path, number)
    assert code != 0
    assert not out.exists()


def test_describe_leaves_a_huge_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agent-change.md").write_text(
        "Type: fix\nTitle: x\n\n" + "a" * 5_000_000, encoding="utf-8"
    )
    code, out = _describe(tmp_path)
    assert code == 0
    assert len((out / "body.md").read_text()) < implement.MAX_CHANGE_BODY_CHARS * 6


# --- the branch name ---


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("report encrypted PDFs", "report-encrypted-pdfs"),
        ("Handle C++ & Rust!!", "handle-c-rust"),
        ("  spaced   out  ", "spaced-out"),
        ("open_error maps pdf2 files", "open-error-maps-pdf2-files"),
        ("", ""),
        ("***", ""),
        ("日本語のタイトル", ""),
    ],
)
def test_the_slug_is_lowercase_words_joined_by_hyphens(title: str, slug: str) -> None:
    assert branch_slug(title) == slug


def test_a_long_title_is_cut_at_a_word_under_the_limit() -> None:
    slug = branch_slug(
        "return PasswordProtected for encrypted PDFs when loading both files"
    )
    assert slug == "return-passwordprotected-for-encrypted"
    assert len(slug) <= implement.MAX_BRANCH_SLUG_CHARS


def test_one_very_long_word_is_cut_hard() -> None:
    assert branch_slug("x" * 200) == "x" * implement.MAX_BRANCH_SLUG_CHARS


@pytest.mark.parametrize(
    "title",
    [
        "../../etc/passwd",
        "a..b",
        "--help",
        "-rf",
        "refs/heads/main",
        "a b\nc",
        "x@{1}",
        "end.lock",
        "a~1^2:?*[",
        "\\ back",
        "tab\there",
        "." * 50,
        "-" * 50,
    ],
)
def test_a_slug_is_always_safe_in_a_branch_name(title: str) -> None:
    slug = branch_slug(title)
    # Words joined by single hyphens, or nothing at all when the title had no usable letters.
    assert re.fullmatch(r"([a-z0-9]+(-[a-z0-9]+)*)?", slug), slug
    assert len(slug) <= implement.MAX_BRANCH_SLUG_CHARS
    # The slug takes the title as it is, whatever the header parser would have done to it.
    name = branch_name(17, _change(title=title)) if slug else ""
    if name:
        assert name.startswith("agent/issue-17-")
        if shutil.which("git"):
            subprocess.run(["git", "check-ref-format", "--branch", name], check=True)


def test_the_branch_name_keeps_the_agent_issue_start() -> None:
    assert branch_name(63, None) == "agent/issue-63"
    assert (
        branch_name(63, _change())
        == "agent/issue-63-return-passwordprotected-for-encrypted"
    )
    # A title with no usable letters leaves the plain name.
    assert branch_name(63, _change(title="日本語")) == "agent/issue-63"


def test_describe_writes_the_branch_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agent-change.md").write_text(GOOD, encoding="utf-8")
    code, out = _describe(tmp_path)
    assert code == 0
    assert (out / "branch.txt").read_text() == (
        "agent/issue-17-return-passwordprotected-for-encrypted"
    )


def test_describe_writes_the_plain_branch_when_it_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out = _describe(tmp_path)
    assert code == 0 and (out / "branch.txt").read_text() == "agent/issue-17"
