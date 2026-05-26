"""``gtk`` — CLI for Glue job dependency check and wheel packaging.

Commands:

- ``check`` — resolve dependencies against bundled Glue runtime pins
- ``build`` — write a ``.gluewheels.zip`` for extra Python libraries

Job commands use :func:`gtk_command`, which loads ``pyproject.toml`` into
:class:`~aws_glue_toolkit.pyproject.GlueJobProject` and maps errors to exit
codes. Commands that do not need a job directory (e.g. ``version``) register
with :meth:`~cyclopts.App.command` on :data:`app` directly.

Example::

    gtk check ./my-glue-job
    gtk build ./my-glue-job

"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Final, cast

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
    GlueJobProject,
    InvalidPyProjectError,
    InvalidTomlError,
    MissingPyProjectError,
    PyProjectError,
    PyProjectUnreadableError,
    load_pyproject,
)
from aws_glue_toolkit.wheels import GlueWheelsBuildError, build_gluewheels_zip

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["app"]

# --- Exit codes ---


class GtkExitCode(IntEnum):
    """Exit codes returned by ``gtk`` subcommands."""

    OK = 0
    DEPENDENCY_CONFLICT = 1
    MISSING_PYPROJECT = 2
    PYPROJECT_UNREADABLE = 3
    INVALID_TOML = 4
    INVALID_PYPROJECT = 5
    UV_NOT_FOUND = 6
    BUILD_FAILED = 7


class GlueToolkitError(Exception):
    """Expected failure; carries an exit code and user-facing message."""

    def __init__(self, exit_code: GtkExitCode, error_message: str) -> None:
        self.exit_code = int(exit_code)
        self.error_message = error_message
        super().__init__()


# --- App ---

app = App(
    help="AWS Glue development lifecycle toolkit.",
    result_action="print_non_int_sys_exit",
)

_JOB_LOAD_EXIT: Final[dict[type[PyProjectError], GtkExitCode]] = {
    MissingPyProjectError: GtkExitCode.MISSING_PYPROJECT,
    PyProjectUnreadableError: GtkExitCode.PYPROJECT_UNREADABLE,
    InvalidTomlError: GtkExitCode.INVALID_TOML,
    InvalidPyProjectError: GtkExitCode.INVALID_PYPROJECT,
}


def gtk_command(
    fn: Callable[[GlueJobProject], int],
) -> Callable[[DirectoryPath], int]:
    """Register ``fn`` as a Cyclopts command with one ``job_dir`` argument.

    Loads :func:`~aws_glue_toolkit.pyproject.load_pyproject`, passes the
    resulting :class:`~aws_glue_toolkit.pyproject.GlueJobProject` to ``fn``,
    and maps load failures and :class:`GlueToolkitError` to exit codes.

    Copies ``__name__`` and ``__doc__`` from the inner function only (not
    ``functools.wraps``), so Cyclopts does not expose inner parameters
    (such as ``--job.name``) on the CLI.
    """

    def wrapper(job_dir: DirectoryPath = Path()) -> int:
        try:
            return fn(load_pyproject(job_dir.resolve()))
        except PyProjectError as e:
            app.error_console.print(str(e))
            return _JOB_LOAD_EXIT[type(e)]
        except GlueToolkitError as exc:
            app.error_console.print(exc.error_message)
            return exc.exit_code

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__

    return cast("Callable[[DirectoryPath], int]", app.command(wrapper))


def get_uv_executable() -> str:
    """Return the ``uv`` executable on ``PATH``.

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


def _print_dependency_conflict() -> int:
    """Print conflict panel; return dependency-conflict exit code."""
    app.error_console.print(
        Panel(
            "Requirements are unsatisfiable with bundled Glue pins.",
            title="[bold red]Conflicts detected[/]",
            border_style="red",
        ),
    )
    return GtkExitCode.DEPENDENCY_CONFLICT


# --- Commands ---


@gtk_command
def check(job: GlueJobProject) -> int:
    """Verify dependencies resolve for the job's Glue version."""
    try:
        resolve_dependencies(job, uv_exe=get_uv_executable())
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
def build(job: GlueJobProject) -> int:
    """Build ``{name}-{version}.gluewheels.zip`` under the job directory."""
    try:
        uv_exe = get_uv_executable()
        resolved = resolve_dependencies(
            job,
            uv_exe=uv_exe,
            exclude_builtins=True,
        )
        result = build_gluewheels_zip(
            resolved,
            job.glue_wheels_zip_path,
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
