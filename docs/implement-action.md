# Issue implementation: design

Status: draft for review. Nothing here is built yet. Items marked **[TBD]** are open
and listed again at the end; a spike in a private repository answers most of them.

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
| Who starts a run | By default only an issue **opened by someone with write access** (`author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`, checked in the workflow as well as by the action). Anyone else's issue starts nothing; see "Other people's issues". |
| Automatic or not | Opt-in per repository by a variable, `AGENT_AUTO=true`, default off. With it off, nothing starts until a maintainer applies `ready-to-implement`. |
| Stages | Two, in separate jobs: **assess** (read-only), then **implement**, only if the assessment passed. |
| What the model decides | A verdict and, in stage two, the code of a draft PR. Never a label, a comment's markdown, a branch name or a command outside the allowlist. |
| Where it lives | A thin wrapper workflow in each consuming repo. The instruction files and the small script that validates the verdict and writes labels live here, in `implement/`, so they are versioned and tagged with the other actions. **[TBD]** whether `implement/` is a composite action like `triage/` or only files the wrapper checks out. |

## Flow

1. **Guards (no model).** Skip if the issue is from a bot, carries `no-agent`, or
   already has a `repro:*` label or an open `agent/issue-N` branch or PR; skip a
   second run for the same issue (concurrency group per issue number).
   Opened by a trusted author and `AGENT_AUTO` is on, or `ready-to-implement` was
   applied by a person with write access: continue. Otherwise stop.
2. **Assess** (job 1). Permissions: `contents: read`, `issues: read`. A checkout of the
   default branch, the model, and a small allowlist of tools: read files, search,
   and run the repository's own test or build command named in the instruction file.
   It returns a verdict as JSON, through `--json-schema`:
   `{valid, reproduced, effort, evidence, summary}`.
3. **Validate the verdict in code.** Anything outside the contract is dropped. The
   script, not the model, applies the labels and posts one comment (sanitized with
   `src/sanitize.py`, like every model-written comment in this repo).
4. **Gate.** Implement only if `valid` is `yes`, `reproduced` is `yes` (a bug) or
   `not_applicable` (a feature or change request), and `effort` is not `large`. Any
   other outcome stops with the comment, for the maintainer to decide. A failed
   reproduction says "could not reproduce"; it never closes or rejects the issue.
5. **Implement** (job 2). Permissions: `contents: write`, `pull-requests: write`,
   `issues: write`, **not** `workflows`. The model edits files and runs the test
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

Two things to settle in the spike, because they decide whether this works at all:

- A pull request opened with the workflow's `GITHUB_TOKEN` does not trigger other
  workflows, so `validate` and the review would never run on it, and a required check
  would block the merge. **[TBD]** the likely fix is a fine-grained token limited to
  the one repository (contents and pull requests: write, no workflow scope) in a
  secret, used only to push and open the PR; a GitHub App is the alternative.
- The repository setting that lets Actions create pull requests must be on.

## Failure behaviour

Any failure leaves the issue as it was, apart from a comment saying that the run
failed and why, at most once per issue. A run that hits its turn or time cap stops;
a branch with partial work is left, never a PR. Exit code is non-zero so a person
sees it.

## Code layout

```
implement/
  assess_instruction.md      what the assessment may do and must return
  implement_instruction.md   how to work an issue, in a plain file (not tool-specific)
  apply_verdict.py           validates the verdict, writes labels and the comment
docs/implement-action.md     this document
```

`apply_verdict.py` reuses `src/sanitize.py` and the label-write helpers of
`src/triage.py`. The instruction files are plain markdown so a different harness can
replace the action later without losing them.

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
2. The spike in the sandbox, then `apply_verdict.py` and the instruction files with
   tests. **The spike is a gate:** if no token can open a pull request on which
   `validate` and the review run, or the subscription cannot carry the runs, the
   design stops here and the rest is not built.
3. One real repository, `pdf-vdiff`, with `AGENT_AUTO` off first, then on.
4. Other repositories only on request. Public repositories with outside issues
   get the manual `ready-to-implement` path only.

## Open questions

- **[TBD]** Is `implement/` a composite action or files the wrapper checks out?
- **[TBD]** Which token opens the PR, and does a repository need the Actions setting?
- **[TBD]** Does the subscription's usage allowance hold for two model runs per
  issue in CI, and what do its terms say about automation?
- **[TBD]** Is an egress allowlist practical on hosted runners?
- **[TBD]** The `effort` cut-off, and the daily cap per repository.
- **[TBD]** Whether a failed assessment on a maintainer's own issue should add
  `needs-info`, or stay silent apart from the comment.
