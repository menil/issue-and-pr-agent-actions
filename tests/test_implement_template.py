"""The implementation workflow decides who may spend a run and what each job may touch.

It is the reusable workflow in .github/workflows/implement.yml, which a repository calls from
a small workflow of its own (tests/test_implement_caller.py). Its `if:` guards are run through
an evaluator against a table of events, so they are tested by what they decide; its
permissions and wiring are read as text."""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from gha_expr import evaluate
from test_action_yml import _declared_inputs

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github" / "workflows" / "implement.yml").read_text(encoding="utf-8")
JOBS = ("assess", "report", "fix", "notify", "finish")


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
        # The caller passes the repository variable as an input.
        "inputs": {"agent_auto": "" if agent_auto is None else agent_auto},
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
    assert _permissions(_job("review")) == {
        "pull-requests": "write",
        "contents": "read",
    }
    assert _permissions(_job("assess")) == {"contents": "read", "issues": "read"}
    assert _permissions(_job("report")) == {"issues": "write"}
    assert _permissions(_job("notify")) == {"issues": "write"}
    assert _permissions(_job("finish")) == {"issues": "write"}
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
    assert set(re.findall(r"secrets\.(\w+)", TEXT)) == {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENROUTER_API_KEY",
    }
    assert "secrets." in _job("assess") and "secrets." in _job("fix")
    assert "secrets." in _job("review")  # the review is a model run too
    # The fallback key reaches the review step and nothing else.
    assert "OPENROUTER_API_KEY" in _job("review")
    for name in ("assess", "report", "fix", "finish", "notify"):
        assert "OPENROUTER_API_KEY" not in _job(name), name
    assert "secrets." not in _job("report")
    assert "secrets." not in _job("notify")


@pytest.mark.parametrize("name", ["report", "notify", "finish"])
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


def test_it_is_only_ever_called_never_started_by_an_event_of_its_own() -> None:
    code = re.sub(r"#.*", "", TEXT)
    # The `on:` block, up to the next top-level key. Asserting it holds the call trigger
    # keeps a layout change from leaving the check looking at the wrong text.
    block = re.search(r"(?ms)^on:\n(.*?)(?=^\S)", code)
    assert block and "workflow_call:" in block.group(1)
    events = re.findall(r"(?m)^  (\w+):", block.group(1))
    assert events == ["workflow_call"]
    # A pull request's own code or branch must never be what the workflow runs on.
    assert "pull_request_target" not in code
    assert "workflow_run" not in code


def test_no_expression_is_ever_put_into_a_shell_script() -> None:
    """The caller's inputs go to the actions' `with:`, never into `run:`: an input holds
    free text (the notes, the commands), and in a script it would be code. What a script
    needs reaches it through `env:`."""
    bodies = re.findall(r"(?ms)^ +run: \|\n(.*?)(?=^ +- name:|^  \w+:|\Z)", TEXT)
    assert bodies, "the workflow has no script to check"
    for body in bodies:
        assert "${{" not in body


def test_the_repository_variables_arrive_as_inputs_not_as_vars() -> None:
    assert "vars." not in re.sub(r"#.*", "", TEXT)
    assert "inputs.agent_auto == 'true'" in _condition("assess")


def test_every_action_it_calls_is_pinned_to_one_exact_release() -> None:
    """A file cannot name its own commit, so the actions inside are named by the release
    tag this file ships in, and a release's tag is locked. They must all agree, or one stage
    would run another release's code."""
    refs = set(re.findall(r"uses: menil/pr-code-review-action[\w/]*@(\S+)", TEXT))
    assert len(refs) == 1, refs
    (ref,) = refs
    assert re.fullmatch(r"v\d+\.\d+\.\d+", ref), ref


def test_nothing_the_issue_author_wrote_is_used_in_an_expression() -> None:
    for banned in ("issue.title", "issue.body", "github.head_ref", "comment.body"):
        assert banned not in TEXT


def test_every_job_is_bounded_in_time() -> None:
    for name in (*JOBS, "review"):
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


def test_the_limit_comes_from_the_daily_cap_input_in_both_places_it_is_used() -> None:
    assert TEXT.count("daily_cap: ${{ inputs.daily_cap }}") == 2


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


# --- reviewing the pull request the fix opened ---
#
# A pull request opened with the workflow's own token does not start the repository's own
# review workflow until a person clicks "Approve and run". So the review is run from here,
# on the number of the pull request the fix reports, and reads only its diff.


def test_the_fix_reports_the_pull_request_it_opened() -> None:
    assert re.search(
        r"(?m)^      pull_request: \$\{\{ steps\.fix\.outputs\.pull_request_url \}\}",
        _job("fix"),
    )
    assert re.search(r"(?m)^        id: fix$", _job("fix"))


def test_the_review_runs_only_after_a_fix_that_opened_a_pull_request() -> None:
    assert re.search(r"(?m)^    needs: fix$", _job("review"))
    assert _condition("review") == "needs.fix.outputs.pull_request != ''"
    assert evaluate(
        _condition("review"),
        {
            "needs": {
                "fix": {"outputs": {"pull_request": "https://github.com/o/r/pull/9"}}
            }
        },
    )
    assert not evaluate(
        _condition("review"), {"needs": {"fix": {"outputs": {"pull_request": ""}}}}
    )


def test_a_failed_review_does_not_fail_the_run_or_comment_on_the_issue() -> None:
    assert re.search(r"(?m)^    continue-on-error: true$", _job("review"))
    assert "review" not in re.search(r"needs: \[([^\]]*)\]", _job("notify")).group(1)  # type: ignore[union-attr]


def test_the_review_checks_out_nothing_and_never_runs_the_pull_requests_code() -> None:
    job = _job("review")
    assert "checkout" not in job
    assert "run: |" in job and "pull_request_number" in job
    for banned in ("pull_request_target", "head.ref", "head_ref"):
        assert banned not in job


def test_the_review_calls_this_repositorys_review_action_with_the_number() -> None:
    job = _job("review")
    assert "uses: menil/pr-code-review-action@v1" in job
    assert "pull_request_number: ${{ steps.pr.outputs.number }}" in job
    assert "claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in job


def test_every_input_the_review_step_passes_is_one_the_review_action_declares() -> None:
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    step = _job("review").split("pr-code-review-action@v1", 1)[1]
    passed = set(re.findall(r"(?m)^          (\w+):", step))
    assert passed and passed <= _declared_inputs(action), passed - _declared_inputs(
        action
    )


def _number_step() -> str:
    match = re.search(
        r"(?ms)id: pr\n.*?run: \|\n(.*?)(?=\n      - name:)", _job("review")
    )
    assert match
    return "\n".join(line[10:] for line in match.group(1).splitlines())


@pytest.mark.parametrize(
    ("url", "number"),
    [
        ("https://github.com/menil/pdf-vdiff/pull/75", "75"),
        ("https://github.com/o/r.with-dots_and-dashes/pull/1", "1"),
    ],
)
def test_the_number_is_read_from_the_pull_request_url(
    tmp_path: Path, url: str, number: str
) -> None:
    done, found = _run_number_step(tmp_path, url)
    assert done.returncode == 0, done.stderr
    assert found == f"number={number}\n"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pull/7/files",
        "https://github.com/o/r/issues/7",
        "https://evil.example/o/r/pull/7",
        "http://github.com/o/r/pull/7",
        "https://github.com/o/r/pull/7; touch pwned",
        "https://github.com/o/r/pull/7\nnumber=8",
        "https://github.com/o/r/pull/$(id)",
    ],
)
def test_anything_else_stops_the_review_and_writes_no_number(
    tmp_path: Path, url: str
) -> None:
    done, found = _run_number_step(tmp_path, url)
    assert done.returncode != 0
    assert found == ""
    assert not (tmp_path / "pwned").exists()


def _run_number_step(
    tmp_path: Path, url: str
) -> tuple["subprocess.CompletedProcess[str]", str]:
    out = tmp_path / "out"
    out.write_text("")
    done = subprocess.run(
        ["bash", "-c", _number_step()],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "URL": url, "GITHUB_OUTPUT": str(out)},
        capture_output=True,
        text=True,
    )
    return done, out.read_text()


# --- tidying the issue once the pull request is open ---


def test_the_issue_is_tidied_after_a_fix_that_finished_either_way() -> None:
    """Whether it opened a pull request or changed nothing, the issue is tidied; when the
    fix did not finish (it failed, or was never asked to run) nothing is."""
    assert re.search(r"(?m)^    needs: fix$", _job("finish"))
    assert _condition("finish") == "needs.fix.outputs.outcome != ''"
    for outcome, tidied in (("opened", True), ("no_change", True), ("", False)):
        context = {"needs": {"fix": {"outputs": {"outcome": outcome}}}}
        assert bool(evaluate(_condition("finish"), context)) is tidied, outcome


def test_the_fix_job_reports_its_outcome() -> None:
    assert re.search(
        r"(?m)^      outcome: \$\{\{ steps\.fix\.outputs\.outcome \}\}", _job("fix")
    )


def test_the_tidy_job_passes_the_outcome_and_the_url() -> None:
    job = _job("finish")
    assert "outcome: ${{ needs.fix.outputs.outcome }}" in job
    assert "pull_request_url: ${{ needs.fix.outputs.pull_request }}" in job


def test_a_fix_that_changed_nothing_runs_neither_the_review_nor_the_failure_comment() -> (
    None
):
    nothing = {
        "needs": {"fix": {"outputs": {"outcome": "no_change", "pull_request": ""}}}
    }
    assert not evaluate(_condition("review"), nothing)
    # The failure comment is for a stage that failed: a fix that finished did not.
    assert _condition("notify") == "failure()"


def test_a_failed_tidy_does_not_fail_the_run_or_comment_on_the_issue() -> None:
    assert re.search(r"(?m)^    continue-on-error: true$", _job("finish"))
    assert "finish" not in re.search(r"needs: \[([^\]]*)\]", _job("notify")).group(1)  # type: ignore[union-attr]


def test_the_tidy_job_gets_no_secret_and_only_the_issue_write_permission() -> None:
    job = _job("finish")
    assert "secrets." not in job and "checkout" not in job
    assert _permissions(job) == {"issues": "write"}
    assert "pull_request_url: ${{ needs.fix.outputs.pull_request }}" in job
    assert "issue_number: ${{ github.event.issue.number }}" in job


def test_each_secret_goes_only_to_the_input_made_for_it() -> None:
    """A secret handed to the wrong input could end up somewhere that logs it or sends it on."""
    expected = {
        "CLAUDE_CODE_OAUTH_TOKEN": "claude_code_oauth_token",
        "OPENROUTER_API_KEY": "openrouter_api_key",
    }
    lines = re.findall(r"(?m)^\s+(\w+): \$\{\{ secrets\.(\w+) \}\}\s*$", TEXT)
    assert lines, "no secret is passed to anything"
    for target, secret in lines:
        assert expected[secret] == target, (secret, target)
    # And none is used in some other shape (inside a larger expression, say).
    assert len(lines) == len(re.findall(r"secrets\.\w+", TEXT))


def test_every_third_party_action_is_pinned_to_a_full_commit() -> None:
    """Only this repository's actions (named by release) and GitHub's own may be named by a
    tag; anything else that is handed the job's token or the checkout is pinned by commit."""
    for owner_repo, ref in re.findall(r"(?m)^\s+(?:- )?uses: ([\w./-]+)@(\S+)", TEXT):
        if owner_repo.startswith(("menil/pr-code-review-action", "actions/")):
            continue
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (owner_repo, ref)
