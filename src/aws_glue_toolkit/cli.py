"""``gtk`` — CLI for Glue job dependency check and wheel packaging.

Commands:

- ``check`` — resolve dependencies in the official AWS Glue local Docker image
- ``build`` — write ``.gluewheels.zip`` and ``.dependencies.zip`` artifacts
- ``run`` — execute the job in the official AWS Glue local Docker image
- ``test`` — run pytest in the official AWS Glue local Docker image

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
    gtk run ./my-glue-job
    gtk test ./my-glue-job

"""

from __future__ import annotations

from enum import IntEnum
from inspect import Parameter as InspectParameter
from inspect import signature
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import (
    TYPE_CHECKING,
    Annotated,
    TypeAlias,
    TypeVar,
    Unpack,
    cast,
    overload,
)

from cyclopts import App, Parameter
from pydantic import ValidationError
from pydantic.types import (
    DirectoryPath,  # noqa: TC002  # CLI coercion needs runtime type
)
from rich.panel import Panel

from aws_glue_toolkit.app import build as build_job
from aws_glue_toolkit.app import check as check_job
from aws_glue_toolkit.app import run as run_job
from aws_glue_toolkit.app import test as test_job
from aws_glue_toolkit.dependencies import PipError, RequirementPreparationError
from aws_glue_toolkit.docker import DockerError
from aws_glue_toolkit.job import GlueJobProject, load_pyproject
from aws_glue_toolkit.runtime import UnsupportedGlueVersionError

if TYPE_CHECKING:
    from collections.abc import Callable

    ForwardedArg: TypeAlias = Annotated[
        str,
        Parameter(allow_leading_hyphen=True),
    ]
    GtkPanelCommand: TypeAlias = Callable[[GlueJobProject], Panel]
    GtkVarargsCommand: TypeAlias = Callable[
        [GlueJobProject, Unpack[tuple[str, ...]]],
        int,
    ]
    GtkForwardingWrapper: TypeAlias = Callable[
        [DirectoryPath, Unpack[tuple[ForwardedArg, ...]]],
        Panel | int,
    ]

__all__ = ["app"]

T = TypeVar("T")

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
    UNAVAILABLE = 69  # docker unavailable or launch failure
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


def _print_command_error(err: GtkCommandError) -> None:
    """Render a :exc:`GtkCommandError` as a red Rich panel."""
    app.error_console.print(
        Panel(
            err.message,
            title=f"[bold red]{err.title}[/]",
            border_style="red",
        ),
    )


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
        # Missing or unreadable pyproject.toml.
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
        # Invalid TOML, schema, layout, or unsupported Glue version.
        raise GtkCommandError(
            GtkExitCode.CONFIG,
            "Invalid pyproject.toml",
            str(err),
        ) from None


def _docker_unavailable(err: DockerError) -> GtkCommandError:
    """Map :exc:`DockerError` to a CLI failure panel."""
    return GtkCommandError(
        GtkExitCode.UNAVAILABLE,
        "Docker unavailable",
        str(err),
    )


def _invalid_dependency(err: RequirementPreparationError) -> GtkCommandError:
    """Map :exc:`RequirementPreparationError` to a CLI failure panel."""
    return GtkCommandError(
        GtkExitCode.CONFIG,
        "Invalid dependency",
        str(err),
    )


def _handle_dependency_errors(fn: Callable[[], T]) -> T:
    """Run ``fn``; map Docker and dep prep errors to GtkCommandError."""
    try:
        return fn()
    except DockerError as err:
        raise _docker_unavailable(err) from None
    except RequirementPreparationError as err:
        raise _invalid_dependency(err) from None


def _gtk_command_body(
    fn: GtkPanelCommand | GtkVarargsCommand,
    job_dir: DirectoryPath,
    forwarded: tuple[str, ...],
) -> Panel | int:
    """Load the job, invoke ``fn``, and map GtkCommandError to exit codes."""
    try:
        job = _load_job_project(job_dir)

        if forwarded:
            return fn(job, *forwarded)
        return fn(job)
    except GtkCommandError as err:
        _print_command_error(err)
        return int(err.exit_code)


def _register_simple_gtk_command(
    fn: GtkPanelCommand,
) -> Callable[[DirectoryPath], Panel | int]:
    """Register a Cyclopts command with only ``job_dir`` on the CLI."""

    def wrapper(job_dir: DirectoryPath = Path()) -> Panel | int:
        return _gtk_command_body(fn, job_dir, ())

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return cast(
        "Callable[[DirectoryPath], Panel | int]",
        app.command(wrapper),
    )


def _register_forwarding_gtk_command(
    fn: GtkVarargsCommand,
) -> GtkForwardingWrapper:
    """Register a Cyclopts command that forwards trailing CLI tokens."""

    def wrapper(  # pylint: disable=keyword-arg-before-vararg
        job_dir: DirectoryPath = Path(),
        *forwarded: Annotated[str, Parameter(allow_leading_hyphen=True)],
    ) -> Panel | int:
        return _gtk_command_body(fn, job_dir, forwarded)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return app.command(wrapper)


@overload
def gtk_command(
    fn: GtkPanelCommand,
) -> Callable[[DirectoryPath], Panel | int]: ...


@overload
def gtk_command(fn: GtkVarargsCommand) -> GtkForwardingWrapper: ...


def gtk_command(
    fn: GtkPanelCommand | GtkVarargsCommand,
) -> Callable[[DirectoryPath], Panel | int] | GtkForwardingWrapper:
    """Register ``fn`` as a Cyclopts command with one ``job_dir`` argument.

    Loads the job via :func:`_load_job_project`, passes the resulting
    :class:`~aws_glue_toolkit.job.GlueJobProject` to ``fn``, and returns
    its result. Inner functions without ``*varargs`` take only ``job``;
    those with ``*varargs`` receive tokens forwarded from the CLI after
    ``job_dir`` (via ``Parameter(allow_leading_hyphen=True)``).

    Success :class:`~rich.panel.Panel` values are printed by Cyclopts (exit
    ``0``); integer return values become the process exit code (for example
    container or pytest status). :exc:`GtkCommandError` (from pyproject
    load or the command body) is rendered as a red Rich panel on
    :attr:`~cyclopts.App.error_console` with the matching
    :class:`GtkExitCode`.

    Copies ``__name__`` and ``__doc__`` from the inner function only (not
    ``functools.wraps``), so Cyclopts does not expose inner parameters
    (such as ``--job.name``) on the CLI.

    """
    has_varargs = any(
        p.kind == InspectParameter.VAR_POSITIONAL
        for p in signature(fn).parameters.values()
    )
    if has_varargs:
        return _register_forwarding_gtk_command(cast("GtkVarargsCommand", fn))
    return _register_simple_gtk_command(cast("GtkPanelCommand", fn))


# --- Commands ---


@gtk_command
def check(job: GlueJobProject) -> Panel:
    """Verify dependencies resolve for the job's Glue runtime.

    Resolves job dependencies inside the official AWS Glue local Docker
    image against bundled Glue runtime pins. Supports PyPI, ``file:`` path,
    and git/VCS direct references in ``project.dependencies``.

    """
    try:
        _handle_dependency_errors(lambda: check_job(job))
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

    Writes ``{name}-{version}.dependencies.zip`` and
    ``{name}-{version}.gluewheels.zip``. Bundles wheels from PyPI, ``file:``
    path, and git/VCS dependencies inside the official AWS Glue local Docker
    image. Omits packages already pinned on the Glue image.

    """
    try:
        project_dir = _handle_dependency_errors(lambda: build_job(job))
    except (OSError, PipError) as err:
        raise GtkCommandError(
            GtkExitCode.SOFTWARE,
            "Build failed",
            str(err),
        ) from None
    return Panel(
        f"Wrote zip files to {project_dir}.",
        title="[bold green]Build complete[/]",
        border_style="green",
    )


@gtk_command
def run(job: GlueJobProject, *job_args: str) -> int:
    """Run the job in the official AWS Glue local Docker image.

    Installs ``project.dependencies`` into the ephemeral container when
    present. Passes ``--JOB_NAME`` from ``project.name`` unless overridden.
    Forwards additional tokens after ``job_dir`` to ``spark-submit`` for
    ``getResolvedOptions``. Container stdout and stderr pass through
    unchanged.

    """
    try:
        return _handle_dependency_errors(lambda: run_job(job, *job_args))
    except PipError as err:
        raise GtkCommandError(
            GtkExitCode.DATAERR,
            "Dependency install failed",
            str(err),
        ) from None


@gtk_command
def test(job: GlueJobProject, *pytest_args: str) -> int:
    """Run pytest in the official AWS Glue local Docker image.

    Installs ``project.dependencies`` into the ephemeral container when
    present. Uses ``tool.aws-glue-toolkit.tests`` and sets ``PYTHONPATH`` to
    ``source``. Forwards additional tokens after ``job_dir`` to ``pytest``.
    Container stdout and stderr pass through unchanged.

    """
    try:
        return _handle_dependency_errors(lambda: test_job(job, *pytest_args))
    except ValueError as err:
        raise GtkCommandError(
            GtkExitCode.CONFIG,
            "Tests directory not found",
            str(err),
        ) from None
    except PipError as err:
        raise GtkCommandError(
            GtkExitCode.DATAERR,
            "Dependency install failed",
            str(err),
        ) from None
