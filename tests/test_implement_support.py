"""The schema, the prompt and the check on what may be committed: the parts of the
implementation actions that would otherwise be shell inside YAML, where nothing tests them."""

import io
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import implement
import pytest
from implement import build_prompt, check_changes, verdict_schema

# --- the schema ---


def test_the_schema_is_built_from_the_contract() -> None:
    schema = verdict_schema()
    props = schema["properties"]
    assert props["valid"]["enum"] == list(implement.VALID_VALUES)
    assert props["reproduced"]["enum"] == list(implement.REPRODUCED_VALUES)
    assert props["effort"]["enum"] == list(implement.EFFORT_VALUES)
    assert props["evidence"]["maxLength"] == implement.MAX_EVIDENCE_CHARS
    assert props["summary"]["maxLength"] == implement.MAX_SUMMARY_CHARS


def test_the_schema_requires_every_field_and_allows_no_others() -> None:
    schema = verdict_schema()
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == sorted(schema["properties"])
    assert set(schema["properties"]) == {
        "valid",
        "reproduced",
        "effort",
        "evidence",
        "summary",
    }


def test_the_schema_command_prints_one_line_with_no_single_quote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It goes into claude_args inside single quotes, so neither may appear in it."""
    out = tmp_path / "out"
    with patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}, clear=True):
        implement.main(["schema"])
    line = out.read_text().strip()
    assert line.startswith("schema=") and "\n" not in line
    text = line.removeprefix("schema=")
    assert "'" not in text
    assert json.loads(text) == verdict_schema()


def test_a_real_verdict_fits_the_schema_the_model_is_given() -> None:
    """The model is told this shape; the code must accept what the shape allows."""
    example = {
        "valid": "yes",
        "reproduced": "yes",
        "effort": "small",
        "evidence": "ran it",
        "summary": "bug",
    }
    schema = verdict_schema()
    for field, value in example.items():
        spec = schema["properties"][field]
        assert value in spec.get("enum", [value])
    implement.parse_verdict(json.dumps(example))


# --- the prompt ---


@pytest.mark.parametrize("stage", ["assess", "fix"])
def test_the_prompt_is_the_instruction_file_then_the_notes(stage: str) -> None:
    prompt = build_prompt(stage, "Run the tests with `cargo test`.")
    instruction = (
        Path(implement.__file__).resolve().parent.parent
        / "implement"
        / f"{stage}_instruction.md"
    ).read_text(encoding="utf-8")
    assert prompt.startswith(instruction.rstrip())
    assert prompt.rstrip().endswith("Run the tests with `cargo test`.")
    assert "## Repository notes" in prompt


def test_the_notes_are_cut_and_cleaned() -> None:
    prompt = build_prompt("fix", "n" * 20000 + "​\U000e0041")
    notes = prompt.split("## Repository notes", 1)[1]
    assert len(notes) < implement.MAX_NOTES_CHARS + 100
    assert "​" not in prompt and "\U000e0041" not in prompt


def test_missing_notes_are_said_to_be_missing() -> None:
    assert "no notes" in build_prompt("assess", "  ").lower()


def test_an_unknown_stage_is_refused() -> None:
    with pytest.raises(ValueError):
        build_prompt("deploy", "")


def test_the_prompt_command_writes_a_multiline_output_that_cannot_be_cut_short(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    notes = "line one\nghadelimiter_x\nline three"
    with patch.dict(
        os.environ, {"GITHUB_OUTPUT": str(out), "REPO_NOTES": notes}, clear=True
    ):
        implement.main(["prompt", "assess"])
    text = out.read_text()
    match = re.match(r"prompt<<(\S+)\n", text)
    assert match
    delimiter = match.group(1)
    body = text.removeprefix(match.group(0))
    assert body.endswith(f"\n{delimiter}\n")
    assert delimiter not in body.removesuffix(f"\n{delimiter}\n")
    assert "ghadelimiter_x" in body  # the notes are kept whole


def test_the_prompt_command_needs_a_stage() -> None:
    with pytest.raises(SystemExit) as excinfo:
        implement.main(["prompt"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        implement.main(["prompt", "deploy"])
    assert excinfo.value.code == 2


# --- what may be committed ---


@pytest.mark.parametrize(
    "paths",
    [
        ["slug.py"],
        ["slug.py", "tests/test_slug.py"],
        ["src/lib.rs", "tests/regression.rs"],
        ["docs/github-notes.md"],
        ["a/.github/x"],  # only a top-level .github is the workflows directory
        ["README.md"],
    ],
)
def test_ordinary_changes_pass(paths: list[str]) -> None:
    assert check_changes(paths) == []


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/validate.yml",
        ".github/CODEOWNERS",
        ".GitHub/workflows/x.yml",
        "./.github/workflows/x.yml",
        "src/../.github/workflows/x.yml",
        ".github",
        ".git/hooks/pre-commit",
        ".git/config",
        ".githooks/pre-commit",
        ".gitmodules",
        ".gitattributes",
        ".claude/settings.json",
        ".codex/config.toml",
        ".agents/skills/x.md",
        ".agent-issue.md",
        "sub/.agent-issue.md",
        "Cargo.lock",
        "sub/package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "flake.lock",
        "go.sum",
        "Gemfile.lock",
        "docs/CODEOWNERS",
        "/etc/passwd",
        "../outside.txt",
        "a/../../outside.txt",
        "a\nb.py",
        "tab\there.py",
        "esc\x1b[31m.py",
        "bell\x07.py",
    ],
)
def test_forbidden_paths_are_refused(path: str) -> None:
    assert check_changes(["slug.py", path]) != []


def test_one_bad_path_among_many_is_enough() -> None:
    assert check_changes(["a.py", "b.py", ".github/workflows/x.yml"])


def test_a_path_is_judged_after_it_is_cleaned_up() -> None:
    assert check_changes(["tests//../.github/x"]) != []
    assert check_changes(["tests/./x.py"]) == []


def test_too_many_files_is_itself_suspicious() -> None:
    assert check_changes([f"f{n}.py" for n in range(implement.MAX_CHANGED_FILES)]) == []
    assert check_changes([f"f{n}.py" for n in range(implement.MAX_CHANGED_FILES + 1)])


def test_an_empty_path_is_refused() -> None:
    assert check_changes([""]) != []


def _run_check(raw: bytes) -> tuple[int, str]:
    class Stdin:
        buffer = io.BytesIO(raw)

    out: list[str] = []
    with (
        patch("sys.stdin", Stdin()),
        patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))),
    ):
        try:
            implement.main(["check-changes"])
            return 0, "\n".join(out)
        except SystemExit as exit_:
            return int(exit_.code or 0), "\n".join(out)


def test_the_command_reads_nul_separated_paths_and_passes_good_ones() -> None:
    code, _ = _run_check(b"slug.py\0tests/test_slug.py\0")
    assert code == 0


def test_the_command_fails_on_a_forbidden_path() -> None:
    code, out = _run_check(b"slug.py\0.github/workflows/validate.yml\0")
    assert code == 1 and "::error::" in out


def test_the_command_fails_when_nothing_changed() -> None:
    assert _run_check(b"")[0] == 1
    assert _run_check(b"\0")[0] == 1


def test_a_path_with_a_newline_or_odd_bytes_cannot_forge_a_log_line() -> None:
    code, out = _run_check(b".github/x\n::add-mask::y\0\xff\xfe\0")
    assert code == 1
    assert not [
        line
        for line in out.splitlines()
        if line.startswith("::") and not line.startswith("::error::")
    ]


def test_a_path_with_a_control_character_is_refused_even_if_it_looks_harmless() -> None:
    assert _run_check(b"ok.py\nmore\0")[0] == 1
