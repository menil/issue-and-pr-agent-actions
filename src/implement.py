#!/usr/bin/env python3
"""Shared code of the issue implementation actions (design: docs/implement-action.md).

Commands, run by the composite actions under implement/:

  fetch          Read an issue, refuse it unless a trusted person stands behind it, and
                 write it to a file for the model to read.
  schema         Print the JSON schema of the verdict, built from the contract below.
  prompt STAGE   Build the model's prompt: the stage's instruction file, then the
                 repository's own notes. STAGE is assess or fix.
  report         Validate the model's verdict, write the labels and the one comment, and
                 say whether the next stage may go ahead.
  check-changes  Read the paths the model changed (NUL-separated, on stdin) and refuse
                 anything that must not be committed.

The model never decides what is written. It returns a verdict; this code checks every
field against a fixed contract, picks the labels from a fixed table, and builds the
comment from a template around sanitized text.
"""

import json
import os
import posixpath
import re
import sys
import urllib.error
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import triage
from sanitize import log_text, sanitize_or_drop, strip_invisible_chars

# Who may start a run: the author's association with the repository, as GitHub reports it,
# or a person with write access who applied the label to someone else's issue.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})

MAX_TITLE_CHARS = 300
MAX_BODY_CHARS = 6000
MAX_EVIDENCE_CHARS = 1500
MAX_SUMMARY_CHARS = 600

VALID_VALUES = ("yes", "no", "unclear")
REPRODUCED_VALUES = ("yes", "no", "not_applicable", "not_attempted")
EFFORT_VALUES = ("small", "medium", "large")

# The only labels this code ever writes, with the colour and description they are created
# with if the repository lacks them. invalid, wontfix and spam are not here: those are
# what a hostile issue would ask for, so they stay with people.
LABEL_STYLE = {
    "repro:confirmed": ("0e8a16", "The assessment reproduced this issue"),
    "repro:failed": ("d93f0b", "The assessment could not reproduce this issue"),
    "repro:not-applicable": ("c5def5", "Reproduction does not apply to this issue"),
    "effort:small": ("c2e0c6", "Assessed as a small change"),
    "effort:medium": ("fef2c0", "Assessed as a medium-sized change"),
    "effort:large": ("f9d0c4", "Assessed as a large change"),
}
ALLOWED_LABELS = frozenset(LABEL_STYLE)
REPRO_LABELS = {
    "yes": "repro:confirmed",
    "no": "repro:failed",
    "not_applicable": "repro:not-applicable",
}

# Hidden in the comment: once it is there the issue has been assessed, so a second run
# stops before asking the model again.
MARKER = "<!-- agent-implement:assessment -->"

# The model is told the repository's commands in notes the maintainer writes; cap them so
# a runaway file cannot crowd out the instructions.
MAX_NOTES_CHARS = 4000
STAGES = ("assess", "fix")
INSTRUCTION_DIR = Path(__file__).resolve().parent.parent / "implement"

# What the model may not have changed, enforced here because the instruction not to is
# only a request. A fix has no business in the workflows, the hooks, the agent
# configuration, the dependencies' lockfiles or the files that decide who reviews.
MAX_CHANGED_FILES = 25
PROTECTED_TOP_DIRS = frozenset(
    {".github", ".git", ".githooks", ".claude", ".codex", ".agents"}
)
PROTECTED_NAMES = frozenset(
    {".gitmodules", ".gitattributes", ".agent-issue.md", "codeowners"}
)
LOCKFILES = frozenset(
    {
        "cargo.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "flake.lock",
        "go.sum",
        "gemfile.lock",
        "composer.lock",
        "mix.lock",
        "pubspec.lock",
        "gradle.lockfile",
        "packages.lock.json",
    }
)

ISSUE_FILE = Path(".agent-issue.md")
EXCLUDE_FILE = Path(".git/info/exclude")
REPO_PATTERN = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
LOGIN_PATTERN = re.compile(r"[A-Za-z0-9-]+(\[bot\])?")


class NotTrusted(Exception):
    """This issue may not start a run. The message is ours and is safe to log."""


@dataclass(frozen=True)
class TrustedIssue:
    number: int
    title: str
    body: str


@dataclass(frozen=True)
class Verdict:
    """What the model said, after validation: every field is inside the contract."""

    valid: str
    reproduced: str
    effort: str
    evidence: str
    summary: str


@dataclass(frozen=True)
class Report:
    """What will be written, decided by this code from a Verdict."""

    comment: str
    labels: tuple[str, ...]
    proceed: bool


# --- the issue and who may start a run ---


def check_issue(raw: Any, actor_permission: str | None = None) -> TrustedIssue:
    """Accept an issue only if a trusted person stands behind it.

    When a person applied the label that started the run, `actor_permission` is their
    permission level and it alone decides: they must have write access, whoever wrote
    the issue, because they are the one spending the run. Otherwise the author must be an
    owner, member or collaborator. A pull request and a bot's issue are never accepted,
    whoever vouches.
    """
    if not isinstance(raw, dict):
        raise NotTrusted("it is not an issue")
    number = raw.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise NotTrusted("it is not an issue")
    if raw.get("pull_request") is not None:
        raise NotTrusted("it is a pull request, not an issue")
    user = raw.get("user")
    user = user if isinstance(user, dict) else {}
    login = str(user.get("login") or "")
    if user.get("type") == "Bot" or login.endswith("[bot]"):
        raise NotTrusted("its author is a bot")
    # No value is quoted below: they come from GitHub, but nothing here depends on that.
    if actor_permission is not None:
        if actor_permission not in WRITE_PERMISSIONS:
            raise NotTrusted(
                "the person who applied the label does not have write access"
            )
    elif raw.get("author_association") not in TRUSTED_ASSOCIATIONS:
        raise NotTrusted("its author is not an owner, member or collaborator")
    return TrustedIssue(
        number=number,
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
    )


def actor_permission(repo: str, login: str, token: str) -> str:
    """The permission level of a person on the repository, or 'none' if unknown."""
    if not LOGIN_PATTERN.fullmatch(login):
        return "none"
    try:
        reply = triage.github_request(
            "GET",
            repo,
            f"/collaborators/{urllib.parse.quote(login, safe='')}/permission",
            token,
        )
    except urllib.error.HTTPError:
        return "none"
    permission = reply.get("permission") if isinstance(reply, dict) else None
    return permission if isinstance(permission, str) else "none"


def issue_markdown(issue: TrustedIssue) -> str:
    """The issue as the file the model reads, with what a reader cannot see removed."""
    title = " ".join(strip_invisible_chars(issue.title).split())[:MAX_TITLE_CHARS]
    body = strip_invisible_chars(issue.body)[:MAX_BODY_CHARS]
    return f"# {title}\n\n{body}"


def write_issue_file(
    text: str, path: Path = ISSUE_FILE, exclude: Path = EXCLUDE_FILE
) -> None:
    """Write the issue file and keep it out of any commit, whatever adds files later."""
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if path.name not in existing.splitlines():
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(f"{existing}{separator}{path.name}\n", encoding="utf-8")
    path.write_text(text, encoding="utf-8")


# --- the verdict ---


def _enum(data: dict[str, Any], field: str, allowed: tuple[str, ...]) -> str:
    value = data.get(field)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"'{field}' is outside the contract")
    return value


def _text(value: Any, max_chars: int) -> str:
    if value is None:
        return ""
    return strip_invisible_chars(str(value)).strip()[:max_chars]


def parse_verdict(raw: str) -> Verdict:
    """Read the model's JSON verdict. Anything outside the contract rejects all of it.

    The three enumerated fields must be exactly one of their values. The two texts are
    made into strings, stripped of invisible characters and cut to length; the model
    cannot add other fields. A claim to have reproduced the problem needs evidence: with
    none, it is not believed.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("the verdict is not an object")
    valid = _enum(data, "valid", VALID_VALUES)
    reproduced = _enum(data, "reproduced", REPRODUCED_VALUES)
    effort = _enum(data, "effort", EFFORT_VALUES)
    evidence = _text(data.get("evidence"), MAX_EVIDENCE_CHARS)
    summary = _text(data.get("summary"), MAX_SUMMARY_CHARS)
    if reproduced == "yes" and not evidence:
        reproduced = "not_attempted"
    return Verdict(valid, reproduced, effort, evidence, summary)


def gate(verdict: Verdict) -> bool:
    """Whether the next stage may write a fix: valid, reproduced (or not applicable)
    and not large. Anything else is for a person to decide."""
    return (
        verdict.valid == "yes"
        and verdict.reproduced in ("yes", "not_applicable")
        and verdict.effort != "large"
    )


def verdict_schema() -> dict[str, Any]:
    """The JSON schema of the verdict, from the same constants the code validates against,
    so what the model is asked for and what is accepted cannot drift apart."""
    return {
        "type": "object",
        "properties": {
            "valid": {"enum": list(VALID_VALUES)},
            "reproduced": {"enum": list(REPRODUCED_VALUES)},
            "effort": {"enum": list(EFFORT_VALUES)},
            "evidence": {"type": "string", "maxLength": MAX_EVIDENCE_CHARS},
            "summary": {"type": "string", "maxLength": MAX_SUMMARY_CHARS},
        },
        "required": ["valid", "reproduced", "effort", "evidence", "summary"],
        "additionalProperties": False,
    }


def build_prompt(stage: str, notes: str) -> str:
    """The instruction file for a stage, followed by the repository's notes."""
    if stage not in STAGES:
        raise ValueError("unknown stage")
    instruction = (INSTRUCTION_DIR / f"{stage}_instruction.md").read_text(
        encoding="utf-8"
    )
    cleaned = strip_invisible_chars(notes).strip()[:MAX_NOTES_CHARS]
    body = cleaned or "(The maintainer gave no notes.)"
    return f"{instruction.rstrip()}\n\n## Repository notes\n\n{body}\n"


# --- what may be committed ---


def _path_problem(raw: str) -> str | None:
    if not raw:
        return "an empty path"
    if "\ufffd" in raw:
        return "a path that is not valid text"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return "a control character in a path"
    if raw.startswith("/"):
        return "an absolute path"
    if ".." in raw.split("/"):
        return "a parent-directory component in a path"
    parts = posixpath.normpath(raw).lower().split("/")
    if parts[0] in PROTECTED_TOP_DIRS:
        return f"a change under {parts[0]}"
    if parts[-1] in PROTECTED_NAMES:
        return "a change to a protected file"
    if parts[-1] in LOCKFILES:
        return "a change to a lockfile"
    return None


def check_changes(paths: list[str]) -> list[str]:
    """What is wrong with these changed paths, if anything. Empty means they may be
    committed. Each entry quotes a path only through log_text, so it is safe to print."""
    problems: list[str] = []
    if len(paths) > MAX_CHANGED_FILES:
        problems.append(
            f"{len(paths)} files changed; at most {MAX_CHANGED_FILES} are allowed"
        )
    for raw in paths:
        problem = _path_problem(raw)
        if problem:
            problems.append(f"{problem}: {log_text(raw)}")
    return problems


# --- what is written ---


def make_report(verdict: Verdict) -> Report:
    """Decide the labels and the comment from a validated verdict."""
    labels: list[str] = []
    if verdict.reproduced in REPRO_LABELS:
        labels.append(REPRO_LABELS[verdict.reproduced])
    if verdict.valid == "yes":
        labels.append(f"effort:{verdict.effort}")
    proceed = gate(verdict)

    lines = [
        "Automated assessment of this issue. It is a model's reading, not a decision.",
        "",
        f"- Valid: {verdict.valid}",
        f"- Reproduced: {verdict.reproduced}",
        f"- Effort: {verdict.effort}",
        "",
    ]
    for heading, text, limit in (
        ("Summary", verdict.summary, MAX_SUMMARY_CHARS),
        ("Evidence", verdict.evidence, MAX_EVIDENCE_CHARS),
    ):
        safe = sanitize_or_drop(text, limit, f"the {heading.lower()}") if text else None
        if safe:
            lines += [f"**{heading}**", "", safe, ""]
    lines += [
        "A draft pull request will be opened for a maintainer to review."
        if proceed
        else "No pull request will be opened from this; a maintainer should decide what to do.",
        "",
        MARKER,
    ]
    return Report("\n".join(lines), tuple(labels), proceed)


def has_marker(comments: list[Any]) -> bool:
    """Whether one of these comments is our own earlier assessment."""
    return any(
        isinstance(comment, dict) and MARKER in str(comment.get("body") or "")
        for comment in comments
    )


def apply_report(
    report: Report,
    repo: str,
    issue_number: int,
    token: str,
    repo_labels: frozenset[str],
) -> None:
    """Write the labels, then the comment.

    The comment goes last because it carries the marker that says the issue was assessed:
    if writing fails halfway, a second run starts over instead of stopping with nothing.
    """
    for label in report.labels:
        if label not in repo_labels:
            color, description = LABEL_STYLE[label]
            triage.create_label(repo, token, label, color, description)
    if report.labels:
        triage.github_request(
            "POST",
            repo,
            f"/issues/{issue_number}/labels",
            token,
            {"labels": list(report.labels)},
        )
    triage.github_request(
        "POST",
        repo,
        f"/issues/{issue_number}/comments",
        token,
        {"body": report.comment},
    )


# --- the commands ---


def _fail(message: str) -> NoReturn:
    print(f"::error::{message}")
    sys.exit(1)


def _settings() -> tuple[str, str, int]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    number = os.environ.get("ISSUE_NUMBER", "")
    if not REPO_PATTERN.fullmatch(repo):
        _fail("GITHUB_REPOSITORY is not in owner/name form.")
    if not token:
        _fail("GITHUB_TOKEN is not set.")
    if not re.fullmatch(r"[0-9]+", number):
        _fail("ISSUE_NUMBER is not an issue number.")
    return repo, token, int(number)


def _set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def run_fetch() -> None:
    repo, token, number = _settings()
    raw = triage.github_request("GET", repo, f"/issues/{number}", token)
    actor = os.environ.get("ACTOR_LOGIN", "")
    permission = actor_permission(repo, actor, token) if actor else None
    try:
        issue = check_issue(raw, permission)
    except NotTrusted as reason:
        _fail(f"Not starting a run for #{number}: {reason}.")
    write_issue_file(issue_markdown(issue))
    print(f"Read issue #{issue.number}.")


def run_report() -> None:
    repo, token, number = _settings()
    comments = triage.github_request(
        "GET", repo, f"/issues/{number}/comments?per_page=100", token
    )
    if has_marker(comments if isinstance(comments, list) else []):
        print(f"#{number} has already been assessed; leaving it alone.")
        _set_output("proceed", "false")
        return
    try:
        verdict = parse_verdict(os.environ.get("VERDICT", ""))
    except ValueError as error:
        # Only our own static message or the parser's, never the model's text.
        print(f"::warning::No usable verdict for #{number} ({type(error).__name__}).")
        _set_output("proceed", "false")
        return
    report = make_report(verdict)
    labels = triage.github_request("GET", repo, "/labels?per_page=100", token) or []
    repo_labels = frozenset(
        label["name"]
        for label in labels
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    )
    apply_report(report, repo, number, token, repo_labels)
    print(
        f"Assessment of #{number}: valid={verdict.valid} reproduced={verdict.reproduced} "
        f"effort={verdict.effort} proceed={str(report.proceed).lower()}"
    )
    _set_output("proceed", str(report.proceed).lower())


def run_schema() -> None:
    _set_output("schema", json.dumps(verdict_schema(), separators=(",", ":")))


def run_prompt(stage: str) -> None:
    prompt = build_prompt(stage, os.environ.get("REPO_NOTES", ""))
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(prompt)
        return
    # A multi-line output needs a delimiter line the text cannot contain.
    delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
    while delimiter in prompt:
        delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"prompt<<{delimiter}\n{prompt}\n{delimiter}\n")


def run_check_changes() -> None:
    raw = sys.stdin.buffer.read()
    paths = [
        chunk.decode("utf-8", errors="replace") for chunk in raw.split(b"\0") if chunk
    ]
    if not paths:
        _fail("The agent made no changes.")
    problems = check_changes(paths)
    if problems:
        for problem in problems[:5]:
            print(f"::error::Refusing to commit: {problem}")
        _fail(
            f"{len(problems)} problem(s) with the changed files; nothing was committed."
        )
    print(f"{len(paths)} changed file(s); none is protected.")


COMMANDS = {
    "fetch": run_fetch,
    "schema": run_schema,
    "report": run_report,
    "check-changes": run_check_changes,
}


def main(argv: list[str]) -> None:
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    handler: Callable[[], None]
    if command == "prompt" and len(args) == 1 and args[0] in STAGES:
        handler = partial(run_prompt, args[0])
    elif command in COMMANDS and not args:
        handler = COMMANDS[command]
    else:
        names = "|".join([*COMMANDS, "prompt {" + "|".join(STAGES) + "}"])
        print(f"usage: implement.py {{{names}}}", file=sys.stderr)
        sys.exit(2)
    try:
        handler()
    except urllib.error.HTTPError as error:
        _fail(f"GitHub answered {error.code}; nothing more was done.")


if __name__ == "__main__":
    main(sys.argv[1:])
