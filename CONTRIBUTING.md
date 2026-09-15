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

## Comments

This repository is public, and none of it is read only by us. Comment every
part of it for someone who has no access to our issue tracker.

Keep what answers **what this does**, and **which external constraint that
isn't visible in the code forces it to do it that way** - the API's shared
request quota, a device's reporting cadence, a field the backend sends
without a unit. Drop what answers **how we got here**.

Two rules hold everywhere:

- **Never write an internal identifier** - a ticket key, a commit hash, a
  path to a file we don't distribute - or a narrative turn: "previously",
  "used to", "unlike", "this card", "introduced by", "now that", "we
  decided".
- **Never write a value that belongs to our infrastructure** and not to the
  code: a Cognito pool or app client other than the one the integration
  ships, an account, a tenant. Those come from `.env`, which is gitignored.

What the rest looks like depends on who reads it.

**`custom_components/`** ships to users through HACS. Comment it as a
library, not as a work log.

- Module docstring: one line; up to three only for a contract the signatures
  don't show. Class, function and method docstrings: one imperative line,
  with `Args`/`Returns`/`Raises` only where the signature isn't enough.
- Inline comments: only where the code looks wrong and isn't. Two lines at
  most, present tense, about behaviour rather than about the choice.

**`tests/`** is read by whoever has just turned one red. The docstring has to
say what breaks, which is exactly what makes a ticket reference useless.

- Test docstring: **one line, present tense, stating the invariant the test
  guarantees** - and it must not promise more than the assertions check. If
  the test gives less than you want to write, the docstring comes down; the
  assertions are a separate change.
- Module docstring: two lines of summary plus one line listing the areas the
  file covers.
- No comparison with how the code used to behave, and no note addressed to
  whoever reviews the change.

**`scripts/`** holds one-shot tools run by hand. Their opening docstring is
the only documentation they have: keep the usage - the command, the
environment variables, what the script does *not* verify and why - and drop
the account of which change produced it.

**`.ruff.toml`**: one or two lines per `per-file-ignores` entry, saying why
that file is exempt. An entry whose file no longer exists goes with it.

The rationale belongs in the ticket, which has a date and an author. A
comment that outlives the behaviour it describes costs more than no comment
at all.

## Tests

This repository has an automated `pytest` suite under `tests/`, built on
`pytest-homeassistant-custom-component`. It mocks the Radoff API at the
transport level (`requests_mock` for HTTP, `AWSSRP.authenticate_user` for
the Cognito SRP handshake) and exercises auth, domain discovery, entity
construction and config entry migration the same way Home Assistant itself
does - no real network access or Radoff account is needed or used.

Install the test dependencies (in addition to `requirements.txt`) and run
the suite from the repository root:

```bash
pip install -r requirements-test.txt
pytest
```

`pytest.ini` enables coverage reporting with a non-regressive floor (60%,
`--cov-fail-under`) - this is a safety net, not a target: it should only
ever go up as more of the integration gets covered. Fixture payloads live
under `tests/fixtures/` as JSON: the ones at the top level are synthetic,
the ones under `tests/fixtures/dev/` are real responses captured against the
dev environment and redacted before they touch disk - that directory's own
README states the redaction policy, and `tests/test_dev_fixtures.py` enforces
it on every commit. `tests/conftest.py` documents the mocking helpers built
on top of both.

Please still describe any manual verification against the dev harness in
your pull request when a change is not fully exercised by the automated
suite (e.g. a real-account-only behaviour).

## Branching and versioning

- Branch off `main` for your change; open a pull request against `main`.
- Keep pull requests scoped to one change where practical - it makes review
  and, if needed, revert much easier.
- The integration follows the version declared in
  `custom_components/radoff/manifest.json`. Contributors should not bump this
  version themselves; it is updated as part of the release process.
- CI runs `hassfest` and HACS validation, plus lint, on every pull request.
  All checks must pass before merge.