"""The action definitions are YAML that no other test runs, so a typo in an input name
or an environment variable would silently hand the script an empty value."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# What the runner provides to every step, so action.yml does not have to set it.
PROVIDED_BY_THE_RUNNER = {"GITHUB_EVENT_PATH"}

ACTIONS = [
    pytest.param(
        ROOT / "action.yml",
        ["src/pr_review.py", "src/llm.py"],
        "src/pr_review.py",
        id="review",
    ),
    pytest.param(
        ROOT / "triage" / "action.yml",
        ["src/triage.py", "src/llm.py"],
        "src/triage.py",
        id="triage",
    ),
]


def _declared_inputs(text: str) -> set[str]:
    block = text.split("\ninputs:\n", 1)[1].split("\nruns:\n", 1)[0]
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


@pytest.mark.parametrize(("action", "sources", "script"), ACTIONS)
def test_every_input_that_is_used_is_declared(
    action: Path, sources: list[str], script: str
) -> None:
    text = action.read_text()
    used = set(re.findall(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", text))
    assert used <= _declared_inputs(text), used - _declared_inputs(text)


@pytest.mark.parametrize(("action", "sources", "script"), ACTIONS)
def test_every_declared_input_is_used(
    action: Path, sources: list[str], script: str
) -> None:
    text = action.read_text()
    used = set(re.findall(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", text))
    assert _declared_inputs(text) == used


@pytest.mark.parametrize(("action", "sources", "script"), ACTIONS)
def test_every_environment_variable_the_code_reads_is_set(
    action: Path, sources: list[str], script: str
) -> None:
    missing = _env_names_read(sources) - _env_names_set(action.read_text())
    assert not missing, missing


@pytest.mark.parametrize(("action", "sources", "script"), ACTIONS)
def test_the_action_runs_a_script_that_exists(
    action: Path, sources: list[str], script: str
) -> None:
    match = re.search(
        r"run: python \$\{\{ github\.action_path \}\}/([^\s]+)", action.read_text()
    )
    assert match, "no run step starts the script"
    assert (action.parent / match.group(1)).resolve() == (ROOT / script).resolve()


def test_the_triage_instructions_sit_beside_the_script() -> None:
    assert (ROOT / "src" / "triage_instruction.md").is_file()
