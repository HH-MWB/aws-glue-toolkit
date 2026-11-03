# Default recipe: display list of available recipes in logical order.
[private]
default:
    @just --list --unsorted

# =============================================================================
# Development & Quality
# =============================================================================

# Initialize local development environment (Python, dependencies, git hooks).
init:
    uv python install
    uv sync --group dev
    uv run pre-commit install

# Run pre-commit hooks on all or specified files (e.g. `just lint` or `just lint ruff-check`).
lint *ARGS="--all-files":
    uv run pre-commit run {{ARGS}}

# =============================================================================
# Documentation
# =============================================================================

# Serve documentation locally with live-reload.
docs:
    uv run --with zensical zensical serve
