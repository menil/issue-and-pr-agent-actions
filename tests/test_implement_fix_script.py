"""Run the fix action's commit-and-open-a-pull-request script for real.

The script is lifted out of implement/fix/action.yml and run in a throwaway git repository
with a local bare repository as its remote and a stub `gh`. What it must refuse is the point:
a change under .github/, a lockfile, no change at all, an odd issue number, or a branch that
already exists must stop it before anything is pushed or any pull request is opened."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_implement_actions import _run_bodies

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = next(body for body in _run_bodies("fix") if "gh pr create" in body)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is needed")


class Sandbox:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.remote = tmp_path / "remote.git"
        self.work = tmp_path / "work"
        self.bin = tmp_path / "bin"
        self.gh_log = tmp_path / "gh.log"
        self.output = tmp_path / "github_output"
        self.bin.mkdir()
        # The script calls `python`; make sure that name exists, whatever runs the tests.
        (self.bin / "python").symlink_to(sys.executable)
        gh = self.bin / "gh"
        gh.write_text(
            f'#!/bin/sh\necho "$@" >> "{self.gh_log}"\n'
            "prev=''\n"
            'for arg in "$@"; do\n'
            f'  if [ "$prev" = "--body-file" ]; then cp "$arg" "{tmp_path}/pr_body.md"; fi\n'
            '  prev="$arg"\n'
            "done\n"
            'if [ -n "$GH_FAIL" ]; then\n'
            '  echo "GraphQL: GitHub Actions is not permitted to create or approve pull requests'
            ' (createPullRequest)" >&2\n'
            "  exit 1\n"
            "fi\n"
            "echo https://github.com/o/r/pull/99\n"
        )
        gh.chmod(0o755)
        self.gitconfig = tmp_path / "gitconfig"
        self.gitconfig.write_text(
            f'[url "file://{self.remote}"]\n'
            "\tinsteadOf = https://x-access-token:tok@github.com/o/r.git\n"
            '[url "file:///nonexistent/nowhere.git"]\n'
            "\tinsteadOf = https://x-access-token:tok@github.com/o/unreachable.git\n"
            "[user]\n\tname = test\n\temail = test@example.com\n"
            "[init]\n\tdefaultBranch = main\n"
        )
        self.output.write_text("")
        self._git(tmp_path, "init", "--bare", "-q", str(self.remote))
        self.work.mkdir()
        self._git(self.work, "init", "-q")
        for rel, text in {
            "slug.py": "x = 1\n",
            "tests/test_slug.py": "def test():\n    pass\n",
            ".github/workflows/validate.yml": "name: v\n",
            "Cargo.lock": "# lock\n",
        }.items():
            self.write(rel, text)
        self._git(self.work, "add", "-A")
        self._git(self.work, "commit", "-q", "-m", "initial")
        self._git(self.work, "remote", "add", "origin", f"file://{self.remote}")
        self._git(self.work, "push", "-q", "origin", "main")
        # Everything below relies on the rewriting in the gitconfig: if it were not applied,
        # a push would try to reach github.com. Fail here, clearly, instead.
        listed = subprocess.run(
            ["git", "ls-remote", "https://x-access-token:tok@github.com/o/r.git"],
            env={**os.environ, "GIT_CONFIG_GLOBAL": str(self.gitconfig)},
            capture_output=True,
            text=True,
        )
        assert "refs/heads/main" in listed.stdout, (
            "the test's git URL rewriting is not applied: " + listed.stderr
        )
        # What `fetch` does: the issue file exists but can never be committed.
        (self.work / ".git" / "info").mkdir(exist_ok=True)
        with open(self.work / ".git" / "info" / "exclude", "a") as f:
            f.write(".agent-issue.md\n.agent-change.md\n.agent-stop.md\n")
        self.write(".agent-issue.md", "# the issue\n")

    def env(self, **overrides: str) -> dict[str, str]:
        base = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GH_TOKEN": "tok",
            "NUMBER": "17",
            "BASE_BRANCH": "main",
            "REPO": "o/r",
            "IMPLEMENT": str(ROOT / "src" / "implement.py"),
            "GITHUB_OUTPUT": str(self.output),
            "DRAFT": "false",
        }
        return {**base, **overrides}

    def _git(self, cwd: Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env={**os.environ, "GIT_CONFIG_GLOBAL": str(self.gitconfig)},
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout

    def write(self, rel: str, text: str) -> None:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", SCRIPT],
            cwd=self.work,
            env=self.env(**overrides),
            capture_output=True,
            text=True,
        )

    def remote_branches(self) -> list[str]:
        out = self._git(self.root, "--git-dir", str(self.remote), "branch", "--list")
        return [line.strip("* ").strip() for line in out.splitlines()]

    def commits_ahead_of_main(self) -> int:
        return int(self._git(self.work, "rev-list", "--count", "main..HEAD"))

    def gh_calls(self) -> list[str]:
        return self.gh_log.read_text().splitlines() if self.gh_log.exists() else []


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_a_good_fix_is_pushed_on_its_own_branch_and_opened_ready_for_review(
    box: Sandbox,
) -> None:
    box.write("slug.py", "x = 2\n")
    box.write("tests/test_regression.py", "def test_it():\n    pass\n")
    done = box.run()
    assert done.returncode == 0, done.stderr
    assert box.remote_branches() == ["agent/issue-17", "main"]
    files = box._git(box.work, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == ["slug.py", "tests/test_regression.py"]
    assert box._git(box.work, "show", "--format=%s", "-s", "HEAD").strip() == (
        "fix: address issue #17"
    )
    log = box.gh_log.read_text()
    assert log.count("pr create") == 1
    assert log.startswith("pr create --base main --head agent/issue-17")
    assert "--draft" not in log
    assert "pull_request_url=https://github.com/o/r/pull/99" in box.output.read_text()


def test_a_draft_is_opened_only_when_asked_for(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    done = box.run(DRAFT="true")
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.gh_log.read_text().startswith(
        "pr create --draft --base main --head agent/issue-17"
    )


# Hostile on purpose. The input ends up in a shell script, so besides near misses ("True",
# "yes", "1") it includes a command-injection attempt ("true; touch x") and a value that
# looks like an option ("--draft"). Each must be refused by the strict true/false check, and
# the test also asserts that nothing was pushed and no pull request was opened.
@pytest.mark.parametrize("value", ["", "yes", "True", "1", "true; touch x", "--draft"])
def test_anything_but_true_or_false_for_the_draft_input_is_refused(
    box: Sandbox, value: str
) -> None:
    box.write("slug.py", "x = 2\n")
    done = box.run(DRAFT=value)
    assert done.returncode != 0
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


def test_a_refused_pull_request_says_which_setting_to_turn_on(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    done = box.run(GH_FAIL="1")
    assert done.returncode != 0
    assert "Allow GitHub Actions to create and approve pull requests" in done.stdout
    assert "agent/issue-17" in done.stdout and "was pushed" in done.stdout
    assert "gh: GraphQL: GitHub Actions is not permitted" in done.stdout
    assert "pull_request_url" not in box.output.read_text()


def test_the_issue_file_is_never_committed(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    box.run()
    tree = box._git(box.work, "ls-tree", "-r", "--name-only", "HEAD")
    assert ".agent-issue.md" not in tree.split()


CHANGE = """\
Type: fix
Title: return PasswordProtected for encrypted PDFs
Not done: InvalidDimensions is still never constructed

Opening an encrypted PDF printed a raw error. The load error is now mapped.
"""


def test_the_model_s_description_becomes_the_commit_and_the_pull_request_text(
    box: Sandbox,
) -> None:
    box.write("slug.py", "x = 2\n")
    box.write(".agent-change.md", CHANGE)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    message = box._git(box.work, "log", "-1", "--format=%B", "HEAD")
    assert message.startswith("fix: return PasswordProtected for encrypted PDFs\n\n")
    assert "Opening an encrypted PDF printed a raw error." in message
    assert message.rstrip().endswith("Refs #17")
    log = box.gh_log.read_text()
    assert "--title fix: return PasswordProtected for encrypted PDFs" in log
    body = (box.root / "pr_body.md").read_text()
    assert body.startswith("Opening an encrypted PDF printed a raw error.")
    assert "Not done" in body and body.rstrip().endswith("Refs #17")
    assert "Closes" not in body
    assert "pull_request_url=https://github.com/o/r/pull/99" in box.output.read_text()


def test_a_complete_change_closes_the_issue(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    box.write(
        ".agent-change.md", "Type: feat\nTitle: add the flag\n\nIt adds the flag.\n"
    )
    assert box.run().returncode == 0
    assert (box.root / "pr_body.md").read_text().rstrip().endswith("Closes #17")
    assert box._git(box.work, "log", "-1", "--format=%s", "HEAD").strip() == (
        "feat: add the flag"
    )


@pytest.mark.parametrize(
    "content",
    [
        "",
        "no header at all",
        "Type: chore\nTitle: x\n\nbody\n",
        "Type: fix\nTitle: x\n",
    ],
)
def test_a_description_outside_the_contract_does_not_cost_the_fix(
    box: Sandbox, content: str
) -> None:
    box.write("slug.py", "x = 2\n")
    box.write(".agent-change.md", content)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert box._git(box.work, "log", "-1", "--format=%s", "HEAD").strip() == (
        "fix: address issue #17"
    )
    assert (box.root / "pr_body.md").read_text().rstrip().endswith("Closes #17")


def test_the_description_file_is_never_committed(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    box.write(".agent-change.md", CHANGE)
    box.run()
    tree = box._git(box.work, "ls-tree", "-r", "--name-only", "HEAD")
    assert ".agent-change.md" not in tree.split()


def test_a_description_alone_is_not_a_change(box: Sandbox) -> None:
    box.write(".agent-change.md", CHANGE)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "outcome=no_change" in box.output.read_text()
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


def test_when_nothing_changed_the_reason_and_the_denials_are_reported(
    box: Sandbox,
) -> None:
    box.write(".agent-stop.md", "It needs a decision about TLS defaults.\n")
    execution = box.root / "exec.json"
    execution.write_text(
        json.dumps(
            [
                {
                    "type": "result",
                    "permission_denials": [
                        {
                            "tool_name": "Bash",
                            "tool_input": {"command": "uv run pytest"},
                        }
                    ],
                }
            ]
        )
    )
    done = box.run(EXECUTION_FILE=str(execution))
    assert done.returncode == 0, done.stdout + done.stderr
    written = box.output.read_text()
    assert "outcome=no_change" in written
    assert "reason=It needs a decision about TLS defaults." in written
    assert 'denied=["Bash: uv run pytest"]' in written


def test_a_missing_or_broken_stop_report_never_costs_the_outcome(box: Sandbox) -> None:
    done = box.run(EXECUTION_FILE=str(box.root / "does-not-exist.json"))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "outcome=no_change" in box.output.read_text()


def test_the_stop_file_is_never_committed(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    box.write(".agent-stop.md", "reason\n")
    assert box.run().returncode == 0
    tree = box._git(box.work, "ls-tree", "-r", "--name-only", "HEAD")
    assert ".agent-stop.md" not in tree.split()


def test_a_change_that_is_opened_says_so(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    assert box.run().returncode == 0
    assert "outcome=opened" in box.output.read_text()
    assert "outcome=no_change" not in box.output.read_text()


def test_a_refused_change_is_still_a_failure_not_an_outcome(box: Sandbox) -> None:
    """Only an empty change is an outcome. A change that must not be committed is the
    model going somewhere it should not, and that stays an error."""
    box.write("Cargo.lock", "changed\n")
    done = box.run()
    assert done.returncode != 0
    assert "outcome=" not in box.output.read_text()
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


def test_a_hostile_title_cannot_become_an_option_or_a_second_command(
    box: Sandbox,
) -> None:
    box.write("slug.py", "x = 2\n")
    box.write(
        ".agent-change.md",
        "Type: fix\nTitle: --web $(touch /tmp/pwned_by_title) `id` ; rm -rf x\n\nb\n",
    )
    assert box.run().returncode == 0
    subject = box._git(box.work, "log", "-1", "--format=%s", "HEAD").strip()
    assert subject.startswith("fix: ")
    assert "$" not in subject and "`" not in subject
    assert not Path("/tmp/pwned_by_title").exists()


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/validate.yml",
        ".github/workflows/new.yml",
        ".github/CODEOWNERS",
        "Cargo.lock",
        ".githooks/pre-commit",
        ".claude/settings.json",
        "docs/CODEOWNERS",
        "a\nb.py",
    ],
)
def test_a_protected_change_stops_everything(box: Sandbox, path: str) -> None:
    box.write("slug.py", "x = 2\n")  # a good change alongside it does not excuse it
    box.write(path, "changed\n")
    done = box.run()
    assert done.returncode != 0
    assert box.remote_branches() == ["main"]
    assert box.gh_calls() == []
    assert box.commits_ahead_of_main() == 0


def test_deleting_a_protected_file_is_a_protected_change(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    (box.work / ".github" / "workflows" / "validate.yml").unlink()
    assert box.run().returncode != 0
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


def test_renaming_a_protected_file_is_a_protected_change(box: Sandbox) -> None:
    box._git(box.work, "mv", "Cargo.lock", "docs.lock")
    assert box.run().returncode != 0
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


def test_a_change_made_by_moving_a_file_out_of_a_protected_directory_is_refused(
    box: Sandbox,
) -> None:
    (box.work / "stolen.yml").write_text("x\n")
    box._git(box.work, "mv", ".github/workflows/validate.yml", "stolen2.yml")
    assert box.run().returncode != 0
    assert box.remote_branches() == ["main"]


def test_a_symbolic_link_to_a_protected_place_is_refused(box: Sandbox) -> None:
    """Writing through a link would change the workflows without the path showing it."""
    os.symlink(".github", box.work / "docs-link")
    box.write("slug.py", "x = 2\n")
    done = box.run()
    assert done.returncode != 0
    assert "symbolic link" in done.stdout
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


def test_a_change_made_through_a_symbolic_link_is_still_seen_at_its_real_path(
    box: Sandbox,
) -> None:
    os.symlink(".github", box.work / "docs-link")
    (box.work / "docs-link" / "workflows" / "validate.yml").write_text("name: pwned\n")
    assert box.run().returncode != 0
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


def test_no_change_is_an_outcome_not_a_failure(box: Sandbox) -> None:
    """The model must change nothing when it cannot show the problem with a failing test.
    That is the instruction being followed, so the step succeeds and says so."""
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "outcome=no_change" in box.output.read_text()
    assert "pull_request_url" not in box.output.read_text()
    assert box.remote_branches() == ["main"] and box.gh_calls() == []
    assert box.commits_ahead_of_main() == 0
    assert box._git(box.work, "branch", "--show-current").strip() == "main"


@pytest.mark.parametrize("number", ["", "x", "17; touch /tmp/pwned", "-1", "1 2"])
def test_an_odd_issue_number_is_refused(box: Sandbox, number: str) -> None:
    box.write("slug.py", "x = 2\n")
    assert box.run(NUMBER=number).returncode != 0
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


@pytest.mark.parametrize("base", ["", "main; touch x", "a b", "$(id)"])
def test_an_odd_base_branch_is_refused(box: Sandbox, base: str) -> None:
    box.write("slug.py", "x = 2\n")
    assert box.run(BASE_BRANCH=base).returncode != 0
    assert box.gh_calls() == []


def test_a_branch_that_already_exists_is_not_overwritten(box: Sandbox) -> None:
    box._git(box.work, "push", "-q", "origin", "main:agent/issue-17")
    box.write("slug.py", "x = 2\n")
    done = box.run()
    assert done.returncode != 0
    assert "already exists" in done.stdout
    assert box.gh_calls() == []
    # Nothing was added to the branch that was there.
    assert (
        box._git(
            box.root,
            "--git-dir",
            str(box.remote),
            "rev-list",
            "--count",
            "agent/issue-17",
        ).strip()
        == "1"
    )


def test_a_good_description_names_the_branch_after_the_change(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    box.write(".agent-change.md", CHANGE)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    branch = "agent/issue-17-return-passwordprotected-for-encrypted"
    assert box.remote_branches() == [branch, "main"]
    assert f"--head {branch} " in box.gh_log.read_text()
    assert box._git(box.work, "branch", "--show-current").strip() == branch


@pytest.mark.parametrize(
    "existing", ["agent/issue-17", "agent/issue-17-an-earlier-try"]
)
def test_an_earlier_branch_for_the_issue_blocks_a_new_one_whatever_its_name(
    box: Sandbox, existing: str
) -> None:
    """The name now depends on the model's words, so a retry may pick another one; the
    check must still see the first attempt, or the issue gets two pull requests."""
    box._git(box.work, "push", "-q", "origin", f"main:{existing}")
    box.write("slug.py", "x = 2\n")
    box.write(".agent-change.md", CHANGE)
    done = box.run()
    assert done.returncode != 0
    assert "already exists" in done.stdout
    assert box.gh_calls() == []
    assert sorted(box.remote_branches()) == sorted([existing, "main"])


@pytest.mark.parametrize(
    "other", ["agent/issue-170", "agent/issue-170-x", "agent/issue-1"]
)
def test_another_issues_branch_does_not_block_this_one(
    box: Sandbox, other: str
) -> None:
    box._git(box.work, "push", "-q", "origin", f"main:{other}")
    box.write("slug.py", "x = 2\n")
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "agent/issue-17" in box.remote_branches()


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "main",
        "agent/issue-18",
        "agent/issue-17x",
        "agent/issue-17-",
        "agent/issue-17-A",
        "agent/issue-17-a b",
        "agent/issue-17-a..b",
        "agent/issue-17-$(touch x)",
        "agent/issue-17--x",
        "../agent/issue-17",
    ],
)
def test_a_branch_name_that_is_not_this_issues_is_refused(
    box: Sandbox, branch: str
) -> None:
    """Whatever writes the name, the script checks it again before it names a branch."""
    shim = box.bin / "python"
    shim.unlink()
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = describe ]; then\n'
        f'  {sys.executable} "$@"\n'
        f'  printf %s "{branch}" > "$3/branch.txt"\n'
        "else\n"
        f'  exec {sys.executable} "$@"\n'
        "fi\n"
    )
    shim.chmod(0o755)
    box.write("slug.py", "x = 2\n")
    done = box.run()
    assert done.returncode != 0, done.stdout
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


def test_a_remote_that_cannot_be_reached_is_not_mistaken_for_no_branch(
    box: Sandbox,
) -> None:
    box.write("slug.py", "x = 2\n")
    done = box.run(REPO="o/unreachable")  # mapped to a path that does not exist
    assert "Could not look for the branch" in done.stdout
    assert done.returncode != 0
    assert box.gh_calls() == []


def test_a_fix_with_too_many_files_is_refused(box: Sandbox) -> None:
    for n in range(30):
        box.write(f"gen/f{n}.py", "x\n")
    assert box.run().returncode != 0
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


# --- the check on the inputs that end up inside quotes, run for both model stages ---


def _input_check(name: str) -> str:
    return next(body for body in _run_bodies(name) if "MAX_TURNS" in body)


def _run_input_check(
    name: str, tmp_path: Path, max_turns: str, tools: str
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if not (bin_dir / "python").exists():
        (bin_dir / "python").symlink_to(sys.executable)
    return subprocess.run(
        ["bash", "-c", _input_check(name)],
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "MAX_TURNS": max_turns,
            "COMMAND_TOOLS": tools,
            "IMPLEMENT": str(ROOT / "src" / "implement.py"),
        },
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("name", ["assess", "fix"])
@pytest.mark.parametrize(
    ("max_turns", "tools"),
    [
        ("12", "Bash(cargo test:*)"),
        ("1", "Bash(python3 slug.py:*),Bash(python3 -m unittest:*)"),
        ("30", "Bash(just test)"),
    ],
)
def test_good_inputs_pass_the_check(
    name: str, tmp_path: Path, max_turns: str, tools: str
) -> None:
    assert _run_input_check(name, tmp_path, max_turns, tools).returncode == 0


@pytest.mark.parametrize("name", ["assess", "fix"])
@pytest.mark.parametrize(
    ("max_turns", "tools"),
    [
        ("", "Bash(x)"),
        ("x", "Bash(x)"),
        ("12; id", "Bash(x)"),
        ("-1", "Bash(x)"),
        ("1.5", "Bash(x)"),
        ("12", ""),
        ("12", 'Bash(x)" --dangerouslySkipPermissions "'),
        ("12", "Bash(x)\n--allowedTools Bash"),
        ("12", 'a"b'),
        ("12", "Bash"),
        ("12", "Bash(*)"),
        ("12", "Bash(bash:*)"),
        ("12", "Bash(curl:*)"),
        ("12", "Bash(cargo test:*),Bash(env)"),
        ("12", "WebFetch"),
    ],
)
def test_inputs_that_could_break_out_or_open_too_much_are_refused(
    name: str, tmp_path: Path, max_turns: str, tools: str
) -> None:
    done = _run_input_check(name, tmp_path, max_turns, tools)
    assert done.returncode != 0
    assert "::error::" in done.stdout
