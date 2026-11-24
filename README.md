# AI PR Code Reviewer Action

An automated Pull Request code reviewer powered by OpenRouter language models (like Gemini, Llama) or the Anthropic API (Claude) directly. Submits both high-level summaries and line-by-line inline review comments directly to GitHub PRs.

## Features
- Two providers: OpenRouter (dynamic model swapping — Gemini, Llama, etc.) or Anthropic (Claude, via an Anthropic API key or a Claude Pro/Max subscription OAuth token).
- Line-by-line inline code comments on modified lines only.
- Strict bug, performance, readability, and security focus.
- Lightweight, plain Python implementation with zero third-party library dependencies.
- A second action, [issue triage](#issue-triage), labels newly opened issues and asks for missing information, with the same providers.

## Usage

### Public Repositories (or Organization Shared Private Repositories)
If this action is shared within your GitHub Organization, you can reference it directly:

```yaml
name: Automated Code Review
on:
  pull_request:
    types: [opened, ready_for_review]

permissions:
  pull-requests: write
  contents: read

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Run Review Action
        uses: menil/pr-code-review-action@v1
        with:
          openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

> [!IMPORTANT]
> The action runs with your secrets, so do not reference `@main`: whatever is pushed there would run in your repositories at once. `@v1` is the floating major tag and only moves to compatible releases. For a fixed version use an exact release (`@v1.0.0`), or a full commit SHA to pin it completely. See [Versioning and releases](#versioning-and-releases).

### Using Anthropic (Claude) instead of OpenRouter
Set `provider: anthropic` and supply credentials using **one** of the two methods below. OpenRouter is not involved in either case.

**Anthropic API key** — billed per token against that key:

```yaml
      - name: Run Review Action
        uses: menil/pr-code-review-action@v1
        with:
          provider: anthropic
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          anthropic_model: claude-haiku-4-5   # optional; this is the default
```

**Claude Pro/Max subscription (OAuth token)** — uses your subscription's usage allowance instead of pay-per-token billing. Generate a long-lived token locally with `claude setup-token` and store it as a repository secret:

```yaml
      - name: Run Review Action
        uses: menil/pr-code-review-action@v1
        with:
          provider: anthropic
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          anthropic_model: claude-haiku-4-5   # optional; this is the default
```

> [!NOTE]
> Provide credentials for whichever provider you select. If both `anthropic_api_key` and `claude_code_oauth_token` are set, `claude_code_oauth_token` takes precedence — a configured Claude Code subscription is preferred over a billed API key. On the Opus/Sonnet-5 tiers the action deliberately omits `temperature`, so any current Claude model works out of the box.

**Choosing automatically with fallback (Default)** — by default (`provider: auto`), the action automatically prefers `claude_code_oauth_token` (Claude Pro/Max subscription), then `anthropic_api_key` (billed API key). If neither Claude credential is set, or if Claude fails due to token errors (e.g., expired OAuth subscription token, invalid key, rate limit / quota exceeded) or 5xx server errors, the action automatically falls back to `openrouter_api_key`:

```yaml
      - name: Run Review Action
        uses: menil/pr-code-review-action@v1
        with:
          provider: auto   # default; optional
          openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```
With this setup:
1. Active Claude Code subscription: uses subscription token without billing OpenRouter.
2. Expired Claude Code subscription / invalid token / Anthropic 5xx outage: automatically falls back to OpenRouter to ensure your PR review never fails or blocks CI.
3. Client/request errors (such as HTTP 400 Bad Request): surfaced immediately without falling back to prevent masking configuration issues.

### Private Repositories (Personal / Standalone Accounts)
If you fork the action into a private repository (which cannot be natively referenced by public workflows), check it out dynamically using a Personal Access Token (PAT) with `repo` scope first:

```yaml
name: Automated Code Review
on:
  pull_request:
    types: [opened, ready_for_review]

permissions:
  pull-requests: write
  contents: read

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Target Code
        uses: actions/checkout@v4

      - name: Checkout Private Review Action
        uses: actions/checkout@v4
        with:
          repository: your-username/pr-code-review-action
          token: ${{ secrets.PAT_WITH_REPO_ACCESS }}
          path: .github/actions/pr-code-review-action

      - name: Run PR Review Action
        uses: ./.github/actions/pr-code-review-action
        with:
          openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

## Configuration Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `provider` | LLM provider: `auto`, `anthropic`, or `openrouter` | No | `auto` |
| `openrouter_api_key` | API key from OpenRouter | When `provider` is `openrouter`, or when used in `auto` mode | - |
| `openrouter_model` | Model used for review | No | `openrouter/free` |
| `openrouter_base_url` | Base completions URL | No | `https://openrouter.ai/api/v1/chat/completions` |
| `openrouter_max_tokens`| Max completion tokens | No | `8192` |
| `anthropic_api_key` | API key from Anthropic (`sk-ant-...`); alternative to `claude_code_oauth_token` | When `provider` is `anthropic` (unless `claude_code_oauth_token` is set), or when used in `auto` mode | - |
| `claude_code_oauth_token` | Claude Pro/Max subscription OAuth token from `claude setup-token`; alternative to `anthropic_api_key` | When `provider` is `anthropic` (unless `anthropic_api_key` is set), or when used in `auto` mode | - |
| `anthropic_model` | Claude model used for review | No | `claude-haiku-4-5` |
| `anthropic_base_url` | Base Messages API URL | No | `https://api.anthropic.com/v1/messages` |
| `anthropic_max_tokens` | Max tokens for the Claude response | No | `8192` |
| `github_token` | GitHub token for posting reviews | No | `${{ github.token }}` |
| `post_summary` | Whether to post the high-level summary (PR-level overview of the review findings) as the review body | No | `true` |
| `exclude_patterns` | Custom regex patterns to exclude from review, separated by commas and/or newlines | No | `''` |
| `pull_request_number` | The pull request to review, for a workflow that did not start from that pull request (the implementation workflow uses it). Empty means the pull request that triggered the workflow | No | `''` |

## Issue triage

A second action in this repository, `menil/pr-code-review-action/triage`, handles newly opened issues. With one model call it picks a type label (`bug`, `enhancement`, `question` or `documentation`), flags a likely duplicate, and, when a report is too thin to act on, posts one comment asking for what is missing. It is a triage clerk, not an analyst: it does not judge whether an issue is valid, reproducible, hard or worth doing, and it never closes, assigns or prioritises anything. The design and its threat model are in [docs/triage-action.md](docs/triage-action.md). A design for the next stage, an agent that assesses and implements trusted issues as draft pull requests, is in [docs/implement-action.md](docs/implement-action.md); nothing there is built yet.

> [!NOTE]
> Available from `v1.1.0`. A workflow pinned to `@v1.0.1` or earlier does not have it.

```yaml
name: Issue Triage
on:
  issues:
    types: [opened]

permissions:
  issues: write
  contents: read

# One run per issue, and a second one never cancels the first halfway through.
concurrency:
  group: issue-triage-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  triage:
    runs-on: ubuntu-latest
    steps:
      - name: Triage the issue
        uses: menil/pr-code-review-action/triage@v1
        with:
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

No checkout is needed: the issue is read from the event. It takes the same provider inputs as the review action (`provider`, `openrouter_*`, `anthropic_*`, `claude_code_oauth_token`), tried in the same order. The ones that differ:

| Input | Description | Default |
|-------|-------------|---------|
| `github_token` | Token used to read issues and write labels and the comment; needs `issues: write` | `${{ github.token }}` |
| `no_triage_label` | An issue that carries this label is left alone | `no-triage` |
| `duplicate_candidates` | How many other issues the model is shown to look for a duplicate | `50` |
| `openrouter_max_tokens`, `anthropic_max_tokens` | The reply is a small JSON object | `2048` |

What it does and does not do:

- It leaves an issue alone, without calling the model, if a bot opened it, if it already has a triage label (`bug`, `enhancement`, `question`, `documentation`, `duplicate`, `needs-info`, `triaged`) or the opt-out label, or if it has already asked its question.
- The type and duplicate labels are added only if the repository already has them; GitHub's default labels include all five. `needs-info` is created if it is missing, and is added only together with the comment that says what is missing. After every run that gets a usable answer from the model the issue is also marked `triaged` (created if missing), even if there was nothing else to write; that is how you list the issues not yet triaged (`-label:triaged`) and what keeps a second run from asking again. A run that fails does not add it.
- A duplicate must be one of the issues the model was shown, and it only adds a label: the issue is never closed.
- The comment is the one place model-written text reaches the thread. It goes through the same sanitizer as review comments, and is set in a fixed template.
- If the model fails or answers in an unusable form, nothing is changed and the run still succeeds. Missing credentials fail the run, as in the review action.

## Issue implementation

> [!NOTE]
> Available from `v1.3.0`. A workflow pinned to `@v1.2.0` or earlier does not have it.

Three more actions under `implement/` take an issue from "reported" to a **draft pull request**, for a person to review. They are used from one workflow, [`implement/example-workflow.yml`](implement/example-workflow.yml), copied into the repository. The design, the threat model and what was measured in a trial are in [docs/implement-action.md](docs/implement-action.md).

1. **`implement/assess`** reads the issue and runs a model with read-only tools to decide whether it is real, whether it reproduces and how large the fix would be. It returns a verdict.
2. **`implement/report`** (no model) checks the verdict against a fixed contract, adds `repro:*` and `effort:*` labels from a fixed table, posts one comment, and says whether to go on. It goes on only if the issue is valid, not large, and either reproduced, confirmed by reading the code (`repro:by-reading`), or a change request.
3. **`implement/fix`** lets a model write a test that shows the problem, see it fail, fix the code and see it pass (it changes nothing if it cannot make the test fail), then code refuses anything that must not be committed, pushes `agent/issue-N` and opens a **draft** pull request. The model also writes a short description of its change (`.agent-change.md`: a type, a title, what it changed and why, and what it left undone). Code checks it and turns it into the commit message and the pull request text; a pull request that leaves part of the issue undone says `Refs #N` instead of `Closes #N`, so merging it does not close the issue. If the file is missing or wrong, the generic text `fix: address issue #N` is used and the run is not lost.

The model never merges, approves, labels, or touches `.github/`, lockfiles, hooks or agent configuration; the last is enforced by code, not by asking.

### Who can start a run

- **An issue opened by an owner, member or collaborator**, when the repository variable `AGENT_AUTO` is `true`. Off by default.
- **The `ready-to-implement` label**, whoever wrote the issue. The person who applied it must have write access (the action checks, whoever the author is), so a stranger's issue only runs because a maintainer chose it.
- **Never** for a bot, for an issue labelled `no-agent`, or for one that already has a `repro:*` label. To run an assessed issue again, remove its `repro:*` labels and the assessment comment, and delete its `agent/issue-N` branch if the fix got that far; applying `ready-to-implement` again does nothing until then.
- **Not past the daily limit.** At most `AGENT_DAILY_CAP` assessments (default 5, `0` pauses everything) in 24 hours, counted from the assessment comments the workflow has left. An issue the limit stops gets one comment saying so; apply `ready-to-implement` again later. It is a soft limit: runs that start side by side can pass it together, so a burst of issues can overshoot by its size.

### Setting it up

1. Copy `implement/example-workflow.yml` to `.github/workflows/implement-issue.yml` and edit the three places marked `EDIT`: the release tag, the commands the model may run, and a few notes about the repository. The commands are the only things the model can run besides reading files, so name the narrowest ones that work, for example `Bash(cargo test:*)`. A pattern that would give it a shell, the network, git, the environment or a way to read any file (`Bash(bash:*)`, `Bash(curl:*)`, `Bash(cat:*)`, a bare `Bash(python3:*)`) is refused before any model runs.
2. Add the `CLAUDE_CODE_OAUTH_TOKEN` secret (`gh secret set CLAUDE_CODE_OAUTH_TOKEN`). Only the two jobs that run a model get it.
3. Once per repository, with an admin login, from inside the repository: turn on the setting that lets workflows open pull requests, and create the two labels a person applies. The actions create every other label themselves, but the label picker only offers labels that exist.

   ```bash
   gh api -X PUT repos/{owner}/{repo}/actions/permissions/workflow \
     -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true
   gh label create ready-to-implement --color 0e8a16 --force \
     --description "Ask the agent to assess and implement this issue"
   gh label create no-agent --color 6e7781 --force \
     --description "Keep the agent off this issue"
   ```

   The setting is **Settings, Actions, General, "Allow GitHub Actions to create and approve pull requests"**. It is off by default and a workflow cannot turn it on. Without it the branch is pushed but the pull request is refused, and the job says so.

   > [!WARNING]
   > That setting is one switch for two things: it also lets workflows in the repository **approve** pull requests. These actions never do. But if you rely on required reviews, check that an approval from a workflow cannot satisfy them, for example by requiring review from code owners.

4. Optionally set `AGENT_AUTO` (`gh variable set AGENT_AUTO --body true`) so that your own new issues are worked on automatically.

### What to expect

- **One click per pull request, for `validate`.** A pull request opened with the workflow's own token does not start your repository's workflows on its own: GitHub creates the runs and holds them until a person clicks "Approve and run". That is the price of not putting a personal access token in a secret. The **review** does not wait for the click: the example workflow's `review` job runs it on the new pull request straight away, reading only its diff. A failed review does not fail the run. Because the click would also start your own `pr-review.yml`, make that one skip these branches, or the pull request gets two reviews: add `&& !(startsWith(github.head_ref, 'agent/issue-') && github.event.pull_request.head.repo.full_name == github.repository)` to its job's `if:`. The repository check keeps a fork from skipping its own review by naming a branch like that.
- **The model is a risk even so.** It reads text an outsider may have written, with your Claude token in the job's environment and a test command it can edit and run. Write the allowed commands narrowly, keep `AGENT_AUTO` off in a repository with strangers' issues, and read the design document's section on what could still go wrong.
- **A failed run says so, once.** If any stage fails, the issue gets one comment with a link to the log, and no pull request. It is not repeated for the same issue.
- **It uses your subscription,** not an API key. One small issue took about 15 seconds of model time across the two stages in a trial; heavy use is unmeasured.

## Output safety

The model reads the pull request diff, which its author controls, so a hostile diff can try to steer what the model writes (indirect prompt injection). Rather than relying on the model to refuse, everything it writes is rewritten before it is posted, so GitHub never sees the original text outside code blocks:

- Characters that can begin a link, image, HTML tag, reference definition or code fence (`[`, `]`, `<`, `|`, `~`, `\`, `&` and backticks) are escaped, so those constructs cannot form. Links and images show as plain text instead of being clickable or loading remote content. A table made with pipes cannot form either, but a one-column table needs no pipe at all (a line of text followed by a `:-` line), so tables can still render. That is harmless here: with every other construct inert, nothing active can sit in a cell.
- `@mentions`, issue and pull request references (`#123`, `GH-123`, `owner/repo#1`), URLs and email addresses are shown as inline code, which GitHub never links or notifies. Dollar signs in prose are shown as code too, because GitHub finds math (`$x$`, `$$x$$`) before it parses Markdown, so an escape does not stop it and `\color{white}` inside could hide text. A quote whose first line is `[!NOTE]` or a similar marker would render as a GitHub alert, which a manipulated model could use to forge a "verified" notice, so the marker is shown as code. Commit SHAs and `:emoji:` are left alone: a SHA only links to a commit, and GitHub's emoji are its own images.
- Fenced code blocks keep their content as written, and one still open at the end of the text is closed so the warnings added after it render normally. Fence info strings are reduced to a known language name, otherwise `text`, because GitHub gives some of them special behaviour (`suggestion` adds a one-click "Commit suggestion" button; `mermaid` and `math` render embedded content).
- Inline code written with one backtick at each end keeps its content. Code that contains a `|` or a backtick (for example ``` `str | None` ``` or double-backtick code) is shown as escaped text instead, with its backticks visible and no monospace. GitHub splits table cells at every pipe, even inside code, and a span holding either character could be cut in two or re-paired with the next one, so none is ever written. In a sample of past reviews this affected about 3% of comments, mostly type unions and shell pipes.
- Control, zero-width, bidirectional, unpaired-surrogate and other invisible characters are removed, since they can hide instructions from reviewers while staying readable to other AI tools. This also removes zero-width joiners, which splits emoji sequences and affects some Persian and Indic text.
- Comments are capped at 4,000 characters and the summary at 10,000 before escaping, so the posted text can be longer (up to five times, for `&`). That bound is enforced: a result over five times the cap is withheld instead of posted, because GitHub rejects a review body over 65,536 characters and would drop every comment with it. If sanitizing itself fails, or leaves nothing of a comment or the summary, that text is withheld rather than posted as written. The review then carries a warning saying how many comments (and whether the summary) were withheld, and never reads "No issues found"; the run log also gets a warning annotation and the reason, naming the failure by type and location but never repeating its message, since a message can quote model text. A comment the model left blank is not counted. Truncations are logged.
- Text that comes from the model or from an API response and is written to the run log (error messages, a skipped comment's path, the raw response when parsing fails) is shown with its line breaks escaped, or with every line prefixed, so it can never start a log line with `::`, which Actions would read as a workflow command.

This limits what injected text can do on the pull request thread. It does not make the model's judgement trustworthy: a manipulated model can still write misleading text, such as a claim that the code was verified, so treat the review as advice. The rewriting is tested against `cmark-gfm`, the parser GitHub's renderer is built on, but GitHub adds steps of its own (math, alerts and issue links, for example, which `cmark-gfm` does not render). Those are checked by an opt-in test that sends everything through GitHub's own renderer (see Development). Both are strong evidence, not proof.

## Versioning and releases

Releases follow [semantic versioning](https://semver.org/). Each release has an immutable tag (`v1.2.3`), and the floating major tag (`v1`) points at the latest `v1.x.y`.

- A new major version is needed to remove or rename an input, to require a new secret or permission, or to change the exit code behaviour.
- Changes to what a posted review looks like, such as the escaping rules described under [Output safety](#output-safety), are not breaking, but the release notes list them.

To cut a release, from an up-to-date `main`:

```bash
git tag -a v1.0.1 -m "v1.0.1"
git tag -fa v1 -m "v1.0.1"        # move the floating major tag
git push origin v1.0.1
git push --force origin v1
gh release create v1.0.1 --generate-notes
```

## Development

```bash
# Enter the nix development shell
nix-shell

# Run linting, formatting check, and test suite
just validate
```

The output-safety tests render sanitized text with `cmark-gfm`, which the nix shell provides. Outside the shell they are skipped locally, and they fail in CI (`CI` set) if it is missing.

A second, opt-in check sends the same corpus and a fuzz run through GitHub's own renderer (`gh api markdown`), which also shows math, alerts, issue links and embedded renderers. It needs the `gh` CLI, a login and network, makes about 650 requests and takes a few minutes, so run it whenever the sanitizer changes:

```bash
GITHUB_RENDER_CHECK=1 pytest -k githubs_own_renderer
```

To see how the issue triage behaves on real issues, replay it over a repository's existing issues. It is read-only: it asks the model about each issue as a live run would, showing only the issues that came before it, and prints what the model said next to the labels a person set. Run it whenever the prompt in `src/triage_instruction.md` changes. With provider credentials in the environment:

```bash
GH_TOKEN=$(gh auth token) python scripts/replay_triage.py owner/repo [owner/repo ...]
```

or, with no credentials, `--dry-run` fetches everything and builds the prompts without calling the model. The same replay runs from the Actions tab as the manual "Replay Triage" workflow, using this repository's secrets, and writes the report to the run summary and the log.

If you use [direnv](https://direnv.net/), the checked-in `.envrc` loads the
nix shell automatically when you enter the directory:

```bash
direnv allow
just validate
```
