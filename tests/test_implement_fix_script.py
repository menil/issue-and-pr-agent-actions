"""Run the fix action's commit-and-open-a-pull-request script for real.

The script is lifted out of implement/fix/action.yml and run in a throwaway git repository
with a local bare repository as its remote and a stub `gh`. What it must refuse is the point:
a change under .github/, a lockfile, no change at all, an odd issue number, or a branch that
already exists must stop it before anything is pushed or any pull request is opened."""

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
            f'#!/bin/sh\necho "$@" >> "{self.gh_log}"\necho https://github.com/o/r/pull/99\n'
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
            f.write(".agent-issue.md\n")
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


def test_a_good_fix_is_pushed_on_its_own_branch_and_opened_as_a_draft(
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
    assert log.startswith("pr create --draft --base main --head agent/issue-17")
    assert "pull_request_url=https://github.com/o/r/pull/99" in box.output.read_text()


def test_the_issue_file_is_never_committed(box: Sandbox) -> None:
    box.write("slug.py", "x = 2\n")
    box.run()
    tree = box._git(box.work, "ls-tree", "-r", "--name-only", "HEAD")
    assert ".agent-issue.md" not in tree.split()


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


def test_no_change_is_not_a_pull_request(box: Sandbox) -> None:
    done = box.run()
    assert done.returncode != 0
    assert box.remote_branches() == ["main"] and box.gh_calls() == []


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
    name: str, max_turns: str, tools: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _input_check(name)],
        env={
            "PATH": os.environ["PATH"],
            "MAX_TURNS": max_turns,
            "COMMAND_TOOLS": tools,
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
def test_good_inputs_pass_the_check(name: str, max_turns: str, tools: str) -> None:
    assert _run_input_check(name, max_turns, tools).returncode == 0


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
    ],
)
def test_inputs_that_could_break_out_of_the_quotes_are_refused(
    name: str, max_turns: str, tools: str
) -> None:
    done = _run_input_check(name, max_turns, tools)
    assert done.returncode != 0
    assert "::error::" in done.stdout
