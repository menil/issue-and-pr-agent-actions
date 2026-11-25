"""The small workflow a repository copies, and how it fits the reusable workflow it calls.

It holds the event it listens to, the one job that calls the shared workflow, the permissions
that job grants and the settings that are the repository's own. Everything that decides who
may start a run, and what each job may touch, is in the shared workflow
(tests/test_implement_template.py); here the two are checked against each other."""

import re
from pathlib import Path

import implement
import pytest

ROOT = Path(__file__).resolve().parent.parent
CALLER = (ROOT / "implement" / "example-workflow.yml").read_text(encoding="utf-8")
SHARED = (ROOT / ".github" / "workflows" / "implement.yml").read_text(encoding="utf-8")


def _block(text: str, key: str, indent: int) -> str:
    """The lines under `key:` at this indent, up to the next key at the same indent."""
    pad = " " * indent
    match = re.search(rf"(?ms)^{pad}{key}:[^\n]*\n(.*?)(?=^{pad}\S|\Z)", text)
    assert match, f"no {key}: at indent {indent}"
    return match.group(1)


def _passed(section: str) -> set[str]:
    return set(re.findall(r"(?m)^      (\w+):", _block(CALLER, section, 4)))


def _declared(section: str) -> dict[str, str]:
    """The inputs (or secrets) the shared workflow declares, with their body text."""
    block = _block(_block(SHARED, "on", 0), "workflow_call", 2)
    body = _block(block, section, 4)
    parts = re.split(r"(?m)^      (?=\w+:\n)", body)
    return {
        match.group(1): part for part in parts if (match := re.match(r"(\w+):\n", part))
    }


# --- what it listens to ---


def test_one_run_per_issue_and_none_is_cancelled_halfway() -> None:
    assert "group: implement-issue-${{ github.event.issue.number }}" in CALLER
    assert "cancel-in-progress: false" in CALLER


def test_only_new_and_labelled_issues_are_events_it_listens_to() -> None:
    assert re.search(r"(?m)^on:\n  issues:\n    types: \[opened, labeled\]\n", CALLER)
    code = re.sub(r"#.*", "", CALLER)
    events = _block(code, "on", 0)
    for banned in (
        "pull_request",
        "issue_comment",
        "workflow_run",
        "workflow_dispatch",
        "schedule",
    ):
        assert banned not in events
    assert "pull_request_target" not in code


def test_nothing_the_issue_author_wrote_is_used_in_an_expression() -> None:
    for banned in ("issue.title", "issue.body", "github.head_ref", "comment.body"):
        assert banned not in CALLER


# --- what it grants ---


def test_nothing_is_granted_by_default() -> None:
    assert re.search(r"(?m)^permissions: \{\}\s*$", CALLER)


def _grants(text: str) -> dict[str, str]:
    return dict(re.findall(r"(?m)^      ([\w-]+): (read|write)\s*$", text))


def test_the_call_grants_the_most_the_shared_jobs_ask_for_between_them() -> None:
    """A called job can only narrow what its caller grants, so the call must grant at least
    the union of what the jobs ask for, and no more: a permission nobody uses would be one
    more for a compromised step to hold."""
    granted = _grants(
        _block(_block(CALLER, "jobs", 0), "implement", 2).split("uses:")[0]
    )
    asked: dict[str, str] = {}
    jobs = _block(SHARED, "jobs", 0)
    for name in ("assess", "report", "fix", "review", "finish", "notify"):
        body = _block(jobs, name, 2)
        for scope, level in re.findall(
            r"(?m)^      ([\w-]+): (read|write)\s*$",
            _block(body, "permissions", 4),
        ):
            if asked.get(scope) != "write":
                asked[scope] = level
    assert granted == asked


def test_no_permission_can_touch_workflows_or_mint_tokens() -> None:
    for banned in (
        "workflows:",
        "id-token:",
        "actions:",
        "packages:",
        "write-all",
        "read-all",
    ):
        assert banned not in CALLER


# --- the call itself ---


def test_it_calls_the_shared_workflow_of_this_repository() -> None:
    assert re.search(
        r"(?m)^    uses: menil/issue-and-pr-agent-actions/\.github/workflows/implement\.yml@\S+$",
        CALLER,
    )


def test_it_passes_only_inputs_the_shared_workflow_declares() -> None:
    declared = _declared("inputs")
    assert _passed("with") <= set(declared), _passed("with") - set(declared)


def test_it_passes_every_input_the_shared_workflow_requires() -> None:
    required = {
        name for name, text in _declared("inputs").items() if "required: true" in text
    }
    assert required <= _passed("with"), required - _passed("with")


def test_it_passes_only_secrets_the_shared_workflow_declares() -> None:
    declared = _declared("secrets")
    assert _passed("secrets") <= set(declared), _passed("secrets") - set(declared)


def test_it_passes_every_secret_the_shared_workflow_requires() -> None:
    required = {
        name for name, text in _declared("secrets").items() if "required: true" in text
    }
    assert required <= _passed("secrets"), required - _passed("secrets")


def test_the_two_repository_variables_are_passed_as_plain_inputs() -> None:
    assert "agent_auto: ${{ vars.AGENT_AUTO }}" in CALLER
    assert "daily_cap: ${{ vars.AGENT_DAILY_CAP }}" in CALLER


def test_only_the_claude_token_and_the_optional_fallback_key_are_handed_over() -> None:
    assert set(re.findall(r"secrets\.(\w+)", CALLER)) == {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENROUTER_API_KEY",
    }
    assert "inherit" not in CALLER  # never every secret of the repository


def test_the_commands_in_the_example_pass_the_check_they_will_meet() -> None:
    """The example is what people copy; it must not be refused by the first thing it meets."""
    tools = re.findall(r"_command_tools: '([^']*)'", CALLER)
    assert len(tools) == 2
    for value in tools:
        assert implement.check_command_tools(value) == [], value


@pytest.mark.parametrize("name", ["assess_command_tools", "fix_command_tools"])
def test_the_commands_the_caller_gives_are_what_the_model_steps_receive(
    name: str,
) -> None:
    assert f"command_tools: ${{{{ inputs.{name} }}}}" in SHARED


def test_the_optional_settings_it_mentions_all_exist() -> None:
    for name in re.findall(r"(?m)^      #   (\w+):", CALLER):
        assert name in _declared("inputs"), name
