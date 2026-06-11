"""``gtk`` — CLI for Glue job dependency check and wheel packaging.

Commands:

- ``check`` — resolve dependencies against bundled Glue runtime pins
- ``build`` — write ``.gluewheels.zip`` and ``.dependencies.zip`` artifacts

Job commands are registered with :func:`gtk_command`. That decorator loads
the job via :func:`_load_job_project`, passes the resulting
:class:`~aws_glue_toolkit.job.GlueJobProject` to the command,
and on failure prints a Rich panel from :exc:`GtkCommandError` on
:attr:`~cyclopts.App.error_console` and returns a :class:`GtkExitCode`
(BSD ``sysexits.h``, 64-78). On success, commands return a Rich panel
that Cyclopts prints and exits ``0``. Unhandled exceptions propagate
with exit code ``1``. Pyproject load failures and command domain failures
raise :exc:`GtkCommandError` locally.

Example::

    gtk check ./my-glue-job
    gtk build ./my-glue-job

"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import TYPE_CHECKING, cast

from cyclopts import App
from pydantic import ValidationError
from pydantic.types import (
    DirectoryPath,  # noqa: TC002  # CLI coercion needs runtime type
)
from rich.panel import Panel

from aws_glue_toolkit.artifacts import (
    build_dependencies_zip,
    build_gluewheels_zip,
)
from aws_glue_toolkit.job import GlueJobProject, load_pyproject
from aws_glue_toolkit.pip import PipError, resolve_packages
from aws_glue_toolkit.runtime import UnsupportedGlueVersionError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["app"]

# --- App ---

app = App(
    help="AWS Glue development lifecycle toolkit.",
    result_action="print_non_int_sys_exit",
)

# --- Exit codes ---


class GtkExitCode(IntEnum):
    """Process exit codes returned by ``gtk`` subcommands on failure.

    Values follow BSD ``sysexits.h`` (64-78): ``0`` is success (returned
    implicitly via a success :class:`~rich.panel.Panel`), ``1`` is reserved for
    unhandled failures. Each member pairs with :exc:`GtkCommandError`.
    """

    DATAERR = 65  # unsatisfiable requirements
    NOINPUT = 66  # cannot read pyproject.toml
    SOFTWARE = 70  # wheel build pipeline failed
    CONFIG = 78  # invalid or unsupported job config


# --- Exception handling ---


class GtkCommandError(Exception):
    """CLI command failure with exit code and user-facing title/message.

    Attributes:
        exit_code: :class:`GtkExitCode` for the failure.
        title: Short panel header (rendered bold red by :func:`gtk_command`).
        message: Panel body text (usually the underlying exception message).

    """

    def __init__(
        self,
        exit_code: GtkExitCode,
        title: str,
        message: str,
    ) -> None:
        """Store exit code, title, and message for the command shell."""
        self.exit_code = exit_code
        self.title = title
        self.message = message


def _load_job_project(job_dir: Path) -> GlueJobProject:
    """Load ``job_dir/pyproject.toml`` or raise :exc:`GtkCommandError`.

    Raises:
        GtkCommandError: :attr:`~GtkExitCode.NOINPUT` when the file cannot
            be read; :attr:`~GtkExitCode.CONFIG` for TOML, schema, or
            unsupported Glue version errors.

    """
    try:
        return load_pyproject(job_dir)
    except (FileNotFoundError, OSError) as err:
        raise GtkCommandError(
            GtkExitCode.NOINPUT,
            "Cannot read pyproject.toml",
            str(err),
        ) from None
    except (
        TOMLDecodeError,
        ValidationError,
        ValueError,
        UnsupportedGlueVersionError,
    ) as err:
        raise GtkCommandError(
            GtkExitCode.CONFIG,
            "Invalid pyproject.toml",
            str(err),
        ) from None


def gtk_command(
    fn: Callable[[GlueJobProject], Panel],
) -> Callable[[DirectoryPath], Panel | GtkExitCode]:
    """Register ``fn`` as a Cyclopts command with one ``job_dir`` argument.

    Loads the job via :func:`_load_job_project`, passes the resulting
    :class:`~aws_glue_toolkit.job.GlueJobProject` to ``fn``,
    and expects a success :class:`~rich.panel.Panel` return value (printed
    by Cyclopts; exit ``0``). :exc:`GtkCommandError` (from pyproject load
    or the command body) is rendered as a red Rich panel on
    :attr:`~cyclopts.App.error_console` with the matching
    :class:`GtkExitCode`.

    Copies ``__name__`` and ``__doc__`` from the inner function only (not
    ``functools.wraps``), so Cyclopts does not expose inner parameters
    (such as ``--job.name``) on the CLI.
    """

    def wrapper(job_dir: DirectoryPath = Path()) -> Panel | GtkExitCode:
        try:
            return fn(_load_job_project(job_dir))
        except GtkCommandError as err:
            app.error_console.print(
                Panel(
                    err.message,
                    title=f"[bold red]{err.title}[/]",
                    border_style="red",
                ),
            )
            return err.exit_code

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__

    return cast(
        "Callable[[DirectoryPath], Panel | GtkExitCode]",
        app.command(wrapper),
    )


# --- Commands ---


@gtk_command
def check(job: GlueJobProject) -> Panel:
    """Verify dependencies resolve for the job's Glue runtime.

    Runs :func:`~aws_glue_toolkit.pip.resolve_packages` with
    :attr:`~aws_glue_toolkit.job.GlueJobProject.runtime` pins, platform,
    and Python version. Raises :exc:`GtkCommandError` with
    :attr:`~GtkExitCode.DATAERR` when that call raises
    :exc:`~aws_glue_toolkit.pip.PipError`.
    """
    try:
        resolve_packages(
            job.dependencies,
            job.runtime.python_packages,
            python_version=job.runtime.core_engines.python,
            platform=job.runtime.pip_platform,
        )
    except PipError:
        raise GtkCommandError(
            GtkExitCode.DATAERR,
            "Conflicts detected",
            "Requirements are unsatisfiable with bundled Glue pins.",
        ) from None
    return Panel(
        "Dependencies resolve against Glue runtime pins.",
        title="[bold green]No conflicts[/]",
        border_style="green",
    )


@gtk_command
def build(job: GlueJobProject) -> Panel:
    """Build gluewheels and dependencies zips under the job directory.

    Calls :func:`~aws_glue_toolkit.artifacts.build_gluewheels_zip` and
    :func:`~aws_glue_toolkit.artifacts.build_dependencies_zip`. Raises
    :exc:`GtkCommandError` with :attr:`~GtkExitCode.SOFTWARE` when either
    builder raises :exc:`~aws_glue_toolkit.pip.PipError` or :exc:`OSError`.
    """
    try:
        build_dependencies_zip(
            job.source_dir,
            job.script,
            job.project_dir / job.dependencies_zip_filename,
        )
        build_gluewheels_zip(
            job.dependencies,
            job.runtime,
            job.project_dir / job.gluewheels_zip_filename,
        )
    except (OSError, PipError) as err:
        raise GtkCommandError(
            GtkExitCode.SOFTWARE,
            "Build failed",
            str(err),
        ) from None
    return Panel(
        f"Wrote zip files to {job.project_dir}.",
        title="[bold green]Build complete[/]",
        border_style="green",
    )
