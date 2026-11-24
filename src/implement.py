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
  check-tools    Refuse a list of allowed commands (COMMAND_TOOLS) that is broad enough
                 to hand the model a shell, the network or the environment.
  check-cap      Say whether the repository has used up its assessments for the day.
  notify KIND    Leave the one comment for a run the cap stopped (capped) or a run that
                 failed (failed), at most once per issue.

The model never decides what is written. It returns a verdict; this code checks every
field against a fixed contract, picks the labels from a fixed table, and builds the
comment from a template around sanitized text.
"""

import json
import os
import posixpath
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

# A claim of having run the problem, or of having read the fault, needs something to
# point at; without it the honest word is not_attempted.
CLAIMS_NEEDING_EVIDENCE = ("yes", "by_reading")
VALID_VALUES = ("yes", "no", "unclear")
REPRODUCED_VALUES = ("yes", "by_reading", "no", "not_applicable", "not_attempted")
EFFORT_VALUES = ("small", "medium", "large")

# The only labels this code ever writes, with the colour and description they are created
# with if the repository lacks them. invalid, wontfix and spam are not here: those are
# what a hostile issue would ask for, so they stay with people.
LABEL_STYLE = {
    "repro:confirmed": ("0e8a16", "The assessment reproduced this issue"),
    "repro:failed": ("d93f0b", "The assessment could not reproduce this issue"),
    "repro:not-applicable": ("c5def5", "Reproduction does not apply to this issue"),
    "repro:by-reading": (
        "fbca04",
        "The assessment confirmed this issue by reading the code, without running it",
    ),
    "effort:small": ("c2e0c6", "Assessed as a small change"),
    "effort:medium": ("fef2c0", "Assessed as a medium-sized change"),
    "effort:large": ("f9d0c4", "Assessed as a large change"),
}
ALLOWED_LABELS = frozenset(LABEL_STYLE)
REPRO_LABELS = {
    "yes": "repro:confirmed",
    "no": "repro:failed",
    "not_applicable": "repro:not-applicable",
    "by_reading": "repro:by-reading",
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
    {
        ".gitmodules",
        ".gitattributes",
        ".agent-issue.md",
        ".agent-change.md",
        "codeowners",
    }
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

# The commands a repository allows the model to run (command_tools) are the widest lever
# in the design, so a careless pattern is refused before any model runs. This is a guard
# against a mistake, not a sandbox: a narrow pattern that runs code, such as a test
# runner, still runs the code the model wrote, which the design says plainly.
DENIED_COMMANDS = frozenset(
    {
        # shells, and ways of running something else
        "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "sudo", "su", "doas",
        "xargs", "eval", "exec", "source", ".",
        # the environment, where the token is
        "env", "printenv", "set", "export", "declare",
        # the network
        "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "ftp", "telnet",
        "socat",
        # git and gh: pushing and opening pull requests is the workflow's job
        "git", "gh",
        # ways of reading any file, such as /proc/self/environ
        "cat", "tac", "head", "tail", "less", "more", "od", "xxd", "strings", "base64",
    }
)  # fmt: skip
# An interpreter with nothing after it runs anything the model types.
BARE_INTERPRETER = re.compile(r"python[0-9.]*|node(js)?|ruby|perl|php|deno|bun|lua")
BASH_PATTERN = re.compile(r"Bash\(([^()]*)\)")

# The cap counts assessments, the comments that say a run reached the model, written by
# the workflow's own bot: anyone can type the marker into a comment, but not as the bot.
# It is a soft limit. Runs for different issues start side by side, so a burst can pass
# the check together before any of them has left a comment.
DEFAULT_DAILY_CAP = 5
CAP_WINDOW = timedelta(hours=24)
MAX_COMMENT_PAGES = 10
BOT_LOGIN = "github-actions[bot]"
CAPPED_MARKER = "<!-- agent-implement:capped -->"
FAILED_MARKER = "<!-- agent-implement:failed -->"
NOTICES = ("capped", "failed")

ISSUE_FILE = Path(".agent-issue.md")
CHANGE_FILE = Path(".agent-change.md")
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


def keep_out_of_commits(name: str, exclude: Path = EXCLUDE_FILE) -> None:
    """Tell git to ignore a file in this clone, so no later `git add -A` can commit it."""
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if name not in existing.splitlines():
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(f"{existing}{separator}{name}\n", encoding="utf-8")


def write_issue_file(
    text: str, path: Path = ISSUE_FILE, exclude: Path = EXCLUDE_FILE
) -> None:
    """Write the issue file and keep it out of any commit, whatever adds files later."""
    keep_out_of_commits(path.name, exclude)
    path.write_text(text, encoding="utf-8")


# --- the commit message and pull request text ---

# The types a change may be given. They are the conventional-commit types a fix stage can
# honestly mean; anything else, and "chore" in particular, is for a person to choose.
CHANGE_TYPES = ("fix", "feat", "test", "refactor", "docs", "perf")
MAX_COMMIT_TITLE_CHARS = 71  # a commit title is kept under 72 characters
COMMIT_WRAP = 70
MAX_CHANGE_BODY_CHARS = 3000
MAX_NOT_DONE_CHARS = 400
MAX_CHANGE_FILE_CHARS = 20_000
_HEADER_KEYS = {
    "type": "kind",
    "title": "title",
    "not done": "not_done",
    "not-done": "not_done",
}
_NOTHING_LEFT = frozenset({"", "none", "nothing", "n/a", "na", "-"})
_TITLE_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S*", re.IGNORECASE)
# What a title may hold: words, spaces and plain punctuation. Anything that renders, links,
# mentions or escapes is dropped, not escaped, because a title has no room for markup.
_TITLE_UNSAFE = re.compile(r"[^\w .,'\":;()/+=?!-]")
_TITLE_PREFIX = re.compile(
    r"^(?:fix|feat|test|refactor|docs|perf|chore|build|ci|style|revert)"
    r"(?:\([^)]*\))?!?:\s*",
    re.IGNORECASE,
)
_LIST_ITEM = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
_FENCE_LINE = re.compile(r"^\s{0,3}(?:`{3,}|~{3,})")


@dataclass(frozen=True)
class Change:
    """What the model said about its change, after every field has been checked."""

    kind: str
    title: str
    not_done: str
    body: str


def _clean_title(value: str) -> str:
    text = _TITLE_URL.sub(" ", strip_invisible_chars(value))
    text = " ".join(_TITLE_UNSAFE.sub(" ", text).split())
    text = _TITLE_PREFIX.sub("", text).rstrip(" .")
    if not text:
        raise ValueError("the title has nothing left once cleaned")
    return text


def parse_change(raw: str) -> Change:
    """Read the model's `.agent-change.md`. Anything outside the contract rejects all of it.

    A short header of `Type:`, `Title:` and an optional `Not done:` line, a blank line, then
    the description. The type must be one of CHANGE_TYPES. The title is reduced to plain
    words; the not-done line and the description are made safe to post, as the assessment
    comment is. The messages here are fixed text, so they can be logged without repeating
    anything the model wrote."""
    text = strip_invisible_chars(raw).replace("\r\n", "\n").strip()
    parts = re.split(r"\n[ \t]*\n", text, maxsplit=1)
    if len(parts) != 2:
        raise ValueError(
            "there is no blank line between the header and the description"
        )
    header, description = parts
    fields: dict[str, str] = {}
    for line in header.split("\n"):
        key, colon, value = line.partition(":")
        name = _HEADER_KEYS.get(" ".join(key.lower().split())) if colon else None
        if name is None:
            raise ValueError("a header line is not Type, Title or Not done")
        if name in fields:
            raise ValueError("a header line is given twice")
        fields[name] = value.strip()
    kind = fields.get("kind", "").lower()
    if kind not in CHANGE_TYPES:
        raise ValueError("the type is not one of the allowed types")
    if not fields.get("title"):
        raise ValueError("there is no title")
    title = _clean_title(fields["title"])

    not_done = ""
    said = fields.get("not_done", "")
    if said.strip(" .").lower() not in _NOTHING_LEFT:
        safe = sanitize_or_drop(said, MAX_NOT_DONE_CHARS, "the not-done line")
        if safe is None:
            raise ValueError("the not-done line could not be made safe")
        not_done = safe

    body = sanitize_or_drop(
        description, MAX_CHANGE_BODY_CHARS, "the change description"
    )
    if not body:
        raise ValueError("the description is empty or could not be made safe")
    return Change(kind, title, not_done, body)


def commit_title(change: Change) -> str:
    """`type: subject`, cut at a word so the whole is under 72 characters."""
    budget = MAX_COMMIT_TITLE_CHARS - len(change.kind) - 2
    title = change.title
    if len(title) > budget:
        cut = title[:budget]
        if title[budget] != " " and " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        title = cut.rstrip(" .,;:-")
    return f"{change.kind}: {title}"


def _closing(change: Change, number: int) -> str:
    # Code decides this, never the model's prose: a change that says part is left undone
    # must not close the issue when it is merged.
    return f"{'Refs' if change.not_done else 'Closes'} #{number}"


def _wrap_for_commit(text: str) -> str:
    """Wrap prose under 72 columns, keeping list items indented and code blocks as written."""
    out: list[str] = []
    in_fence = False
    for line in text.strip("\n").split("\n"):
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            out.append(line)
        elif in_fence or not line.strip():
            out.append(line)
        else:
            item = _LIST_ITEM.match(line)
            marker, rest = (item.group(1), item.group(2)) if item else ("", line)
            out.append(
                textwrap.fill(
                    rest,
                    width=COMMIT_WRAP,
                    initial_indent=marker,
                    subsequent_indent=" " * len(marker),
                    break_on_hyphens=False,
                )
            )
    return "\n".join(out)


def commit_message(change: Change, number: int) -> str:
    parts = [commit_title(change), _wrap_for_commit(change.body)]
    if change.not_done:
        parts.append(_wrap_for_commit(f"Not done: {change.not_done}"))
    parts.append(_closing(change, number))
    return "\n\n".join(parts) + "\n"


_PULL_REQUEST_NOTICE = (
    "Opened automatically from #{n}. A model wrote this change{and_text}; "
    "nothing here has been reviewed, merged or approved by it. Read the change and its "
    "regression test before anything else."
)


def pull_request_body(change: Change, number: int) -> str:
    parts = [change.body]
    if change.not_done:
        parts.append(f"**Not done:** {change.not_done}")
    parts.append(
        _PULL_REQUEST_NOTICE.format(n=number, and_text=" and this description")
    )
    parts.append(_closing(change, number))
    return "\n\n".join(parts)


MAX_BRANCH_SLUG_CHARS = 40


def branch_slug(title: str) -> str:
    """Lowercase words joined by hyphens, cut at a word, for the end of a branch name.

    Only [a-z0-9] and single hyphens can come out, so the result is safe in a ref name and
    in a shell word whatever the title held; a title with no such characters gives ''."""
    words = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(words) <= MAX_BRANCH_SLUG_CHARS:
        return words
    cut = words[:MAX_BRANCH_SLUG_CHARS]
    if words[MAX_BRANCH_SLUG_CHARS] != "-" and "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-")


def branch_name(number: int, change: Change | None) -> str:
    """`agent/issue-N`, then the words of the change's title when there are any. The start
    never changes: repositories skip, and look for, branches that begin with it."""
    slug = branch_slug(change.title) if change else ""
    return f"agent/issue-{number}" + (f"-{slug}" if slug else "")


def generic_change_texts(number: int) -> tuple[str, str, str]:
    """The title, commit message and body used when the model's file cannot be used."""
    title = f"fix: address issue #{number}"
    body = _PULL_REQUEST_NOTICE.format(n=number, and_text="") + f"\n\nCloses #{number}"
    return title, f"{title}\n", body


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
    cannot add other fields. A claim to have run the problem, or to have read the fault in
    the code, needs evidence to point at: with none, it is not believed.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("the verdict is not an object")
    valid = _enum(data, "valid", VALID_VALUES)
    reproduced = _enum(data, "reproduced", REPRODUCED_VALUES)
    effort = _enum(data, "effort", EFFORT_VALUES)
    evidence = _text(data.get("evidence"), MAX_EVIDENCE_CHARS)
    summary = _text(data.get("summary"), MAX_SUMMARY_CHARS)
    if reproduced in CLAIMS_NEEDING_EVIDENCE and not evidence:
        reproduced = "not_attempted"
    return Verdict(valid, reproduced, effort, evidence, summary)


def gate(verdict: Verdict) -> bool:
    """Whether the next stage may write a fix: valid, not large, and the problem shown by
    running it, confirmed by reading the code, or not something that can be reproduced.
    Anything else is for a person to decide. A reading is weaker than a run, which is why
    the fix stage has to show the problem with a failing test before it changes anything."""
    return (
        verdict.valid == "yes"
        and verdict.reproduced in ("yes", "by_reading", "not_applicable")
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


def check_command_tools(tools: str) -> list[str]:
    """What is wrong with this list of allowed commands, if anything.

    Each entry must be a `Bash(...)` pattern naming one command, and not one that gives
    the model a shell, the network, git, the environment or a way to read any file; nor
    an interpreter with nothing after it. Anything else, including other tools, is
    refused: the tools every run has are written in the actions themselves.
    """
    if any(ch in tools for ch in '"\n\r'):
        return ["the list contains a double quote or a line break"]
    if not tools.strip():
        return ["no command is allowed"]
    problems: list[str] = []
    for entry in (part.strip() for part in tools.split(",")):
        match = BASH_PATTERN.fullmatch(entry)
        if not match:
            problems.append(f"not a Bash(...) pattern: {log_text(entry)}")
            continue
        # ":" and "*" only separate or widen what is allowed; the words are what it is.
        words = match.group(1).replace(":", " ").replace("*", " ").split()
        if not words:
            problems.append(f"too broad, it allows any command: {log_text(entry)}")
            continue
        command = posixpath.basename(words[0]).lower()
        if command in DENIED_COMMANDS:
            problems.append(f"it would give the model {command}: {log_text(entry)}")
        elif len(words) == 1 and BARE_INTERPRETER.fullmatch(command):
            problems.append(f"an interpreter with no restriction: {log_text(entry)}")
    return problems


def check_changes(paths: list[str]) -> list[str]:
    """What is wrong with these changed paths, if anything. Empty means they may be
    committed. Each entry quotes a path only through log_text, so it is safe to print."""
    problems: list[str] = []
    if len(paths) > MAX_CHANGED_FILES:
        problems.append(
            f"{len(paths)} files changed; at most {MAX_CHANGED_FILES} are allowed"
        )
    for raw in paths:
        # A link is judged after the other checks: they are what make the path safe to
        # look at on disk (relative, no control characters).
        problem = _path_problem(raw)
        if problem is None and os.path.islink(raw):
            problem = "a symbolic link"
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
        "A pull request will be opened for a maintainer to review."
        if proceed
        else "No pull request will be opened from this; a maintainer should decide what to do.",
        "",
        MARKER,
    ]
    return Report("\n".join(lines), tuple(labels), proceed)


def has_marker(comments: list[Any], marker: str = MARKER) -> bool:
    """Whether one of these comments is one of our own earlier ones."""
    return any(
        isinstance(comment, dict) and marker in str(comment.get("body") or "")
        for comment in comments
    )


# --- the daily cap ---


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_cap(raw: str) -> int:
    """The number of assessments allowed per day: empty means the default, 0 means none."""
    text = raw.strip()
    if not text:
        return DEFAULT_DAILY_CAP
    if not re.fullmatch(r"[0-9]{1,4}", text):
        raise ValueError("the daily cap is not a number from 0 to 9999")
    return int(text)


def _is_recent_assessment(comment: Any, since: datetime) -> bool:
    if not isinstance(comment, dict):
        return False
    user = comment.get("user")
    if not isinstance(user, dict) or user.get("login") != BOT_LOGIN:
        return False
    if MARKER not in str(comment.get("body") or ""):
        return False
    try:
        created = datetime.strptime(
            str(comment.get("created_at")), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return created >= since


def recent_assessments(repo: str, token: str, since: datetime, limit: int) -> int:
    """How many assessments the repository has had since `since`, counting up to `limit`.

    GitHub's `since` filter is on when a comment was last edited, so each comment's own
    creation time is checked as well.
    """
    stamp = urllib.parse.quote(since.strftime("%Y-%m-%dT%H:%M:%SZ"), safe="")
    count = 0
    for page in range(1, MAX_COMMENT_PAGES + 1):
        batch = triage.github_request(
            "GET",
            repo,
            f"/issues/comments?since={stamp}&per_page=100&page={page}",
            token,
        )
        if not isinstance(batch, list):
            break
        for comment in batch:
            if _is_recent_assessment(comment, since):
                count += 1
                if count >= limit:
                    return count
        if len(batch) < 100:
            break
    return count


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
    # The model writes this one later; it must be ignored before it exists.
    keep_out_of_commits(CHANGE_FILE.name)
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


def _cap_from_environment() -> int:
    try:
        return parse_cap(os.environ.get("DAILY_CAP", ""))
    except ValueError as reason:
        _fail(f"DAILY_CAP: {reason}.")


def run_check_cap() -> None:
    repo, token, number = _settings()
    cap = _cap_from_environment()
    capped = (
        cap == 0 or recent_assessments(repo, token, _now() - CAP_WINDOW, cap) >= cap
    )
    if capped:
        print(
            f"The daily limit of {cap} assessments has been reached; #{number} waits."
        )
    _set_output("capped", str(capped).lower())


def _run_url(repo: str) -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+", server) or not re.fullmatch(
        r"[0-9]+", run_id
    ):
        return None
    return f"{server}/{repo}/actions/runs/{run_id}"


def run_notify(kind: str) -> None:
    repo, token, number = _settings()
    marker = CAPPED_MARKER if kind == "capped" else FAILED_MARKER
    comments = triage.github_request(
        "GET", repo, f"/issues/{number}/comments?per_page=100", token
    )
    if has_marker(comments if isinstance(comments, list) else [], marker):
        print(f"#{number} has already been told ({kind}); leaving it alone.")
        return
    if kind == "capped":
        cap = _cap_from_environment()
        body = (
            f"This issue was not assessed: the repository has reached its limit of {cap} "
            "automated assessments in the last 24 hours, so no model was run on it. "
            "Apply `ready-to-implement` again later to try again.\n\n"
            f"{CAPPED_MARKER}"
        )
    else:
        url = _run_url(repo)
        log = f" The log is here: {url}" if url else ""
        body = (
            "A run for this issue failed before it finished, so no pull request was "
            f"opened by it. Nothing was merged, approved or closed.{log}\n\n"
            "To try again, a maintainer removes any `repro:` labels, deletes the "
            f"assessment comment and any `agent/issue-{number}` or "
            f"`agent/issue-{number}-*` branch, "
            f"and applies `ready-to-implement` again.\n\n{FAILED_MARKER}"
        )
    triage.github_request(
        "POST", repo, f"/issues/{number}/comments", token, {"body": body}
    )
    print(f"Left the {kind} comment on #{number}.")


def run_check_tools() -> None:
    problems = check_command_tools(os.environ.get("COMMAND_TOOLS", ""))
    if problems:
        for problem in problems[:5]:
            print(f"::error::command_tools: {problem}")
        _fail("command_tools would give the model more than it should have.")
    print("command_tools: every entry names one narrow command.")


def run_describe(out_dir: str, number: str) -> None:
    """Write the commit title, commit message and pull request body for the fix stage.

    The text is the model's, read under the contract and made safe; if the file is missing
    or outside the contract the generic text is written instead, because a wording problem
    must not cost the fix. Nothing the model wrote is printed."""
    if not re.fullmatch(r"[0-9]+", number):
        _fail("Not an issue number.")
    issue = int(number)
    change: Change | None = None
    try:
        with open(CHANGE_FILE, encoding="utf-8", errors="replace") as f:
            raw = f.read(MAX_CHANGE_FILE_CHARS)
        change = parse_change(raw)
        title, message, body = (
            commit_title(change),
            commit_message(change, issue),
            pull_request_body(change, issue),
        )
        print("Using the change description the model wrote.")
    except FileNotFoundError:
        print("No change description was written; using the generic text.")
        title, message, body = generic_change_texts(issue)
    except ValueError as reason:
        print(
            f"The change description was not usable ({reason}); using the generic text."
        )
        title, message, body = generic_change_texts(issue)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "title.txt").write_text(title, encoding="utf-8")
    (out / "commit.txt").write_text(message, encoding="utf-8")
    (out / "body.md").write_text(body, encoding="utf-8")
    (out / "branch.txt").write_text(branch_name(issue, change), encoding="utf-8")


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
    "check-tools": run_check_tools,
    "check-cap": run_check_cap,
}


def main(argv: list[str]) -> None:
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    handler: Callable[[], None]
    if command == "prompt" and len(args) == 1 and args[0] in STAGES:
        handler = partial(run_prompt, args[0])
    elif command == "notify" and len(args) == 1 and args[0] in NOTICES:
        handler = partial(run_notify, args[0])
    elif command == "describe" and len(args) == 2:
        handler = partial(run_describe, args[0], args[1])
    elif command in COMMANDS and not args:
        handler = COMMANDS[command]
    else:
        names = "|".join(
            [
                *COMMANDS,
                "prompt {" + "|".join(STAGES) + "}",
                "notify {" + "|".join(NOTICES) + "}",
                "describe DIR NUMBER",
            ]
        )
        print(f"usage: implement.py {{{names}}}", file=sys.stderr)
        sys.exit(2)
    try:
        handler()
    except urllib.error.HTTPError as error:
        _fail(f"GitHub answered {error.code}; nothing more was done.")


if __name__ == "__main__":
    main(sys.argv[1:])
