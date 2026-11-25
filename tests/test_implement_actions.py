"""The shape of the three implementation actions, read as text.

What these actions may do is decided here, in YAML nothing else executes in a test, so the
rules that keep a fooled model contained are asserted on the files themselves: who gets
the token, which tools the model has, that the model cannot push, and that the check on
what may be committed runs before anything is."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("assess", "report", "fix", "notify", "finish")
TEXT = {
    name: (ROOT / "implement" / name / "action.yml").read_text(encoding="utf-8")
    for name in NAMES
}
MODEL_STAGES = pytest.mark.parametrize("name", ["assess", "fix"])
EVERY = pytest.mark.parametrize("name", NAMES)
MODEL_ACTION = "anthropics/claude-code-action"


def _steps(name: str) -> list[str]:
    """The text of each step, in order."""
    runs = TEXT[name].split("\nruns:\n", 1)[1]
    return re.split(r"(?m)^    - ", runs)[1:]


def _step_using(name: str, action: str) -> str:
    return next(step for step in _steps(name) if f"uses: {action}" in step)


def _run_bodies(name: str) -> list[str]:
    """The script of every multi-line `run: |` block."""
    bodies = []
    lines = TEXT[name].splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s+)run: \|\s*$", line)
        if not match:
            continue
        indent = len(match.group(1))
        body = []
        for later in lines[index + 1 :]:
            if later.strip() and len(later) - len(later.lstrip()) <= indent:
                break
            body.append(later)
        bodies.append("\n".join(body))
    return bodies


# --- what runs ---


@EVERY
def test_a_third_party_action_is_pinned_to_a_commit_with_its_version(name: str) -> None:
    for line in TEXT[name].splitlines():
        match = re.search(r"uses:\s*(\S+)(.*)$", line)
        if not match or match.group(1).startswith("actions/"):
            continue
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", match.group(1)), line
        assert re.search(r"#\s*v\d+\.\d+\.\d+", match.group(2)), line


@EVERY
def test_only_this_repositorys_own_script_is_run(name: str) -> None:
    scripts = re.findall(r"python \$\{\{ github\.action_path \}\}/(\S+)", TEXT[name])
    assert scripts
    assert set(scripts) == {"../../src/implement.py"}


@EVERY
def test_no_script_has_an_expression_in_it(name: str) -> None:
    """Values reach a script through env, where a quote or a newline is only data."""
    for body in _run_bodies(name):
        assert "${{" not in body, body


@EVERY
def test_nothing_an_issue_author_wrote_is_used_in_an_expression(name: str) -> None:
    for banned in ("github.event.issue", "github.event.comment", "github.head_ref"):
        assert banned not in TEXT[name]


@EVERY
def test_no_way_around_the_write_access_check_is_switched_on(name: str) -> None:
    for banned in (
        "allowed_non_write_users",
        "allowed_bots",
        "--dangerously-skip-permissions",
        "bypassPermissions",
    ):
        assert banned not in TEXT[name]


# --- the model ---


@pytest.mark.parametrize("name", ["report", "notify", "finish"])
def test_the_stages_that_write_to_the_issue_have_no_model_and_no_token(
    name: str,
) -> None:
    assert MODEL_ACTION not in TEXT[name]
    assert "claude_code_oauth_token" not in TEXT[name]


@MODEL_STAGES
def test_the_token_reaches_the_model_step_and_nothing_else(name: str) -> None:
    assert TEXT[name].count("inputs.claude_code_oauth_token") == 1
    assert "inputs.claude_code_oauth_token" in _step_using(name, MODEL_ACTION)


@MODEL_STAGES
def test_the_model_step_gets_the_workflows_own_token_and_nothing_wider(
    name: str,
) -> None:
    step = _step_using(name, MODEL_ACTION)
    assert "github_token: ${{ inputs.github_token }}" in step


@MODEL_STAGES
def test_the_model_cannot_reach_the_network_or_git(name: str) -> None:
    step = _step_using(name, MODEL_ACTION)
    assert '--disallowedTools "WebFetch,WebSearch,Bash(git:*)"' in step


@MODEL_STAGES
def test_the_model_is_bounded(name: str) -> None:
    step = _step_using(name, MODEL_ACTION)
    assert "--max-turns ${{ inputs.max_turns }}" in step
    assert "--permission-mode acceptEdits" in step


def test_the_assessment_can_only_read() -> None:
    tools = re.search(r"--allowedTools \"([^\"]*)\"", TEXT["assess"])
    assert tools
    names = set(re.split(r"[,\s]+", tools.group(1)))
    assert {"Read", "Grep", "Glob"} <= names
    assert not names & {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def test_the_fix_can_edit_files() -> None:
    tools = re.search(r"--allowedTools \"([^\"]*)\"", TEXT["fix"])
    assert tools and {"Edit", "Write"} <= set(re.split(r"[,\s]+", tools.group(1)))


@MODEL_STAGES
def test_the_repositorys_own_commands_come_in_as_an_input(name: str) -> None:
    assert "${{ inputs.command_tools }}" in TEXT[name]
    assert "${{ inputs.repository_notes }}" in TEXT[name]


@MODEL_STAGES
def test_inputs_that_end_up_in_quotes_are_checked_first(name: str) -> None:
    check = next(body for body in _run_bodies(name) if "MAX_TURNS" in body)
    assert "[0-9]+" in check
    assert 'python "$IMPLEMENT" check-tools' in check
    text = TEXT[name]
    assert text.index("check-tools") < text.index("implement.py fetch")
    assert text.index("check-tools") < text.index(f"uses: {MODEL_ACTION}")


@MODEL_STAGES
def test_the_issue_is_fetched_before_the_model_runs(name: str) -> None:
    text = TEXT[name]
    assert text.index("implement.py fetch") < text.index(f"uses: {MODEL_ACTION}")


@MODEL_STAGES
def test_the_prompt_is_built_by_code_from_the_instruction_file(name: str) -> None:
    stage = "assess" if name == "assess" else "fix"
    assert f"implement.py prompt {stage}" in TEXT[name]
    assert "prompt: ${{ steps." in _step_using(name, MODEL_ACTION)


def test_the_verdict_schema_has_one_source() -> None:
    assert "implement.py schema" in TEXT["assess"]
    assert "--json-schema '${{ steps." in TEXT["assess"]
    assert '"enum"' not in TEXT["assess"]


def test_the_stages_pass_their_results_on() -> None:
    assert "value: ${{ steps." in TEXT["assess"].split("\nruns:\n")[0]
    assert "structured_output" in TEXT["assess"]
    assert "proceed" in TEXT["report"].split("\nruns:\n")[0]


@MODEL_STAGES
def test_how_long_a_run_took_is_reported_without_failing_it(name: str) -> None:
    step = next(s for s in _steps(name) if "execution_file" in s)
    assert "if: always() && steps.model.outcome != 'skipped'" in step and "||" in step


# --- the fix stage: what happens after the model ---


def _fix_script() -> str:
    return next(body for body in _run_bodies("fix") if "gh pr create" in body)


def test_what_may_be_committed_is_checked_before_anything_is() -> None:
    script = _fix_script()
    order = [
        script.index("check-changes"),
        script.index("git add"),
        script.index("git commit"),
        script.index("git push"),
        script.index("gh pr create"),
    ]
    assert order == sorted(order)


def test_the_changed_files_come_from_git_with_renames_split_and_new_files_included() -> (
    None
):
    script = _fix_script()
    assert "git diff --name-only --no-renames -z HEAD" in script
    assert "git ls-files --others --exclude-standard -z" in script


def test_only_the_agents_own_branch_is_pushed_and_never_by_force() -> None:
    script = _fix_script()
    pushes = [line for line in script.splitlines() if "git push" in line]
    assert len(pushes) == 1
    assert '"$branch"' in pushes[0]
    assert "--force" not in script and " -f " not in pushes[0] and "+" not in pushes[0]
    assert 'branch="$(cat "$texts/branch.txt")"' in script
    # The name is checked again before it is used, and must be this issue's.
    assert 'agent/issue-${NUMBER}-"*' in script and "[a-z0-9]+" in script


def test_the_pull_request_is_ready_for_review_unless_a_draft_is_asked_for() -> None:
    script = _fix_script()
    assert "gh pr create" in script
    # `--draft` is added only when the input says so, never unconditionally.
    assert "gh pr create --draft" not in script
    assert script.count("--draft") == 1 and 'DRAFT" == "true"' in script
    assert "--title" in script and "--body-file" in script
    assert "gh pr merge" not in script and "gh pr ready" not in script
    assert "gh pr review" not in script and "--approve" not in script


def test_the_issue_number_is_checked_before_it_names_a_branch() -> None:
    script = _fix_script()
    assert script.index("[0-9]+") < script.index("agent/issue-")


def test_the_fix_script_stops_at_the_first_error() -> None:
    assert "set -euo pipefail" in _fix_script()


# --- the daily limit ---


def test_the_limit_is_checked_after_the_author_and_before_anything_is_spent() -> None:
    text = TEXT["assess"]
    assert text.index("implement.py fetch") < text.index("implement.py check-cap")
    assert text.index("implement.py check-cap") < text.index("implement.py schema")
    assert text.index("implement.py check-cap") < text.index(f"uses: {MODEL_ACTION}")


def test_everything_after_the_limit_is_skipped_when_it_is_reached() -> None:
    for marker in (
        "implement.py schema",
        "implement.py prompt assess",
        f"uses: {MODEL_ACTION}",
    ):
        step = next(s for s in _steps("assess") if marker in s)
        assert "if: steps.cap.outputs.capped != 'true'" in step, marker


def test_the_run_size_is_still_reported_but_not_for_a_run_that_never_started() -> None:
    step = next(s for s in _steps("assess") if "execution_file" in s)
    assert "if: always() && steps.model.outcome != 'skipped'" in step


def test_the_assess_stage_hands_the_limit_result_on() -> None:
    head = TEXT["assess"].split("\nruns:\n")[0]
    assert "value: ${{ steps.cap.outputs.capped }}" in head


def test_the_limit_check_gets_its_number_from_an_input() -> None:
    step = next(s for s in _steps("assess") if "check-cap" in s)
    assert "DAILY_CAP: ${{ inputs.daily_cap }}" in step


def test_notify_says_one_of_two_fixed_things_and_nothing_else() -> None:
    assert "notify capped" in TEXT["notify"] and "notify failed" in TEXT["notify"]
    check = next(body for body in _run_bodies("notify") if "KIND" in body)
    assert "capped|failed" in check and "exit 1" in check


def test_the_draft_input_defaults_to_a_pull_request_ready_for_review() -> None:
    action = (ROOT / "implement" / "fix" / "action.yml").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^  draft:\n(.*?)(?=^  \w+:\n|^\w+:|\Z)", action)
    assert block, "the fix action has no draft input"
    assert "required: true" not in block.group(1)
    assert "default: 'false'" in block.group(1)
    assert "DRAFT: ${{ inputs.draft }}" in action


def test_finish_takes_the_url_and_the_number_and_nothing_else_from_the_workflow() -> (
    None
):
    action = TEXT["finish"]
    step = next(s for s in _steps("finish") if "implement.py finish" in s)
    assert "ISSUE_NUMBER: ${{ inputs.issue_number }}" in step
    assert "PULL_REQUEST_URL: ${{ inputs.pull_request_url }}" in step
    assert "GITHUB_TOKEN: ${{ inputs.github_token }}" in step
    assert "inputs.pull_request_url" not in action.replace(step, "")  # only in env
    assert MODEL_ACTION not in action and "actions/checkout" not in action


def test_the_fix_reports_what_happened_as_an_output() -> None:
    action = TEXT["fix"]
    assert "outcome:" in action.split("\nruns:\n", 1)[0]
    assert "value: ${{ steps.open.outputs.outcome }}" in action
    script = _fix_script()
    assert 'echo "outcome=no_change" >> "$GITHUB_OUTPUT"' in script
    assert 'echo "outcome=opened" >> "$GITHUB_OUTPUT"' in script


def test_nothing_changed_is_decided_before_anything_is_checked_or_pushed() -> None:
    """An empty change is told apart first, and ends the step cleanly; everything after it
    (the check of what may be committed, the branch, the push) only ever sees a real change."""
    script = _fix_script()
    empty = script.index("outcome=no_change")
    assert empty < script.index("check-changes")
    assert empty < script.index("git checkout -b")
    assert empty < script.index("git push")
    between = script[empty : script.index("check-changes")]
    assert "exit 0" in between


def test_finish_takes_the_outcome_and_the_url_is_only_for_an_opened_pull_request() -> (
    None
):
    action = TEXT["finish"]
    step = next(s for s in _steps("finish") if "implement.py finish" in s)
    assert "OUTCOME: ${{ inputs.outcome }}" in step
    block = re.search(r"(?ms)^  outcome:\n(.*?)(?=^  \w+:\n|^runs:)", action)
    assert block and "default: 'opened'" in block.group(1)
    url = re.search(r"(?ms)^  pull_request_url:\n(.*?)(?=^  \w+:\n|^runs:)", action)
    assert url and "required: true" not in url.group(1)
