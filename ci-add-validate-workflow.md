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
- **nixpkgs pinned in `shell.nix`** (revision + sha256 via `fetchTarball`).
  The first CI run failed with 44 lint errors: CI installed ruff 0.17.4 from
  the floating `nixos-unstable` channel while local development had 0.15.13,
  and the newer ruff enables many more rules by default (the repo has no ruff
  config). The pin uses the revision local development already used and
  passes with, so CI and local run identical tool versions. The workflow no
  longer sets `nix_path`, because `shell.nix` no longer reads `<nixpkgs>`.
  This also closes the reproducibility part of bead c8a.
- **Least privilege:** `permissions: contents: read`, `pull_request` trigger
  (never `pull_request_target`), no secrets, so fork PRs are safe.
- **Concurrency and timeout:** superseded PR runs are cancelled; pushes to
  main use the commit SHA as their group so none is cancelled or replaced in
  the queue. `timeout-minutes: 15` replaces the 6-hour default.
- **One commit:** the shell.nix fix and pin are prerequisites for the
  workflow to pass, so splitting would leave a red commit.

## Alternatives Considered & Rejected
- **`ignore_missing_imports` or `--python-executable` in mypy config:**
  hides pytest types from `mypy --strict` or pins a Nix store path; it moves
  an environment problem into tool config.
- **`actions/setup-python` + `pip install` instead of Nix:** a few lines
  shorter and faster, but creates a second environment that can drift from the
  documented local flow, and leaves local `just validate` broken.
- **Pinning ruff rules in `pyproject.toml` instead of nixpkgs:** a smaller
  change, but ruff, mypy and python versions would still drift. Pinning the
  whole toolchain fixes the cause rather than one symptom.
- **Adopting the newer ruff's defaults and fixing the 44 findings:** a larger
  change to src and tests that is unrelated to CI plumbing; better done
  deliberately in its own PR.
- **Pinning third-party actions by SHA:** deferred; matches repo convention
  and Dependabot already bumps them.

## Known follow-ups
Align the Python version with the action's 3.11 runtime; extend lint scope to
`src/` and `action.yml` (actionlint); mark the check as required in repo
settings now that the pin is in; add a way to bump the pin (Dependabot has no
plain-nix ecosystem for `fetchTarball`, so a flake.lock or npins would let it
be automated).
