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
        ROOT / "action.yml",
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
        {"VERDICT"},
        id="implement-assess",
    ),
    pytest.param(
        ROOT / "implement" / "report" / "action.yml",
        ["src/implement.py"],
        "src/implement.py",
        {"REPO_NOTES", "ACTOR_LOGIN", "COMMAND_TOOLS", "DAILY_CAP"},
        id="implement-report",
    ),
    pytest.param(
        ROOT / "implement" / "fix" / "action.yml",
        ["src/implement.py"],
        "src/implement.py",
        {"VERDICT", "DAILY_CAP"},
        id="implement-fix",
    ),
    pytest.param(
        ROOT / "implement" / "notify" / "action.yml",
        ["src/implement.py"],
        "src/implement.py",
        {"VERDICT", "REPO_NOTES", "ACTOR_LOGIN", "COMMAND_TOOLS"},
        id="implement-notify",
    ),
]


def _declared_inputs(text: str) -> set[str]:
    block = text.split("\ninputs:\n", 1)[1]
    # An action may declare outputs between its inputs and its steps.
    block = block.split("\noutputs:\n", 1)[0].split("\nruns:\n", 1)[0]
    return set(re.findall(r"^  (\w+):\s*$", block, flags=re.MULTILINE))


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
    used = set(re.findall(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", text))
    assert used <= _declared_inputs(text), used - _declared_inputs(text)


@pytest.mark.parametrize(("action", "sources", "script", "unused_env"), ACTIONS)
def test_every_declared_input_is_used(
    action: Path, sources: list[str], script: str, unused_env: set[str]
) -> None:
    text = action.read_text()
    used = set(re.findall(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", text))
    assert _declared_inputs(text) == used


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
