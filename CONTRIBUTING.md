# Contributing to the Radoff Home Assistant integration

Thanks for taking the time to contribute. This document covers the basics:
how to run the development harness, how to lint your changes, and the
branching/versioning conventions this repository follows.

## Setting up the development harness

This repository ships a dev container (`.devcontainer.json`) and a set of
helper scripts (`scripts/setup`, `scripts/develop`, `scripts/lint`) to run a
local Home Assistant instance against your working copy of the integration.

Full setup instructions (WSL/Docker prerequisites, first run, debugging,
troubleshooting) are maintained separately in the internal development
runbook rather than duplicated here; ask a maintainer for the current link if
you don't have it. In short:

1. Open the repository in the dev container (VS Code: **Dev Containers:
   Reopen in Container**), or set up a local Python 3.12 virtual environment
   and run `scripts/setup`.
2. Run `./scripts/develop` to start Home Assistant against the local dev
   harness (`.devcontainer/config/`).
3. Configure a real Radoff account against your local instance to exercise
   the integration end to end.

**Do not commit anything under `.devcontainer/config/` other than
`configuration.yaml`.** Home Assistant writes your Radoff account credentials
and Cognito tokens to `.devcontainer/config/.storage/` on first run; this
path is excluded via `.gitignore` and must stay that way.

## Linting

```bash
./scripts/lint
```

This runs `ruff format .` followed by `ruff check . --fix`, using the rules
in `.ruff.toml`. CI runs the equivalent checks on every pull request; please
run this locally before opening one.

## Tests

This repository does not yet have an automated test suite (`pytest`-based
testing is tracked as separate work). Until it lands, verify your change
manually against the dev harness described above, and describe how you
tested it in your pull request.

## Branching and versioning

- Branch off `main` for your change; open a pull request against `main`.
- Keep pull requests scoped to one change where practical - it makes review
  and, if needed, revert much easier.
- The integration follows the version declared in
  `custom_components/radoff/manifest.json`. Contributors should not bump this
  version themselves; it is updated as part of the release process.
- CI runs `hassfest` and HACS validation, plus lint, on every pull request.
  All checks must pass before merge.