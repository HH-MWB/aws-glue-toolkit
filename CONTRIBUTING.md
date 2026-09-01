# Contributing

Thank you for your interest in AWS Glue Toolkit. This guide covers local setup, the change workflow, and what to expect when opening a pull request.

## Prerequisites

- Python 3.11+
- [Docker](https://docs.docker.com/get-docker/) (for manual `gtk` smoke tests against a sample job)
- [uv](https://docs.astral.sh/uv/) (dependency and environment management)
- [just](https://github.com/casey/just) (optional; wraps common dev commands)

## Setup

From the repository root:

```bash
just init
```

This installs the project Python version, syncs dev dependencies, and installs pre-commit hooks. Without `just`, run the equivalent:

```bash
uv python install
uv sync --group dev
uv run pre-commit install
```

## Architecture

Before changing code, read [docs/architecture.md](docs/architecture.md). The package follows a **functional core / imperative shell** split:

- **Core** (`paths.py`, `runtime.py`, `job.py`, `dependencies.py`, `artifacts.py`) — pure domain logic; no CLI, Docker, or terminal I/O.
- **Shell** (`docker.py`, `run_wrapper.py`, `app.py`, `cli.py`) — subprocess orchestration, use-case wiring, the `gtk run` spark-submit entry, and the `gtk` CLI.

Put new business rules and transforms in core modules. Keep subprocess calls, exit-code mapping, and Rich output in the shell layer.

## Making changes

1. Edit code under `src/aws_glue_toolkit/`.
2. Run lint and format checks on staged files:

   ```bash
   just lint
   ```

   For a full sweep (matches CI):

   ```bash
   uv run pre-commit run --all-files
   ```

3. If you changed user-facing behavior, update [README.md](README.md).
4. Open a pull request with a small, focused diff.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) for the subject line, for example:

```text
feat(runtime): add load_runtime
fix: correct wheel packaging for data files
docs: clarify GlueVersion in docstrings
```

## Pull requests

- Keep diffs focused on one concern.
- Ensure pre-commit passes before requesting review.
- Use a Conventional Commit-style PR title when possible.

## Code of conduct

Participants are expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Report vulnerabilities privately via [SECURITY.md](SECURITY.md) — do not open public issues for security reports.
