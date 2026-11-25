"""What the fix stage says when it changes nothing: the model's one-paragraph reason, and the
commands it tried to run and was not allowed to.

Both come from the model, which an outsider's issue text can steer, and both end up in a comment
and in job outputs. So they are collapsed to one line (a newline in a job output would let a
value write another output), capped, and never printed raw to the log; the comment writer then
makes them safe to post."""

import json
import os
from pathlib import Path

import implement
import pytest
from implement import (
    MAX_DENIED_CHARS,
    MAX_DENIED_SHOWN,
    MAX_STOP_REASON_CHARS,
    denied_commands,
    stop_reason,
)


def _result(*denials: dict[str, object]) -> str:
    return json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "subtype": "success",
                "permission_denials": list(denials),
            },
        ]
    )


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_use_id": "t", "tool_input": {"command": command}}


# --- the reason ---


def test_the_reason_is_one_line() -> None:
    assert stop_reason("  could not\n\nwrite   a test\tfor this  ") == (
        "could not write a test for this"
    )


def test_a_newline_cannot_start_another_output() -> None:
    reason = stop_reason("because\nverdict=passed\r\noutcome=opened")
    assert "\n" not in reason and "\r" not in reason


@pytest.mark.parametrize("empty", ["", "   ", "\n\n", "​‮"])
def test_nothing_visible_is_no_reason(empty: str) -> None:
    assert stop_reason(empty) == ""


def test_the_reason_is_cut_to_a_bound() -> None:
    assert len(stop_reason("word " * 5000)) <= MAX_STOP_REASON_CHARS


def test_the_cap_leaves_room_for_a_real_explanation() -> None:
    assert MAX_STOP_REASON_CHARS >= 1000


def test_a_reason_that_fits_is_left_exactly_as_it_is() -> None:
    text = "x" * MAX_STOP_REASON_CHARS
    assert stop_reason(text) == text
    assert "…" not in stop_reason("A short reason.")


def test_a_cut_reason_ends_with_an_ellipsis_at_a_word() -> None:
    cut = stop_reason("word " * 5000)
    assert cut.endswith("word…") and len(cut) <= MAX_STOP_REASON_CHARS
    # One character over the cap is cut too, and says so.
    assert stop_reason("x" * (MAX_STOP_REASON_CHARS + 1)).endswith("…")


def test_one_very_long_word_is_cut_hard_with_an_ellipsis() -> None:
    cut = stop_reason("y" * 5000)
    assert len(cut) == MAX_STOP_REASON_CHARS and cut.endswith("y…")


def test_cutting_twice_changes_nothing_more() -> None:
    once = stop_reason("word " * 5000)
    assert stop_reason(once) == once


def test_invisible_characters_are_removed() -> None:
    assert stop_reason("a‮b​c") == "abc"


# --- the denied commands ---


def test_the_denied_commands_are_listed_with_their_tool() -> None:
    raw = _result(_bash("uv run pytest tests/x.py"), _bash("pip install foo"))
    assert denied_commands(raw) == [
        "Bash: uv run pytest tests/x.py",
        "Bash: pip install foo",
    ]


def test_a_denied_tool_without_a_command_is_named() -> None:
    raw = _result(
        {"tool_name": "WebFetch", "tool_input": {"url": "https://evil.example"}}
    )
    assert denied_commands(raw) == ["WebFetch"]


def test_a_repeated_denial_is_listed_once() -> None:
    raw = _result(
        _bash("uv run pytest"), _bash("uv run pytest"), _bash("uv run pytest")
    )
    assert denied_commands(raw) == ["Bash: uv run pytest"]


def test_only_a_few_are_shown() -> None:
    raw = _result(*[_bash(f"cmd{n}") for n in range(20)])
    assert len(denied_commands(raw)) == MAX_DENIED_SHOWN


def test_a_long_command_is_cut() -> None:
    (entry,) = denied_commands(_result(_bash("x" * 5000)))
    assert len(entry) <= MAX_DENIED_CHARS


def test_a_cut_command_ends_with_an_ellipsis_and_a_short_one_does_not() -> None:
    (cut,) = denied_commands(_result(_bash("x" * 5000)))
    assert cut.endswith("…") and len(cut) == MAX_DENIED_CHARS
    (short,) = denied_commands(_result(_bash("uv run pytest")))
    assert short == "Bash: uv run pytest"


@pytest.mark.parametrize(
    "command",
    [
        "echo hi\n::error::INJECTED",
        "a`b`c",
        "x‮y",
        "line one\nline two",
    ],
)
def test_a_denied_command_is_one_line_with_no_backtick(command: str) -> None:
    (entry,) = denied_commands(_result(_bash(command)))
    assert "\n" not in entry and "`" not in entry and "‮" not in entry


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "null",
        "[]",
        "{}",
        "[1, 2]",
        '[{"type": "result"}]',
        '[{"permission_denials": "x"}]',
        '[{"permission_denials": [1, "a", null]}]',
        '[{"permission_denials": [{"tool_name": 5}]}]',
    ],
)
def test_anything_unexpected_gives_no_denials(raw: str) -> None:
    assert denied_commands(raw) == []


def test_lines_of_json_work_too() -> None:
    raw = "\n".join(
        json.dumps(m)
        for m in (
            {"type": "system"},
            {"type": "result", "permission_denials": [_bash("uv run pytest")]},
        )
    )
    assert denied_commands(raw) == ["Bash: uv run pytest"]


# --- the command the fix action runs ---


def _run_stop_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "github_output"
    out.write_text("")
    env = {"GITHUB_OUTPUT": str(out), "EXECUTION_FILE": str(tmp_path / "exec.json")}
    from unittest.mock import patch

    with patch.dict(os.environ, env, clear=True):
        implement.main(["stop-report"])
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_the_report_writes_the_reason_and_the_denials_as_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".agent-stop.md").write_text(
        "It needs a decision about TLS defaults.\n"
    )
    (tmp_path / "exec.json").write_text(_result(_bash("uv run pytest")))
    outputs = _run_stop_report(tmp_path, monkeypatch)
    assert outputs["reason"] == "It needs a decision about TLS defaults."
    assert json.loads(outputs["denied"]) == ["Bash: uv run pytest"]


def test_each_output_is_exactly_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".agent-stop.md").write_text("why\noutcome=opened\nreason=other\n")
    (tmp_path / "exec.json").write_text(_result(_bash("a\nsecret=1")))
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "github_output"
    out.write_text("")
    from unittest.mock import patch

    with patch.dict(
        os.environ,
        {"GITHUB_OUTPUT": str(out), "EXECUTION_FILE": str(tmp_path / "exec.json")},
        clear=True,
    ):
        implement.main(["stop-report"])
    lines = out.read_text().splitlines()
    assert len(lines) == 2
    assert [line.split("=", 1)[0] for line in lines] == ["reason", "denied"]


def test_no_files_give_empty_outputs_and_no_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = _run_stop_report(tmp_path, monkeypatch)
    assert outputs == {"reason": "", "denied": "[]"}


def test_a_broken_execution_file_does_not_fail_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "exec.json").write_text("{{{ not json")
    assert _run_stop_report(tmp_path, monkeypatch)["denied"] == "[]"


def _starts_a_command(text: str) -> bool:
    """GitHub reads a log line as a workflow command only if it starts with `::`."""
    return any(line.lstrip().startswith("::") for line in text.splitlines())


def test_the_log_never_carries_a_workflow_command_from_the_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".agent-stop.md").write_text(
        "::error::INJECTED\n::set-output name=x::y"
    )
    (tmp_path / "exec.json").write_text(
        _result(_bash("::add-mask::value"), _bash("a\n::error::b"))
    )
    _run_stop_report(tmp_path, monkeypatch)
    seen = capsys.readouterr()
    assert not _starts_a_command(seen.out + seen.err)
    # The reason is not printed at all, only how long it is.
    assert "INJECTED" not in seen.out + seen.err


def test_the_denials_command_prints_them_inertly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "exec.json").write_text(
        _result(_bash("uv run pytest"), _bash("::error::x"))
    )
    monkeypatch.chdir(tmp_path)
    from unittest.mock import patch

    with patch.dict(
        os.environ, {"EXECUTION_FILE": str(tmp_path / "exec.json")}, clear=True
    ):
        implement.main(["denials"])
    out = capsys.readouterr().out
    assert "uv run pytest" in out and not _starts_a_command(out)


def test_one_long_word_after_a_short_prefix_is_not_lost() -> None:
    """Backing up to the only space would leave just the prefix."""
    (cut,) = denied_commands(_result(_bash("x" * 5000)))
    assert cut.startswith("Bash: xxxx") and cut.endswith("x…")
    reason = stop_reason("Because " + "y" * 5000)
    assert reason.startswith("Because yyyy") and len(reason) == MAX_STOP_REASON_CHARS


def _one_space_at(position: int) -> str:
    """Text whose only space is at `position`, followed by a word far longer than the cap."""
    return "x" * position + " " + "y" * 5000


@pytest.mark.parametrize("position", [800, 801, 850, 990, 998])
def test_a_space_in_the_last_fifth_is_backed_up_to(position: int) -> None:
    """800 is the threshold itself (a fifth of 1000 from the end), and counts."""
    assert stop_reason(_one_space_at(position)) == "x" * position + "…"


@pytest.mark.parametrize("position", [5, 700, 799])
def test_a_space_before_the_last_fifth_is_not_backed_up_to(position: int) -> None:
    cut = stop_reason(_one_space_at(position))
    assert len(cut) == MAX_STOP_REASON_CHARS
    assert cut.startswith("x" * position + " yyy") and cut.endswith("y…")


def test_a_space_exactly_where_the_cut_falls_needs_no_backing_up() -> None:
    """The first character left out is the space, so the cut is already between words."""
    assert stop_reason(_one_space_at(MAX_STOP_REASON_CHARS - 1)) == (
        "x" * (MAX_STOP_REASON_CHARS - 1) + "…"
    )
