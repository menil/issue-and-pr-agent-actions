"""The action definitions are YAML that no other test runs, so a typo in an input name
or an environment variable would silently hand the script an empty value."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# What the runner provides to every step, so action.yml does not have to set it.
PROVIDED_BY_THE_RUNNER = {
    "GITHUB_EVENT_PATH",
    "GITHUB_OUTPUT",
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ID",
    "GITHUB_SERVER_URL",
}

# The last value is the environment variables the code reads that this action's stage
# does not need: implement.py serves three actions, and each uses some of what it reads.
ACTIONS = [
    pytest.param(
        ROOT / "review" / "action.yml",
        ["src/pr_review.py", "src/llm.py"],
        "src/pr_review.py",
        set(),
        id="review",
    ),
    pytest.param(
        ROOT / "triage" / "action.yml",
        ["src/triage.py", "src/llm.py"],
        "src/triage.py",
        set(),
        id="triage",
    ),
    pytest.param(
        ROOT / "implement" / "assess" / "action.yml",
        ["src/implement.py"],
        "src/implement.py",
        {"VERDICT", "PULL_REQUEST_URL", "OUTCOME"},
        id="implement-assess",
    ),
    pytest.param(
        ROOT / "implement" / "report" / "action.yml",
        ["src/implement.py"],
        "src/implement.py",
        {
            "REPO_NOTES",
            "ACTOR_LOGIN",
            "COMMAND_TOOLS",
            "DAILY_CAP",
            "PULL_REQUEST_URL",
            "OUTCOME",
        },
        id="implement-report",
    ),
    pytest.param(
        ROOT / "implement" / "fix" / "action.yml",
        ["src/implement.py"],
        "src/implement.py",
        {"VERDICT", "DAILY_CAP", "PULL_REQUEST_URL", "OUTCOME"},
        id="implement-fix",
    ),
    pytest.param(
        ROOT / "implement" / "notify" / "action.yml",
        ["src/implement.py"],
        "src/implement.py",
        {
            "VERDICT",
            "REPO_NOTES",
            "ACTOR_LOGIN",
            "COMMAND_TOOLS",
            "PULL_REQUEST_URL",
            "OUTCOME",
        },
        id="implement-notify",
    ),
    pytest.param(
        ROOT / "implement" / "finish" / "action.yml",
        ["src/implement.py"],
        "src/implement.py",
        {"VERDICT", "REPO_NOTES", "ACTOR_LOGIN", "COMMAND_TOOLS", "DAILY_CAP"},
        id="implement-finish",
    ),
]


def _declared_inputs(text: str) -> set[str]:
    block = text.split("\ninputs:\n", 1)[1]
    # An action may declare outputs between its inputs and its steps.
    block = block.split("\noutputs:\n", 1)[0].split("\nruns:\n", 1)[0]
    return set(re.findall(r"^  (\w+):\s*$", block, flags=re.MULTILINE))


def _inputs_used(text: str) -> set[str]:
    """The inputs read in `${{ ... }}` expressions, wherever in the expression they are
    (`inputs.a || github.x` counts). A comment or a description that merely mentions
    `inputs.x` is not a use, so it can neither invent one nor hide an unused input."""
    return {
        name
        for expression in re.findall(r"\$\{\{(.*?)\}\}", text, flags=re.DOTALL)
        for name in re.findall(r"\binputs\.(\w+)", expression)
    }


def test_only_an_expression_counts_as_using_an_input() -> None:
    text = (
        "# see inputs.in_comment\n"
        "description: 'set inputs.in_description'\n"
        "a: ${{ inputs.plain }}\n"
        "b: ${{ inputs.first || github.event.x || inputs.second }}\n"
        "c: ${{ github.event.inputs_not.here }}\n"
    )
    assert _inputs_used(text) == {"plain", "first", "second"}


def _env_names_set(text: str) -> set[str]:
    block = text.split("\nruns:\n", 1)[1]
    return set(re.findall(r"^ {8}([A-Z][A-Z0-9_]*):", block, flags=re.MULTILINE))


def _env_names_read(sources: list[str]) -> set[str]:
    names: set[str] = set()
    for source in sources:
        names |= set(
            re.findall(
                r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]*)"', (ROOT / source).read_text()
            )
        )
    return names - PROVIDED_BY_THE_RUNNER


@pytest.mark.parametrize(("action", "sources", "script", "unused_env"), ACTIONS)
def test_every_input_that_is_used_is_declared(
    action: Path, sources: list[str], script: str, unused_env: set[str]
) -> None:
    text = action.read_text()
    used = _inputs_used(text)
    assert used <= _declared_inputs(text), used - _declared_inputs(text)


@pytest.mark.parametrize(("action", "sources", "script", "unused_env"), ACTIONS)
def test_every_declared_input_is_used(
    action: Path, sources: list[str], script: str, unused_env: set[str]
) -> None:
    text = action.read_text()
    assert _declared_inputs(text) == _inputs_used(text)


@pytest.mark.parametrize(("action", "sources", "script", "unused_env"), ACTIONS)
def test_every_environment_variable_the_code_reads_is_set(
    action: Path, sources: list[str], script: str, unused_env: set[str]
) -> None:
    missing = _env_names_read(sources) - _env_names_set(action.read_text()) - unused_env
    assert not missing, missing


@pytest.mark.parametrize(("action", "sources", "script", "unused_env"), ACTIONS)
def test_the_action_runs_a_script_that_exists(
    action: Path, sources: list[str], script: str, unused_env: set[str]
) -> None:
    match = re.search(
        r"run: python \$\{\{ github\.action_path \}\}/([^\s]+)", action.read_text()
    )
    assert match, "no run step starts the script"
    assert (action.parent / match.group(1)).resolve() == (ROOT / script).resolve()


def test_the_triage_instructions_sit_beside_the_script() -> None:
    assert (ROOT / "src" / "triage_instruction.md").is_file()


# --- reviewing a pull request that did not start the workflow ---

REVIEW = (ROOT / "review" / "action.yml").read_text(encoding="utf-8")


def test_the_review_can_be_pointed_at_a_pull_request_by_number() -> None:
    assert "pull_request_number" in _declared_inputs(REVIEW)
    block = re.search(r"(?ms)^  pull_request_number:\n(.*?)(?=^  \w+:\n|\Z)", REVIEW)
    assert block and "required: true" not in block.group(1)
    assert "default: ''" in block.group(1)


def test_both_steps_that_need_the_number_take_it_from_the_input_before_the_event() -> (
    None
):
    expression = "${{ inputs.pull_request_number || github.event.pull_request.number }}"
    assert REVIEW.count(f"PR_NUMBER: {expression}") == 2
    assert "PR_NUMBER: ${{ github.event.pull_request.number }}" not in REVIEW


def _fetch_step() -> str:
    match = re.search(
        r"- name: Fetch PR Diff\n.*?run: \|\n(.*?)(?=\n    - name:)", REVIEW, re.S
    )
    assert match
    return "\n".join(line[8:] for line in match.group(1).splitlines())


@pytest.mark.parametrize("number", ["7", "123"])
def test_the_diff_is_fetched_for_a_numeric_pull_request(
    tmp_path: Path, number: str
) -> None:
    import subprocess

    fake = tmp_path / "gh"
    fake.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/args"\necho diff\n')
    fake.chmod(0o755)
    done = subprocess.run(
        ["bash", "-c", _fetch_step()],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "PR_NUMBER": number,
            "REPOSITORY": "o/r",
            "GH_TOKEN": "t",
        },
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "args").read_text().split() == [
        "pr",
        "diff",
        number,
        "--repo",
        "o/r",
    ]


@pytest.mark.parametrize("number", ["", "x", "--help", "1 2", "12; id", "-3"])
def test_the_diff_is_not_fetched_for_anything_but_a_number(
    tmp_path: Path, number: str
) -> None:
    import subprocess

    fake = tmp_path / "gh"
    fake.write_text(f'#!/bin/sh\ntouch "{tmp_path}/called"\n')
    fake.chmod(0o755)
    done = subprocess.run(
        ["bash", "-c", _fetch_step()],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "PR_NUMBER": number,
            "REPOSITORY": "o/r",
            "GH_TOKEN": "t",
        },
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert not (tmp_path / "called").exists()
