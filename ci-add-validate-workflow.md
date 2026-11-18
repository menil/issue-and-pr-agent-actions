# AI Decision Record: CI validation workflow and nix shell fix

## Context & Goal
The repo had a Justfile `validate` recipe (ruff, mypy --strict, pytest) but no
workflow ran it. Other repos consume this action at `@main` while passing
secrets, so a regression here ships immediately. Goal: run the existing
validation on every PR and on main, using the same toolchain as local
development.

While verifying the recipe under `nix-shell` it already failed on main:
`mypy --strict` reported `import-not-found` for pytest. A CI workflow added
without fixing this would have failed on its first run.

## Architecture & Key Decisions
- **Workflow runs `nix-shell --run "just validate"`** with
  `cachix/install-nix-action`. Nix is the documented dev environment
  (README, `.envrc`), so CI reuses it instead of adding a second toolchain
  that could drift from local results.
- **Root cause of the mypy failure:** pytest was a separate top-level nix
  package, not part of the interpreter's package set, and mypy runs in its own
  environment, so neither could import it. Fixed in `shell.nix` with a single
  `python3.withPackages [pytest mypy]` environment. A comment records the
  reason so nobody "simplifies" it back.
- **Least privilege:** `permissions: contents: read`, `pull_request` trigger
  (never `pull_request_target`), no secrets, so fork PRs are safe.
- **Concurrency and timeout:** superseded PR runs are cancelled; pushes to
  main use the commit SHA as their group so none is cancelled or replaced in
  the queue. `timeout-minutes: 15` replaces the 6-hour default.
- **One commit:** the shell.nix fix is a prerequisite for the workflow to
  pass, so splitting would leave a red commit.

## Alternatives Considered & Rejected
- **`ignore_missing_imports` or `--python-executable` in mypy config:**
  hides pytest types from `mypy --strict` or pins a Nix store path; it moves
  an environment problem into tool config.
- **`actions/setup-python` + `pip install` instead of Nix:** a few lines
  shorter and faster, but creates a second environment that can drift from the
  documented local flow, and leaves local `just validate` broken.
- **Pinning nixpkgs in this change:** deferred. Floating `nixos-unstable`
  can cause drift-induced failures and a Python version different from the
  action's 3.11 runtime. Tracked by existing bead c8a; kept out to keep this
  change small.
- **Pinning third-party actions by SHA:** deferred; matches repo convention
  and Dependabot already bumps them.

## Known follow-ups
Pin nixpkgs and align the Python version with the action's 3.11; extend lint
scope to `src/` and `action.yml` (actionlint); mark the check as required in
repo settings once the pin lands.
