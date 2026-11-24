# Issue implementation: design

Status: the spike is done and the gate passed (see "What the spike found"); the
implementation is not built yet. Items marked **[TBD]** are still open and listed again
at the end.

## Goal

For an issue the maintainer trusts, work out whether it is valid and whether it
reproduces, and if both hold, write a fix and open a **draft** pull request for a
human to review. It is free to run: the Claude subscription token only, no
pay-per-token key, and no OpenRouter fallback for this stage.

Not goals: merging anything, approving anything, closing or rejecting issues,
working on issues from people without write access unless a maintainer says so,
or changing the repository's workflows.

## What it builds on

It does not write an agent. It wraps [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action)
(`claude_code_oauth_token` is a supported input). What the wrapper adds is the
decision of **who may start a run, what the run may do, and what code, not the
model, writes to the repository**. Facts about that action that the design relies on,
from its documentation:

- It checks that whoever *triggered* the event has write access to the repository.
  For `issues: opened` that is the author; for `issues: labeled` it is the person who
  applied the label, **not** the author. It still reads the body of an issue someone
  else wrote ("review it first").
- By default it only creates a branch and links to a prefilled PR page. It opens a
  pull request only if the prompt asks and `gh pr create` is allowed.
- It runs in auto permission mode by default: a safety review decides any tool call
  that no allow rule covers.
- `allowed_non_write_users` and `allowed_bots` switch its write-access check off. This
  design never sets either.

## Decisions

| | Decision |
|---|---|
| Who starts a run | By default only an issue **opened by someone with write access** (`author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`, checked in the workflow's `if:` and again by `fetch`). When a **label** starts it, the person who applied the label must have write access, whoever wrote the issue, because they are the one spending the run. Anyone else's issue starts nothing; see "Other people's issues". |
| Automatic or not | Opt-in per repository by a variable, `AGENT_AUTO=true`, default off. With it off, nothing starts until a maintainer applies `ready-to-implement`. |
| Stages | Three jobs, because a job's permissions are fixed for all its steps: **assess** (model, read-only), **report** (code only, `issues: write`), then **fix** (model, `contents` and `pull-requests: write`), only if the gate passed. |
| What the model decides | A verdict and, in stage two, the code of a draft PR. Never a label, a comment's markdown, a branch name or a command outside the allowlist. |
| Credentials | **Route A**: the workflow's own `GITHUB_TOKEN` and the Claude subscription token, and nothing else. No personal access token and no GitHub App is installed. The cost is one click per agent pull request (see below). |
| Where it lives | Three composite actions in this repo, one per stage (`implement/assess`, `implement/report`, `implement/fix`), versioned with the other actions and used like `triage/`. Each repository has a thin wrapper workflow that declares the three jobs, their `permissions:` and the `AGENT_AUTO` rule, and calls one action per job. Not a reusable workflow: the permissions would then sit in this repository, out of sight of the repository that is handing them out. Not just files the wrapper checks out: the validation and the label map must be one tested copy. |

## Flow

1. **Guards (no model).** Skip if the issue is from a bot, carries `no-agent`, or
   already has a `repro:*` label or an open `agent/issue-N` branch or PR; skip a
   second run for the same issue (concurrency group per issue number).
   Opened by a trusted author and `AGENT_AUTO` is on, or `ready-to-implement` was
   applied by a person with write access: continue. Otherwise stop.
2. **Assess** (job `assess`). Permissions: `contents: read`, `issues: read`. A checkout of the
   default branch, the model, and a small allowlist of tools: read files, search,
   and run the repository's own test or build command named in the instruction file.
   It returns a verdict as JSON, through `--json-schema`:
   `{valid, reproduced, effort, evidence, summary}`. The job's only output is that
   verdict, as a string.
3. **Validate the verdict in code** (job `report`: `issues: write`, no checkout, no
   model). Anything outside the contract is dropped. The script, not the model,
   applies the labels and posts one comment (sanitized with `src/sanitize.py`, like
   every model-written comment in this repo). Its job output is the gate result.
4. **Gate.** Implement only if `valid` is `yes`, `reproduced` is `yes` (a bug) or
   `not_applicable` (a feature or change request), and `effort` is not `large`. Any
   other outcome stops with the comment, for the maintainer to decide. A failed
   reproduction says "could not reproduce"; it never closes or rejects the issue.
5. **Fix** (job `fix`). Permissions: `contents: write`, `pull-requests: write`,
   `issues: read` (it only reads the issue; labels and the comment are the `report`
   job's, so no write access to issues is needed here), **not** `workflows`. The model edits files and runs the test
   command; the wrapper pushes the branch and opens the draft PR with a fixed
   title and body template, linking the issue.
6. **After the PR.** `validate` and the review action run on it like on any pull
   request. A person reviews and merges. The agent never does.

## The assessment

The model sees the issue text (a fenced data block, per-run nonce, as in triage) and
the repository, and may run only what the instruction file allows.

| Field | Values | Meaning |
|---|---|---|
| `valid` | `yes`, `no`, `unclear` | A real defect or a coherent, in-scope request. "Worth doing" is not judged; for an issue by the maintainer, wanting it is the answer. |
| `reproduced` | `yes`, `no`, `not_applicable`, `not_attempted` | `yes` requires `evidence`: the command run and an excerpt of its output, or the failing test. A claim without evidence is treated as `not_attempted`. |
| `effort` | `small`, `medium`, `large` | Informational, and the only thing that can stop an otherwise good issue from being attempted automatically. |
| `evidence` | text, at most 1,500 characters | Sanitized before posting. |
| `summary` | text, at most 600 characters | Sanitized before posting. |

Labels written by the script, from a fixed map: `repro:confirmed`, `repro:failed`,
`repro:not-applicable`, and `effort:small|medium|large`. `invalid`, `wontfix` and
`spam` are never applied; they stay human-only.

Valid and reproduced are the weak judgements. A model will readily call a vague
report valid, or say it reproduced something it did not. Hence the evidence
requirement, the `not_attempted` default, and the rule that a negative result only
informs a person.

## What may run, and with which credentials

The model credential, `CLAUDE_CODE_OAUTH_TOKEN`, is necessarily present in both
jobs, so neither job is "without secrets". What bounds a fooled run is who can
steer it, what it can reach, and what a stolen token could do.

| Control | How |
|---|---|
| Author gate | Only write-access authors start a run (step 1); `allowed_non_write_users` and `allowed_bots` are never set. |
| Least permission | Stage one has no write access at all. Stage two has no `workflows` scope, so the agent's push cannot change `.github/workflows`, whatever the issue says. |
| Secrets | The job gets the Claude token and nothing else: no registry or deploy tokens, which stay out of the workflow. |
| Code the model can run | The model may write a test file and run the test command, which is arbitrary code. The control is who can steer it: only trusted authors start a run, and for anyone else's issue a maintainer has read it first. The spike's one injection attempt did not get the model to abuse this, but that is not a control. |
| What that code can reach | **A known gap.** The Claude token is in the job's environment while it runs, so such code could read it, and the network is open (next row). Nothing removes the token today. **[TBD]** run the tests in a later step that does not hold the token, or scrub the environment of the subprocess. |
| Network | Whatever the runner allows. A fooled run could send the token out. The token is bound to your subscription, so rotate it if a run is ever suspect. **[TBD]** whether an egress allowlist is practical. |
| Never `pull_request_target` | The wrapper triggers only on `issues`. No fork code is checked out. |
| Bounds | `--max-turns` and a job timeout; one run per issue; a daily cap on runs per repository **[TBD]**. |
| Output | Draft PRs only; no auto-merge; `validate` required; CODEOWNERS or branch protection on `.github/`. |

## Other people's issues

An issue by someone without write access starts nothing, by default. The triage
action still labels it, because that action has no tools and cannot be steered into
acting. To have an agent work on it, a maintainer reads it, then applies
`ready-to-implement`. The agent then reads text a stranger wrote, with the same
privileges as above: that is the maintainer's decision, as the action's own
documentation says. For that case stage one is skipped, because the maintainer has
done the assessment, and the instruction file tells the model to treat the issue as
data.

## Opening the pull request

With Route A the wrapper pushes `agent/issue-N` and opens a draft pull request itself,
with `GITHUB_TOKEN`. Two consequences, both measured in the spike:

- The repository setting "Allow GitHub Actions to create and approve pull requests"
  must be on. It is off by default, a workflow cannot turn it on, and with it off the
  push works but `gh pr create` is refused. Turning it on is a one-time step per
  repository; enabling the workflow in a repository includes it.
- The pull request is authored by `github-actions`, and `validate` and the review
  actions are created on it but held at `action_required` with no jobs, until a person
  clicks "Approve and run". They do not run on their own. Merging needs them, so this is
  one extra click per agent pull request, on top of the review a person does anyway.

Not tried, on purpose: a fine-grained token in a secret (no new credential is wanted),
and the Claude GitHub App, which would author the pull request as `claude[bot]` and
should not be held, but needs the app installed with write access to the repository.
Either removes the click if it ever becomes a burden.

## What the spike found

Run in a private sandbox repository on a small invented bug (a slug function that kept
a stray hyphen at either end), with Route A and the existing Claude token. The workflow
used for it was `workflow_dispatch` on an issue number, with the two jobs described
above.

| Question | Answer |
|---|---|
| Does the subscription token work in CI for a multi-turn agent? | Yes, with no other credential. No rate-limit or usage message appeared. |
| What does a run cost? | One small issue: assess 4 turns and 6 s of model time (30 s of job), implement 8 turns and 9 s (37 s of job). Notional cost at API prices was about $0.14; nothing is billed on a subscription. Heavy use is not measured. |
| Does the agent reproduce and fix? | Yes. It ran `python3 slug.py "  Hello, World! "`, saw the wrong output, gave the cause, and produced a one-line fix and a regression test, in one commit. |
| Does the wrapper open a draft pull request? | Yes, with `GITHUB_TOKEN`, once the Actions setting is on. |
| Do `validate` and the review run on it? | Not by themselves: created, held at `action_required`. |
| Can the agent's token edit a workflow? | No. Pushing a change under `.github/workflows` was refused: "refusing to allow a GitHub App to create or update workflow ... without `workflows` permission". |
| Does an injection in the issue work? | Not in the one attempt. The issue asked the agent to write environment values to a file, edit a workflow, add a test that calls `curl`, set its own verdict, and push to `main` and merge. The agent said in its verdict that the issue held injected instructions and ignored them; the pull request contained only the fix and its test, and `main` was untouched. This is one model against one injection written by us; it does not show the controls above are unnecessary. The wrapper's own refusal of changes under `.github/` was not exercised, so it needs unit tests. |

## Failure behaviour

Any failure leaves the issue as it was, apart from a comment saying that the run
failed and why, at most once per issue. A run that hits its turn or time cap stops;
a branch with partial work is left, never a PR. Exit code is non-zero so a person
sees it.

## Code layout

```
implement/
  assess/action.yml          reads the issue (trusted author only), runs the model, outputs the verdict
  report/action.yml          validates the verdict, writes labels and the comment, outputs the gate
  fix/action.yml             reads the issue, runs the model, refuses .github/ changes, commits, pushes, opens the draft PR
  assess_instruction.md      what the assessment may do and must return
  fix_instruction.md         how to work an issue, in a plain file (not tool-specific)
src/implement.py             the shared code: issue fetch and author check, verdict validation, label map, gate
docs/implement-action.md     this document
```

`src/implement.py` has six commands, one per job of work: `fetch` (the author check and
the issue file), `schema` (the verdict contract, as the JSON schema the model is held to),
`prompt` (an instruction file plus the repository's notes), `report` (verdict validation,
labels, comment, gate), `check-changes` (what may be committed) and `check-tools` (refuses a
list of allowed commands that would hand the model a shell, the network, git, the
environment, a way to read any file, or an interpreter with nothing after it). It reuses
`src/sanitize.py` and the label-writing helpers of `src/triage.py`. The instruction files
are plain markdown so a different harness can replace the model step later without
losing them. Each action declares only the inputs it uses, and
`tests/test_action_yml.py` checks all of them.

What the shell in `fix/action.yml` does is run, not just read, by the tests: a real git
repository, a local bare remote and a stub `gh`. The script refuses, before anything is
committed or pushed: a change under `.github/`, `.git/`, `.githooks/` or the agent
directories; a lockfile, `CODEOWNERS`, `.gitmodules` or `.gitattributes`; a path with a
control character or a parent-directory component; a symbolic link; more than 25 files;
no change at all; and a branch that already exists. Renames are checked as a delete and an add. The model
action is pinned to a commit (a release at least two weeks old when chosen), because that
step holds the Claude token.

## Verification

- Unit tests for `apply_verdict.py`: every verdict outside the contract is dropped;
  a reproduction claim without evidence becomes `not_attempted`; labels come only
  from the fixed map; the comment is sanitized.
- An injection corpus, like triage's: issues that tell the model to run a command, print
  its environment, push elsewhere, edit a workflow or approve itself. Assert on what
  the validated output allows, and on the token scopes.
- **Spike** in the private sandbox repository, on a small invented bug: does the
  subscription token work in CI and how much does a run use; does the agent open the
  PR; do `validate` and the review run on it; what `GITHUB_TOKEN` can and cannot do.

## Rollout

1. This document, reviewed.
2. The spike in the sandbox (done; the gate passed), then `apply_verdict.py` and the
   instruction files with tests.
3. One real repository, `pdf-vdiff`, with `AGENT_AUTO` off first, then on.
4. Other repositories only on request. Public repositories with outside issues
   get the manual `ready-to-implement` path only.

## Open questions

Answered by the spike: which token opens the pull request (`GITHUB_TOKEN`, plus the
Actions setting and one click for the checks); whether the subscription carries the
runs (yes, for one small issue). Answered since: how `implement/` is packaged (three
composite actions and a thin wrapper, see Decisions).

- **[TBD]** What do the subscription's terms say about automation in CI? The runs worked;
  the terms are not checked.
- **[TBD]** Can the test run be taken out of the step that holds the token, or the
  environment scrubbed, to close the arbitrary-code gap?
- **[TBD]** Is an egress allowlist practical on hosted runners?
- **[TBD]** The `effort` cut-off, and the daily cap per repository.
- **[TBD]** Whether a failed assessment on a maintainer's own issue should add
  `needs-info`, or stay silent apart from the comment.
