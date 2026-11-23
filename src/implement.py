#!/usr/bin/env python3
"""Shared code of the issue implementation actions (design: docs/implement-action.md).

Two commands, run by the composite actions under implement/:

  fetch   Read an issue, refuse it unless a trusted person stands behind it, and write
          it to a file for the model to read.
  report  Validate the model's verdict, write the labels and the one comment, and say
          whether the next stage may go ahead.

The model never decides what is written. It returns a verdict; this code checks every
field against a fixed contract, picks the labels from a fixed table, and builds the
comment from a template around sanitized text.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import triage
from sanitize import sanitize_or_drop, strip_invisible_chars

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

    The author counts if they are an owner, member or collaborator. Otherwise a person
    with write access who applied the label (`actor_permission`) can vouch for it. A pull
    request and a bot's issue are never accepted, whoever vouches.
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
    association = raw.get("author_association")
    if (
        association not in TRUSTED_ASSOCIATIONS
        and actor_permission not in WRITE_PERMISSIONS
    ):
        # Neither value is quoted: the first comes from GitHub, but nothing here depends on that.
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
    try:
        try:
            issue = check_issue(raw)
        except NotTrusted:
            if not actor:
                raise
            issue = check_issue(raw, actor_permission(repo, actor, token))
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


COMMANDS = {"fetch": run_fetch, "report": run_report}


def main(argv: list[str]) -> None:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print(f"usage: implement.py {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        sys.exit(2)
    try:
        COMMANDS[argv[0]]()
    except urllib.error.HTTPError as error:
        _fail(f"GitHub answered {error.code}; nothing more was done.")


if __name__ == "__main__":
    main(sys.argv[1:])
