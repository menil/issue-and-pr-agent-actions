#!/usr/bin/env python3
"""Replay the issue triage over issues that already exist, and compare with a person.

Read-only: it fetches the issues of the given repositories, asks the model about each
one exactly as a live run would (through `triage.ask_model`, showing only the issues
that came before it), and prints a markdown report of what the model said, what it
would have written, and what a person had decided. Nothing is written to GitHub.

    GH_TOKEN=$(gh auth token) python scripts/replay_triage.py owner/repo [owner/repo ...]

Use it to see how the prompt behaves on real issues before relying on it, and again
whenever the prompt changes. `--dry-run` fetches and builds everything but does not
call the model, so it needs no provider credentials.
"""

import argparse
import contextlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

# Run as `python scripts/replay_triage.py`, with no PYTHONPATH: the modules it reuses
# live in ../src, beside the action's own script.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from llm import check_credentials  # noqa: E402
from sanitize import sanitize_llm_text  # noqa: E402
from triage import (  # noqa: E402
    TYPE_LABELS,
    Decision,
    Plan,
    ask_model,
    candidate_issues,
    github_request,
    make_plan,
    parse_event,
)

REPO_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
DEFAULT_LIMIT = 50
DEFAULT_CANDIDATES = 50


@dataclass(frozen=True)
class Row:
    """One replayed issue: what a person set, and what the model would do."""

    repo: str
    number: int
    title: str
    human_type: str | None
    human_labels: tuple[str, ...]
    decision: Decision | None
    plan: Plan | None
    error: str


def human_type(labels: frozenset[str] | tuple[str, ...]) -> str | None:
    """The type a person gave the issue, if one of the labels is a type."""
    return next((name for name in sorted(labels) if name in TYPE_LABELS), None)


def _created(raw: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(raw["created_at"]).replace("Z", "+00:00"))


MAX_PAGES = 10  # 1,000 issues and pull requests; the issues API mixes the two


def _all_issues(repo: str, token: str) -> list[dict[str, Any]]:
    """Every issue of a repository, oldest first, paging through the pull requests too."""
    issues: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = github_request(
            "GET",
            repo,
            f"/issues?state=all&per_page=100&sort=created&direction=asc&page={page}",
            token,
        )
        batch = batch if isinstance(batch, list) else []
        issues += [
            raw for raw in batch if isinstance(raw, dict) and "pull_request" not in raw
        ]
        if len(batch) < 100:
            break
    else:
        print(
            f"Note: only the first {MAX_PAGES * 100} items of {repo} were read.",
            file=sys.stderr,
        )
    return issues


def replay_repo(
    repo: str, token: str, provider: str, limit: int, dry_run: bool
) -> list[Row]:
    """Replay the first `limit` issues of one repository, oldest first."""
    issues = _all_issues(repo, token)
    repo_labels = frozenset(
        label["name"]
        for label in github_request("GET", repo, "/labels?per_page=100", token) or []
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    )

    rows: list[Row] = []
    for raw in issues[:limit]:
        _, issue = parse_event({"action": "opened", "issue": raw})
        # A live run is offered the earlier issues, most recently updated first (the
        # selection itself keeps only the earlier ones); the closest stand-in for
        # "recently updated" at the time is the newest number.
        newest_first = sorted(issues, key=lambda other: other["number"], reverse=True)
        candidates = candidate_issues(
            newest_first, issue.number, DEFAULT_CANDIDATES, _created(raw)
        )
        decision: Decision | None = None
        error = "dry run: the model was not asked" if dry_run else ""
        if not dry_run:
            decision, last_error = ask_model(provider, repo, issue, candidates)
            if decision is None:
                error = f"no usable answer ({type(last_error).__name__})"
        rows.append(
            Row(
                repo=repo,
                number=issue.number,
                title=issue.title,
                human_type=human_type(issue.labels),
                human_labels=tuple(sorted(issue.labels)),
                decision=decision,
                plan=make_plan(decision, repo_labels) if decision else None,
                error=error,
            )
        )
    return rows


def _cell(text: str, max_chars: int) -> str:
    """Text for a table cell: cut to length, made safe like any model text, one line."""
    shown = text if len(text) <= max_chars else text[: max_chars - 1] + "\u2026"
    return " ".join(sanitize_llm_text(shown, max_chars).split()) or "-"


def render_report(rows: list[Row]) -> str:
    """The markdown report. Everything that came from an issue or the model is
    sanitized, because a report is read as markdown (for example in a job summary)."""
    answered = [row for row in rows if row.decision is not None]
    comparable = [row for row in answered if row.human_type is not None]
    agreed = [
        row
        for row in comparable
        if row.decision and row.decision.type == row.human_type
    ]
    asked = [row for row in answered if row.decision and row.decision.needs_info]

    lines = [
        "## Triage replay",
        "",
        f"{len(rows)} issues replayed, {len(answered)} answered by the model.",
    ]
    if comparable:
        lines.append(
            f"Type matches a person's label on {len(agreed)} of {len(comparable)} "
            "issues that had one."
        )
    lines += [
        f"It would ask for more information on {len(asked)}.",
        "",
        "| Issue | Title | A person set | The model said | Asks for info | Duplicate of | Would write |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        decision = row.decision
        if decision is None:
            model = _cell(row.error or "no answer", 80)
            asks, duplicate, writes = "-", "-", "-"
        else:
            model = decision.type or "none"
            asks = "yes" if decision.needs_info else "no"
            duplicate = f"#{decision.duplicate_of}" if decision.duplicate_of else "-"
            labels = row.plan.labels if row.plan else ()
            writes = ", ".join(labels) if labels else "nothing"
        match = (
            ""
            if decision is None or row.human_type is None
            else (" (match)" if decision.type == row.human_type else " (differs)")
        )
        lines.append(
            f"| {_cell(f'{row.repo}#{row.number}', 60)} | {_cell(row.title, 70)} "
            f"| {row.human_type or '-'} | {_cell(model, 80)}{match} | {asks} "
            f"| {duplicate} | {_cell(writes, 60)} |"
        )

    comments = [row for row in answered if row.plan and row.plan.comment]
    if comments:
        lines += ["", "### Comments it would post", ""]
        for row in comments:
            assert row.plan is not None and row.plan.comment is not None
            lines.append(f"**{_cell(f'{row.repo}#{row.number}', 60)}**")
            lines.append("")
            lines.append("> " + row.plan.comment.replace("\n", "\n> "))
            lines.append("")
    reasons = [row for row in answered if row.decision and row.decision.reason]
    if reasons:
        lines += ["### Why, in the model's words", ""]
        for row in reasons:
            assert row.decision is not None
            lines.append(
                f"- {_cell(f'{row.repo}#{row.number}', 60)}: {_cell(row.decision.reason, 300)}"
            )
    return "\n".join(lines) + "\n"


def _repo_argument(value: str) -> str:
    if not REPO_PATTERN.match(value):
        raise argparse.ArgumentTypeError(f"'{value}' is not in owner/repo form")
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repos", nargs="+", type=_repo_argument, metavar="owner/repo")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="issues per repo"
    )
    parser.add_argument("--dry-run", action="store_true", help="do not call the model")
    parser.add_argument("--json", metavar="PATH", help="also write the raw rows here")
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print(
            "Error: set GH_TOKEN, for example GH_TOKEN=$(gh auth token).",
            file=sys.stderr,
        )
        sys.exit(1)
    provider = os.environ.get("PROVIDER", "auto").strip().lower()
    if not args.dry_run:
        check_credentials(provider)

    rows: list[Row] = []
    # The providers log to stdout; keep that out of the report, which is the only thing
    # on stdout so it can be redirected into a file or a job summary.
    with contextlib.redirect_stdout(sys.stderr):
        for repo in args.repos:
            rows += replay_repo(repo, token, provider, args.limit, args.dry_run)
    print(render_report(rows))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(row) for row in rows], f, indent=2)


if __name__ == "__main__":
    main()
