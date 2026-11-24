"""The template workflow decides who may spend a run and what each job may touch.

Its `if:` guards are run through an evaluator against a table of events, so they are
tested by what they decide; its permissions and wiring are read as text."""

import re
from pathlib import Path
from typing import Any

import pytest
from gha_expr import evaluate
from test_action_yml import _declared_inputs

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "implement" / "example-workflow.yml").read_text(encoding="utf-8")
JOBS = ("assess", "report", "fix", "notify")


def _job(name: str) -> str:
    match = re.search(rf"(?m)^  {name}:\n(.*?)(?=^  \w+:\n|\Z)", TEXT, flags=re.S)
    assert match, name
    return match.group(1)


def _condition(name: str) -> str:
    """The job's `if:` as one line, with any folding undone."""
    lines = _job(name).splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^    if: (.*)$", line)
        if not match:
            continue
        first = match.group(1).strip()
        if first not in (">-", ">", "|"):
            return first
        body = []
        for later in lines[index + 1 :]:
            if later.strip() and len(later) - len(later.lstrip()) <= 4:
                break
            body.append(later.strip())
        return " ".join(part for part in body if part and not part.startswith("#"))
    raise AssertionError(f"{name} has no if")


def _permissions(block: str) -> dict[str, str]:
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^    permissions:\s*$", line):
            found = {}
            for later in lines[index + 1 :]:
                match = re.match(r"^      ([\w-]+): (\w+)\s*$", later)
                if not match:
                    break
                found[match.group(1)] = match.group(2)
            return found
    raise AssertionError("no permissions")


def _event(
    action: str = "opened",
    *,
    association: str = "OWNER",
    user_type: str = "User",
    labels: tuple[str, ...] = (),
    label: str = "",
    sender: str = "maintainer",
) -> dict[str, Any]:
    return {
        "action": action,
        "sender": {"login": sender},
        "label": {"name": label},
        "issue": {
            "number": 17,
            "author_association": association,
            "user": {"type": user_type, "login": "someone"},
            "labels": [{"name": name} for name in labels],
        },
    }


def _runs(event: dict[str, Any], agent_auto: str | None = "true") -> bool:
    context = {
        "github": {"event": event},
        "vars": {} if agent_auto is None else {"AGENT_AUTO": agent_auto},
    }
    return bool(evaluate(_condition("assess"), context))


# --- who may start a run ---


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_a_trusted_authors_new_issue_runs_when_auto_is_on(association: str) -> None:
    assert _runs(_event(association=association))


@pytest.mark.parametrize(
    "association",
    ["CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", "NONE", ""],
)
def test_anyone_elses_new_issue_never_runs_on_its_own(association: str) -> None:
    assert not _runs(_event(association=association))


@pytest.mark.parametrize("auto", [None, "", "false", "no", "1"])
def test_a_new_issue_does_not_run_unless_auto_is_exactly_true(auto: str | None) -> None:
    assert not _runs(_event(), agent_auto=auto)


def test_a_maintainers_label_starts_a_run_even_without_auto_and_for_a_stranger() -> (
    None
):
    event = _event("labeled", association="NONE", label="ready-to-implement")
    assert _runs(event, agent_auto=None)
    assert _runs(event, agent_auto="true")


@pytest.mark.parametrize(
    "label", ["bug", "enhancement", "triaged", "ready", "", "repro"]
)
def test_any_other_label_starts_nothing(label: str) -> None:
    assert not _runs(_event("labeled", label=label))


@pytest.mark.parametrize(
    "action", ["edited", "reopened", "closed", "assigned", "unlabeled", ""]
)
def test_no_other_event_starts_anything(action: str) -> None:
    assert not _runs(_event(action, label="ready-to-implement"))


@pytest.mark.parametrize("action", ["opened", "labeled"])
def test_a_bot_never_starts_a_run(action: str) -> None:
    event = _event(action, user_type="Bot", label="ready-to-implement")
    assert not _runs(event)


@pytest.mark.parametrize("action", ["opened", "labeled"])
def test_no_agent_stops_everything(action: str) -> None:
    event = _event(action, labels=("no-agent",), label="ready-to-implement")
    assert not _runs(event)


@pytest.mark.parametrize(
    "label", ["repro:confirmed", "repro:failed", "repro:not-applicable"]
)
@pytest.mark.parametrize("action", ["opened", "labeled"])
def test_an_issue_that_was_assessed_is_not_assessed_again(
    action: str, label: str
) -> None:
    event = _event(action, labels=(label,), label="ready-to-implement")
    assert not _runs(event)


def test_other_labels_on_the_issue_do_not_get_in_the_way() -> None:
    assert _runs(_event(labels=("bug", "triaged", "effort:small")))


def test_the_guard_is_case_insensitive_about_labels_like_github_is() -> None:
    assert not _runs(_event(labels=("No-Agent",)))


# --- what happens next ---


@pytest.mark.parametrize(
    ("proceed", "runs"), [("true", True), ("false", False), ("", False), (None, False)]
)
def test_the_fix_runs_only_if_the_report_said_proceed(
    proceed: str | None, runs: bool
) -> None:
    outputs = {} if proceed is None else {"proceed": proceed}
    context = {"needs": {"report": {"outputs": outputs}}}
    assert bool(evaluate(_condition("fix"), context)) is runs


@pytest.mark.parametrize(
    ("action", "login", "expected"),
    [("labeled", "maint", "maint"), ("opened", "author", "")],
)
def test_the_labeller_is_passed_only_when_a_label_started_the_run(
    action: str, login: str, expected: str
) -> None:
    expressions = re.findall(r"actor_login: \$\{\{ (.*?) \}\}", TEXT)
    assert len(expressions) == 2  # the assessment and the fix both pass it
    context = {"github": {"event": _event(action, sender=login)}}
    for expression in expressions:
        assert evaluate(expression, context) == expected


# --- what each job may touch ---


def test_nothing_is_granted_by_default() -> None:
    assert re.search(r"(?m)^permissions: \{\}\s*$", TEXT)


def test_each_job_asks_for_exactly_what_it_needs() -> None:
    assert _permissions(_job("assess")) == {"contents": "read", "issues": "read"}
    assert _permissions(_job("report")) == {"issues": "write"}
    assert _permissions(_job("notify")) == {"issues": "write"}
    assert _permissions(_job("fix")) == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "read",
    }


def test_no_job_can_touch_workflows_or_mint_tokens() -> None:
    for banned in (
        "workflows:",
        "id-token:",
        "actions:",
        "packages:",
        "write-all",
        "read-all",
    ):
        assert banned not in TEXT


def test_only_the_jobs_that_run_the_model_see_the_claude_token() -> None:
    assert set(re.findall(r"secrets\.(\w+)", TEXT)) == {"CLAUDE_CODE_OAUTH_TOKEN"}
    assert "secrets." in _job("assess") and "secrets." in _job("fix")
    assert "secrets." not in _job("report")
    assert "secrets." not in _job("notify")


@pytest.mark.parametrize("name", ["report", "notify"])
def test_the_jobs_that_only_write_a_comment_check_nothing_out(name: str) -> None:
    assert "checkout" not in _job(name)


@pytest.mark.parametrize("name", ["assess", "fix"])
def test_a_checkout_leaves_no_credentials_behind(name: str) -> None:
    job = _job(name)
    assert "actions/checkout@" in job and "persist-credentials: false" in job


def test_the_jobs_run_in_order_and_the_fix_needs_both() -> None:
    assert "needs:" not in _job("assess")
    assert re.search(r"needs: assess\s", _job("report"))
    assert re.search(r"needs: \[assess, report\]", _job("fix"))
    assert re.search(r"needs: \[assess, report, fix\]", _job("notify"))


def test_one_run_per_issue_and_none_is_cancelled_halfway() -> None:
    assert "group: implement-issue-${{ github.event.issue.number }}" in TEXT
    assert "cancel-in-progress: false" in TEXT


def test_only_new_and_labelled_issues_are_events_it_listens_to() -> None:
    assert re.search(r"(?m)^on:\n  issues:\n    types: \[opened, labeled\]\n", TEXT)
    for banned in (
        "pull_request",
        "issue_comment",
        "workflow_run",
        "workflow_dispatch",
        "schedule",
    ):
        assert banned not in re.sub(r"#.*", "", TEXT)


def test_nothing_the_issue_author_wrote_is_used_in_an_expression() -> None:
    for banned in ("issue.title", "issue.body", "github.head_ref", "comment.body"):
        assert banned not in TEXT


def test_every_job_is_bounded_in_time() -> None:
    for name in JOBS:
        assert re.search(r"timeout-minutes: \d+", _job(name)), name


# --- that it matches the actions it calls ---


@pytest.mark.parametrize("name", JOBS)
def test_the_inputs_it_passes_exist_and_the_required_ones_are_all_there(
    name: str,
) -> None:
    action = (ROOT / "implement" / name / "action.yml").read_text(encoding="utf-8")
    declared = _declared_inputs(action)
    inputs_block = (
        action.split("\ninputs:\n", 1)[1]
        .split("\noutputs:\n", 1)[0]
        .split("\nruns:\n", 1)[0]
    )
    required = {
        key
        for key in declared
        if re.search(rf"(?ms)^  {key}:\n(.*?)(?=^  \w+:\n|\Z)", inputs_block)
        and "required: true"
        in re.search(rf"(?ms)^  {key}:\n(.*?)(?=^  \w+:\n|\Z)", inputs_block).group(1)  # type: ignore[union-attr]
    }
    step = next(
        part
        for part in re.split(r"(?m)^      - ", _job(name))
        if f"implement/{name}@" in part
    )
    passed = set(re.findall(r"(?m)^          (\w+):", step))
    assert passed <= declared, passed - declared
    assert required <= passed, required - passed


@pytest.mark.parametrize("name", JOBS)
def test_it_calls_this_repositorys_action_for_each_job(name: str) -> None:
    assert f"uses: menil/pr-code-review-action/implement/{name}@" in _job(name)


def test_the_commands_in_the_example_pass_the_check_they_will_meet() -> None:
    """The example is what people copy; it must not be refused by the first thing it meets."""
    import implement

    tools = re.findall(r"command_tools: '([^']*)'", TEXT)
    assert len(tools) == 2
    for value in tools:
        assert implement.check_command_tools(value) == [], value


# --- the daily limit and the failure comment ---


def _step(job: str, name: str) -> str:
    parts = re.split(r"(?m)^      - ", _job(job))
    return next(part for part in parts if part.startswith(f"name: {name}"))


def _step_if(job: str, name: str) -> str:
    match = re.search(r"(?m)^        if: (.*)$", _step(job, name))
    assert match, (job, name)
    return match.group(1)


def _after_assess(capped: str | None) -> dict[str, Any]:
    outputs = {} if capped is None else {"capped": capped}
    return {"needs": {"assess": {"result": "success", "outputs": outputs}}}


@pytest.mark.parametrize(
    ("capped", "assessed", "told"),
    [
        ("false", True, False),
        ("true", False, True),
        ("", True, False),
        (None, True, False),
    ],
)
def test_a_run_the_limit_stopped_is_told_so_and_not_assessed(
    capped: str | None, assessed: bool, told: bool
) -> None:
    context = _after_assess(capped)
    assert (
        bool(evaluate(_step_if("report", "Write the assessment"), context)) is assessed
    )
    assert (
        bool(evaluate(_step_if("report", "Say the daily limit was reached"), context))
        is told
    )


def test_the_two_report_steps_are_never_both_on_or_both_off() -> None:
    for capped in ("true", "false", "", None):
        context = _after_assess(capped)
        first = bool(evaluate(_step_if("report", "Write the assessment"), context))
        second = bool(
            evaluate(_step_if("report", "Say the daily limit was reached"), context)
        )
        assert first != second, capped


def test_a_run_the_limit_stopped_does_not_go_on_to_fix() -> None:
    """The fix needs the report's proceed output, which a skipped step never writes."""
    context = {"needs": {"report": {"result": "success", "outputs": {}}}}
    assert not evaluate(_condition("fix"), context)


@pytest.mark.parametrize(
    ("results", "says"),
    [
        ({"assess": "success", "report": "success", "fix": "success"}, False),
        ({"assess": "failure", "report": "skipped", "fix": "skipped"}, True),
        ({"assess": "success", "report": "failure", "fix": "skipped"}, True),
        ({"assess": "success", "report": "success", "fix": "failure"}, True),
        ({"assess": "success", "report": "success", "fix": "skipped"}, False),
        ({"assess": "skipped", "report": "skipped", "fix": "skipped"}, False),
        ({"assess": "cancelled", "report": "skipped", "fix": "skipped"}, False),
    ],
)
def test_a_failed_stage_is_reported_and_nothing_else_is(
    results: dict[str, str], says: bool
) -> None:
    context = {"needs": {name: {"result": result} for name, result in results.items()}}
    assert bool(evaluate(_condition("notify"), context)) is says


def test_the_limit_comes_from_a_repository_variable_in_both_places_it_is_used() -> None:
    assert TEXT.count("daily_cap: ${{ vars.AGENT_DAILY_CAP }}") == 2


def test_the_two_notices_ask_for_the_two_kinds() -> None:
    assert "kind: capped" in _step("report", "Say the daily limit was reached")
    assert "kind: failed" in _job("notify")


def _declared_outputs(job: str) -> set[str]:
    match = re.search(r"(?ms)^    outputs:\n(.*?)(?=^    \w|\Z)", _job(job))
    return set(re.findall(r"(?m)^      (\w+):", match.group(1))) if match else set()


def test_every_output_a_job_reads_from_another_is_one_that_job_declares() -> None:
    """A job output that is read but never declared is just empty, and nothing fails."""
    reads = set(re.findall(r"needs\.(\w+)\.outputs\.(\w+)", TEXT))
    assert reads, "the template reads no job outputs at all"
    for job, name in sorted(reads):
        assert name in _declared_outputs(job), (
            f"{job} does not declare the output {name}"
        )


def test_the_outputs_the_assess_job_declares_come_from_its_action() -> None:
    assert re.search(
        r"(?m)^      verdict: \$\{\{ steps\.assess\.outputs\.verdict \}\}", TEXT
    )
    assert re.search(
        r"(?m)^      capped: \$\{\{ steps\.assess\.outputs\.capped \}\}", TEXT
    )
