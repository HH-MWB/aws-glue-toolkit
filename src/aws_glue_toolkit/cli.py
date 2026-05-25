"""``gtk`` CLI entry point.

Registers Cyclopts commands and orchestrates the toolkit workflow:

- :func:`check` — load a job ``pyproject.toml`` and verify dependencies resolve
  against Glue runtime pins.
- :func:`build` — resolve packageable dependencies and write a
  ``.gluewheels.zip`` artifact.

Translates domain errors from :mod:`aws_glue_toolkit.pyproject`,
:mod:`aws_glue_toolkit.dependencies`, and :mod:`aws_glue_toolkit.wheels` into
process exit codes and Rich console output. Does not implement resolution or
packaging logic itself.

Example:
    gtk check
    gtk check ./my-glue-job
    gtk build ./my-glue-job

"""

from __future__ import annotations

from enum import IntEnum
from functools import wraps
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Final, ParamSpec, cast

from cyclopts import App
from pydantic.types import (
    DirectoryPath,  # noqa: TC002  # CLI coercion needs runtime type
)
from rich.panel import Panel

from aws_glue_toolkit.dependencies import (
    DependencyConflictError,
    resolve_dependencies,
)
from aws_glue_toolkit.pyproject import (
    InvalidPyProjectError,
    InvalidTomlError,
    MissingPyProjectError,
    PyProject,
    PyProjectError,
    PyProjectUnreadableError,
    load_pyproject,
)
from aws_glue_toolkit.wheels import (
    GlueWheelsBuildError,
    build_gluewheels_zip,
    glue_wheels_zip_name,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["app"]

P = ParamSpec("P")

# --- Exit codes and errors ---


class GtkExitCode(IntEnum):
    """Process exit codes for ``gtk`` commands."""

    OK = 0
    DEPENDENCY_CONFLICT = 1
    MISSING_PYPROJECT = 2
    PYPROJECT_UNREADABLE = 3
    INVALID_TOML = 4
    INVALID_PYPROJECT = 5
    UV_NOT_FOUND = 6
    BUILD_FAILED = 7


class GlueToolkitError(Exception):
    """Expected CLI failure with a user-facing message and exit code."""

    def __init__(self, exit_code: GtkExitCode, error_message: str) -> None:
        self.exit_code = int(exit_code)
        self.error_message = error_message
        super().__init__()


# --- App and command decorator ---

app = App(
    help="AWS Glue development lifecycle toolkit.",
    result_action="print_non_int_sys_exit",
)


def gtk_command(fn: Callable[P, int]) -> Callable[P, int]:
    """Register on :data:`app` with :class:`GlueToolkitError` handling."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return fn(*args, **kwargs)
        except GlueToolkitError as exc:
            app.error_console.print(exc.error_message)
            return exc.exit_code

    return cast("Callable[P, int]", app.command(wrapper))


def get_uv_executable() -> str:
    """Locate the ``uv`` executable on ``PATH``.

    Raises:
        GlueToolkitError: Not found (:attr:`GtkExitCode.UV_NOT_FOUND`).

    """
    uv = which("uv")
    if not uv:
        raise GlueToolkitError(
            GtkExitCode.UV_NOT_FOUND,
            "uv executable not found; install uv or ensure it is on PATH",
        )
    return uv


# --- Tooling helpers ---

_PYPROJECT_EXIT: Final[dict[type[PyProjectError], GtkExitCode]] = {
    MissingPyProjectError: GtkExitCode.MISSING_PYPROJECT,
    PyProjectUnreadableError: GtkExitCode.PYPROJECT_UNREADABLE,
    InvalidTomlError: GtkExitCode.INVALID_TOML,
    InvalidPyProjectError: GtkExitCode.INVALID_PYPROJECT,
}


def _load_pyproject(project_dir: Path) -> PyProject:
    try:
        return load_pyproject(project_dir)
    except PyProjectError as e:
        exit_code = _PYPROJECT_EXIT.get(type(e), GtkExitCode.INVALID_PYPROJECT)
        raise GlueToolkitError(exit_code, str(e)) from e


# --- UI helpers ---


def _print_dependency_conflict() -> int:
    app.error_console.print(
        Panel(
            "Requirements are unsatisfiable with bundled Glue pins.",
            title="[bold red]Conflicts detected[/]",
            border_style="red",
        ),
    )
    return GtkExitCode.DEPENDENCY_CONFLICT


def _run_build(pyproject: PyProject, output_path: Path) -> int:
    try:
        uv_exe = get_uv_executable()
        resolved = resolve_dependencies(
            pyproject,
            uv_exe=uv_exe,
            exclude_builtins=True,
        )
        result = build_gluewheels_zip(
            resolved,
            output_path,
            uv_exe=uv_exe,
        )
    except DependencyConflictError:
        return _print_dependency_conflict()
    except GlueWheelsBuildError as e:
        raise GlueToolkitError(
            GtkExitCode.BUILD_FAILED,
            str(e),
        ) from e
    app.console.print(
        Panel(
            f"Wrote {result.output_path} ({result.wheel_count} wheels).",
            title="[bold green]Build complete[/]",
            border_style="green",
        ),
    )
    return GtkExitCode.OK


# --- Commands ---


@gtk_command
def check(project_dir: DirectoryPath = Path()) -> int:
    """Check job dependencies against bundled Glue runtime pins."""
    pyproject = _load_pyproject(project_dir.resolve())
    try:
        resolve_dependencies(pyproject, uv_exe=get_uv_executable())
    except DependencyConflictError:
        return _print_dependency_conflict()
    app.console.print(
        Panel(
            "Dependencies resolve against Glue runtime pins.",
            title="[bold green]No conflicts[/]",
            border_style="green",
        ),
    )
    return GtkExitCode.OK


@gtk_command
def build(project_dir: DirectoryPath = Path()) -> int:
    """Build a ``.gluewheels.zip`` artifact from ``pyproject.toml``."""
    root = project_dir.resolve()
    pyproject = _load_pyproject(root)
    return _run_build(pyproject, root / glue_wheels_zip_name(pyproject))
