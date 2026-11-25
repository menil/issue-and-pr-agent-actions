# Issue triage action: design

Status: draft for review. Nothing here is built yet.

## Goal

When someone opens an issue, add the right type label, say if it is a duplicate
of an open issue, and ask once for whatever is missing. One model call per issue,
free to run (Claude subscription token, then OpenRouter free models), nothing
the model writes reaches the thread unsanitized, and a failure changes nothing.

Not goals: fixing issues (a separate stage behind a human-applied label),
closing, prioritising, assigning, or replying to later comments.

It is a triage clerk, not an analyst. It does not judge whether an issue is
valid, whether it reproduces, how hard it would be, or whether it is worth
doing. It sees only the issue text and the titles of other issues, never the
code, so those would be guesses; and `invalid`, `wontfix` and `spam` are exactly
what a hostile issue would ask the model to apply, so they stay human-only.
`needs-info` is the closest it gets: it means the report is too thin to try to
reproduce, not that it was tried. An advisory comment (a likely area, a one-line
summary) and a mode that reads the repository were considered and deferred until
there are real outside issues to look at.

## Decisions

| | Decision |
|---|---|
| Location | A second action in this repo: `triage/action.yml`, used as `menil/issue-and-pr-agent-actions/triage@v1`. It shares the sanitizer and the provider cascade with the review action instead of copying them. |
| Scope | Every new issue except those opened by bots, and except any that already carries a triage label or `no-triage`. |
| Output | Labels, plus at most one comment, only to ask for missing information. |

## How it runs

- Trigger: `issues: [opened]`. `reopened` and `edited` are ignored.
- Permissions: `issues: write`, `contents: read`. No checkout is needed: the issue
  comes from the event payload (`GITHUB_EVENT_PATH`).
- Concurrency: one run per issue number.
- Inputs: the same provider inputs as the review action (`provider`,
  `openrouter_*`, `anthropic_*`, `claude_code_oauth_token`, `github_token`), plus
  `no_triage_label` (default `no-triage`) and `duplicate_candidates` (default 50).

## Flow

1. **Guards (no model call).** Skip if the author is a bot, the issue already has
   `bug`, `enhancement`, `question`, `documentation`, `duplicate`, `needs-info`,
   `triaged` or the opt-out label, or a comment containing our marker already exists.
2. **Gather.** The title (cut at 300 characters) and body (cut at 6,000 characters,
   invisible characters removed), and up to 50 other issues as `#N: title` for
   duplicate candidates: the open ones, plus (proposed, see the open questions)
   those closed in the last 90 days. Only issues opened before this one are
   offered, so a duplicate always points back at the earlier report and two
   similar issues are never marked as duplicates of each other.
3. **Ask the model once** for the JSON below, through the provider cascade. The
   issue text goes in clearly delimited and is described as untrusted data.
4. **Validate** the answer in code against the contract below. Anything outside
   it is dropped, not repaired.
5. **Apply**, in this order: the comment (if `needs_info`), then the labels. The last
   label is always `triaged`, so a run that got a usable answer marks the issue even
   when there is nothing else to write.
6. **Log** what was decided and why (`reason`, never posted).

## Model contract

```json
{
  "type": "bug | enhancement | question | documentation | none",
  "needs_info": false,
  "missing": "",
  "duplicate_of": null,
  "reason": ""
}
```

- `type` maps to the label of the same name; anything else is `none`.
- `missing`: at most 600 characters, one or two sentences saying what the reporter
  should add. Only read when `needs_info` is true.
- `duplicate_of`: must be the number of an issue in the candidate list and not the
  issue itself, or it is ignored.
- `reason`: at most 300 characters, for the log only.

## What the action may and may not do

| May | Only by |
|---|---|
| Add `bug`, `enhancement`, `question` or `documentation` | a fixed map from `type`; skipped, never created, if the repository lacks the label |
| Add `duplicate` | a validated `duplicate_of`; no comment, no closing |
| Add `needs-info` | created once if missing, with a fixed description (proposed: see the open questions) |
| Add `triaged` | after every run that got a usable answer; created once if missing, with a fixed description. It is what a person filters on (`-label:triaged`) and what stops a second run. Not added when the model gave no usable answer, so it never claims what did not happen |
| Post one comment | a fixed template around the sanitized `missing`, ending in a hidden marker |

It never closes, reopens, edits, assigns, sets a milestone or priority, applies
`invalid`, `wontfix` or `spam`, mentions anyone, or adds any other label. At most
4 labels and 1 comment per issue; the model's output never names a label.

## Threat model

The issue title, body and the candidate titles are written by whoever can open an
issue, so they are untrusted input to the model.

| Threat | Control |
|---|---|
| Steer the model to apply a label such as `priority/p0` or `spam`, or to close | Labels come from a fixed map in code; the model only picks one of four types, or none. There is no code path that closes. |
| Put links, pings, hidden text or a forged alert in the comment | `missing` goes through `sanitize_llm_text` (the review action's sanitizer), inside a fixed template, capped at 600 characters. |
| Make the model point at an unrelated issue | `duplicate_of` must be in the fetched candidate list; it only adds a label. |
| Loops (our own comment or label retriggering us) | `opened` only; bot authors skipped; marker comment; existing-label guard. |
| Cost abuse | One call per issue, input caps, one run per issue, free providers. |
| Log injection | Model and issue text in the log goes through the same single-line rendering the review action uses. |

The `issues` event runs with the repository's secrets even for outside reporters.
The model never sees them and nothing from the issue is executed.

## Failure behaviour

Any failure (provider, malformed JSON, GitHub API) logs a `::warning::` with fixed
text and exits 0 having changed nothing further. Applying writes only after the
whole decision has been validated, so there is no half-triaged issue from a bad
model answer. A failed write is logged and not retried.

## Code layout

`src/pr_review.py` is one file of about 1,700 lines. Both actions need the same
two things, so they move out first, with no behaviour change:

- `src/sanitize.py`: `sanitize_llm_text`, the log rendering helpers, and their constants.
- `src/llm.py`: the provider config, request functions, retry and fallback loop.
  Provider selection and the attempt loop are now inline in `main()` and become
  functions both entry points call.
- `pr_review.py` imports them by name, so existing tests that patch
  `pr_review.<name>` keep working.

Then `src/triage.py`, `src/triage_instruction.md`, `triage/action.yml`, and
`tests/test_triage.py`.

## Verification

1. Unit tests with a mocked model: guards, validation of every field, label map,
   the comment template, failure paths, and that nothing is written on failure.
2. An injection corpus of issue bodies (forbidden labels, "close this", mentions,
   links, hidden text, fake system prompts, JSON embedded in the body). Assert:
   only allowlisted labels, no canary text in the comment, no other writes.
3. A replay script over the real issues (14 today) with a real provider, to
   compare its labels with the ones a human applied. Manual, not in CI.
4. The comment template with hostile `missing` text through the opt-in GitHub
   renderer check.
5. End to end on a real issue in the private `menil/sandbox` repository.

## Rollout

Dogfood in `menil/sandbox`, then the repositories that receive issues, then a
`enable_issue_automation` toggle in project-template (off by default; a separate
change). Releases are cut with the normal process in the README.

## Open questions

- [TBD] Create `needs-info` automatically. Proposed: yes, once, with a fixed
  description. The alternative is to require it to exist.
- [TBD] Does a closed issue count as a duplicate candidate? Proposed: yes if
  closed in the last 90 days.
- [TBD] An empty or near-empty body: leave it to the model, or skip the call and
  ask for details deterministically? Proposed: leave it to the model.
- [TBD] Body limit of 6,000 characters and candidate count of 50: starting values,
  to adjust after the replay.
