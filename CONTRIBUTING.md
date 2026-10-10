# Contributing

Thank you for your interest in AWS Glue Toolkit. This guide covers local setup and the change workflow. It also describes what to expect when you open a pull request.

## Prerequisites

- Python 3.11+
- [Docker](https://docs.docker.com/get-docker/) (to run `gtk` against a sample job on your machine)
- [uv](https://docs.astral.sh/uv/) (dependency and environment management)
- [just](https://github.com/casey/just) (optional; wraps common development commands)

## Setup

From the repository root:

```bash
just init
```

`just init` installs the project Python version, syncs development dependencies, and installs pre-commit hooks.

If you do not use `just`, run:

```bash
uv python install
uv sync --group dev
uv run pre-commit install
```

## Architecture

Review the layout below before you change code. The package uses a **functional core / imperative shell** split.

- **Core** (`mounts.py`, `runtime.py`, `job.py`, `requirements.py`, `wheels.py`, `artifacts.py`) — domain logic: parsing, pins, wheels, and zip layout.
- **Shell** (`container.py`, `run_wrapper.py`, `workflows.py`, `cli.py`) — subprocess orchestration, use-case wiring, the `gtk run` spark-submit entry, and the `gtk` command-line interface.

Put new business rules and transforms in core modules. Keep subprocess calls, exit-code mapping, and Rich output in the shell layer.

### Where to add code

1. Fact, rule, or transform → core modules listed above.
2. Multi-step user workflow → `workflows.py`.
3. Subprocess or Docker argument lists → `container.py`.
4. In-container `spark-submit` entry for `gtk run` → `run_wrapper.py`.
5. Terminal presentation, Cyclopts, or exit codes → `cli.py`.

When a change needs both pip and Docker for a user-facing workflow, wire it in **`workflows`** and pass a **`PipRunner`** into **`requirements`** helpers.

## Stable interface

The stable interface is the **`gtk` command-line interface**. The README documents [commands](README.md#commands), [build artifacts](README.md#build-artifacts), and [exit codes](README.md#exit-codes). Run `gtk COMMAND --help` for flags.

## Making changes

1. Edit code under `src/aws_glue_toolkit/`.
2. Run lint and format checks on staged files:

   ```bash
   just lint
   ```

   For a full sweep that matches continuous integration:

   ```bash
   uv run pre-commit run --all-files
   ```

3. If you changed user-facing behavior, update [README.md](README.md). For advanced or niche behavior that does not belong in the README, describe it in the commit message or pull request body.
4. Open a pull request with a small, focused diff.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) for commit subjects, for example:

```text
feat(runtime): add load_runtime
fix: correct wheel packaging for data files
docs: clarify GlueRuntimeMetadata in docstrings
```

## Pull requests

- Keep diffs focused on one concern.
- Ensure pre-commit passes before you request review.
- Use a [Conventional Commits](https://www.conventionalcommits.org/) subject line for the pull request title when possible.

## Code of conduct

Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Report security vulnerabilities through [SECURITY.md](SECURITY.md).
