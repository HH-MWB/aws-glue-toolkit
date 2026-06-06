"""``gtk`` — CLI for Glue job dependency check and wheel packaging.

Commands:

- ``check`` — resolve dependencies against bundled Glue runtime pins
- ``build`` — write a ``.gluewheels.zip`` for extra Python libraries

Job commands are registered with :func:`gtk_command`. That decorator loads
``pyproject.toml`` into :class:`~aws_glue_toolkit.job.GlueJobProject`,
runs the command body, and on failure looks up
:data:`_GTK_EXCEPTION_HANDLERS` to print a Rich panel (or plain text for
unexpected errors) on :attr:`~cyclopts.App.error_console` and return a
:class:`GtkExitCode`. Commands that do not take a job directory register on
:data:`app` with :meth:`~cyclopts.App.command` directly.

Example::

    gtk check ./my-glue-job
    gtk build ./my-glue-job

"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from cyclopts import App
from pydantic.types import (
    DirectoryPath,  # noqa: TC002  # CLI coercion needs runtime type
)
from rich.panel import Panel

from aws_glue_toolkit.job import (
    GlueJobProject,
    InvalidPyProjectError,
    InvalidTomlError,
    MissingPyProjectError,
    PyProjectUnreadableError,
    load_pyproject,
)
from aws_glue_toolkit.pip import PipError, resolve_packages
from aws_glue_toolkit.wheels import GlueWheelsBuildError, build_gluewheels_zip

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["app"]

# --- Exit codes ---


class GtkExitCode(IntEnum):
    """Process exit codes returned by ``gtk`` subcommands.

    Members ``OK`` and ``UNEXPECTED`` are used outside
    :data:`_GTK_EXCEPTION_HANDLERS`. The rest pair with known failure types.
    """

    OK = 0
    DEPENDENCY_CONFLICT = 1
    MISSING_PYPROJECT = 2
    PYPROJECT_UNREADABLE = 3
    INVALID_TOML = 4
    INVALID_PYPROJECT = 5
    BUILD_FAILED = 7
    UNEXPECTED = 9


# --- App ---

app = App(
    help="AWS Glue development lifecycle toolkit.",
    result_action="print_non_int_sys_exit",
)

# --- Exception handling ---

# Maps exception type → (exit code, error panel for app.error_console).
_GTK_EXCEPTION_HANDLERS: Final[
    dict[type[Exception], tuple[GtkExitCode, Panel]]
] = {
    MissingPyProjectError: (
        GtkExitCode.MISSING_PYPROJECT,
        Panel(
            "No pyproject.toml in the job directory.",
            title="[bold red]Missing pyproject.toml[/]",
            border_style="red",
        ),
    ),
    PyProjectUnreadableError: (
        GtkExitCode.PYPROJECT_UNREADABLE,
        Panel(
            "Could not read pyproject.toml.",
            title="[bold red]Cannot read pyproject.toml[/]",
            border_style="red",
        ),
    ),
    InvalidTomlError: (
        GtkExitCode.INVALID_TOML,
        Panel(
            "pyproject.toml is not valid TOML.",
            title="[bold red]Invalid TOML[/]",
            border_style="red",
        ),
    ),
    InvalidPyProjectError: (
        GtkExitCode.INVALID_PYPROJECT,
        Panel(
            "pyproject.toml does not match the Glue job schema.",
            title="[bold red]Invalid pyproject.toml[/]",
            border_style="red",
        ),
    ),
    PipError: (
        GtkExitCode.DEPENDENCY_CONFLICT,
        Panel(
            "Requirements are unsatisfiable with bundled Glue pins.",
            title="[bold red]Conflicts detected[/]",
            border_style="red",
        ),
    ),
    GlueWheelsBuildError: (
        GtkExitCode.BUILD_FAILED,
        Panel(
            "Building .gluewheels.zip failed.",
            title="[bold red]Build failed[/]",
            border_style="red",
        ),
    ),
}


def _lookup_handler(exc: Exception) -> tuple[GtkExitCode, str | Panel]:
    """Map ``exc`` to an exit code and printable error output.

    Walks ``type(exc).__mro__`` for the first key in
    :data:`_GTK_EXCEPTION_HANDLERS`. Unregistered exceptions use
    :attr:`GtkExitCode.UNEXPECTED` and a ``[ExceptionName] …`` string (not a
    panel).
    """
    for exc_type in type(exc).__mro__:
        if exc_type in _GTK_EXCEPTION_HANDLERS:
            return _GTK_EXCEPTION_HANDLERS[exc_type]
    return GtkExitCode.UNEXPECTED, f"[{type(exc).__name__}] {exc}"


def gtk_command(
    fn: Callable[[GlueJobProject], int],
) -> Callable[[DirectoryPath], int]:
    """Register ``fn`` as a Cyclopts command with one ``job_dir`` argument.

    Loads :func:`~aws_glue_toolkit.job.load_pyproject`, passes the
    resulting :class:`~aws_glue_toolkit.job.GlueJobProject` to ``fn``,
    and expects an :class:`GtkExitCode` return value. Any other
    :exc:`Exception` is handled by :func:`_lookup_handler` (print + non-zero
    exit code).

    Copies ``__name__`` and ``__doc__`` from the inner function only (not
    ``functools.wraps``), so Cyclopts does not expose inner parameters
    (such as ``--job.name``) on the CLI.
    """

    def wrapper(job_dir: DirectoryPath = Path()) -> int:
        try:
            return fn(load_pyproject(job_dir.resolve()))
        except Exception as exc:  # noqa: BLE001  # CLI shell; see _lookup_handler  # pylint: disable=broad-exception-caught
            exit_code, message = _lookup_handler(exc)
            if isinstance(message, Panel) and str(exc):
                message = Panel(
                    f"{message.renderable}\n\n{exc}",
                    title=message.title,
                    border_style=message.border_style,
                )
            app.error_console.print(message)
            return exit_code

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__

    return cast("Callable[[DirectoryPath], int]", app.command(wrapper))


# --- Commands ---


@gtk_command
def check(job: GlueJobProject) -> int:
    """Verify dependencies resolve for the job's Glue version.

    Runs :func:`~aws_glue_toolkit.pip.resolve_packages` with bundled Glue
    runtime pins, platform, and Python version. Skipped when the job has no
    dependencies.
    """
    if job.dependencies:
        resolve_packages(
            job.dependencies,
            job.runtime_metadata.python_packages,
            python_version=job.runtime_metadata.core_engines.python,
            platform=job.runtime_metadata.pip_platform,
        )
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
    """Build ``{name}-{version}.gluewheels.zip`` under the job directory.

    Resolves dependencies, omits Glue built-ins at the same version, downloads
    wheels for the runtime platform, and writes the job's default artifact
    path.
    """
    result = build_gluewheels_zip(job)
    app.console.print(
        Panel(
            f"Wrote {result.output_path} ({result.wheel_count} wheels).",
            title="[bold green]Build complete[/]",
            border_style="green",
        ),
    )
    return GtkExitCode.OK
