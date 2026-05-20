"""Cyclopts CLI for the AWS Glue Toolkit (``gtk``).

Registers commands that read a Glue job ``pyproject.toml``, validate it, and
resolve dependencies against bundled runtime pins via ``uv pip compile``.

``gtk check`` returns :attr:`GtkExitCode.OK` (panel on :data:`app.console`) or
:attr:`GtkExitCode.DEPENDENCY_CONFLICT` (panel on :data:`app.error_console`).
Other failures raise :class:`GlueToolkitError` (codes 2-6 on
:data:`app.error_console`; see :class:`GtkExitCode`).

Example:
    gtk check
    gtk check ./my-glue-job

"""

from __future__ import annotations

from enum import IntEnum
from functools import wraps
from pathlib import Path
from shutil import which
from tomllib import TOMLDecodeError, loads
from typing import TYPE_CHECKING, ParamSpec, cast

from cyclopts import App
from pydantic import ValidationError
from pydantic.types import (
    DirectoryPath,  # noqa: TC002  # CLI coercion needs runtime type
)
from rich.panel import Panel

from aws_glue_toolkit.glue_pyproject import PyProject
from aws_glue_toolkit.glue_resolve import (
    UvPipCompileError,
    resolve_glue_dependencies,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["app"]

P = ParamSpec("P")


class GtkExitCode(IntEnum):
    """Process exit codes for ``gtk`` commands.

    ``check`` returns :attr:`OK` or :attr:`DEPENDENCY_CONFLICT` as its ``int``
    result. :attr:`MISSING_PYPROJECT` through :attr:`UV_NOT_FOUND` are carried
    on :class:`GlueToolkitError`; :func:`gtk_command` prints
    :attr:`GlueToolkitError.error_message` and returns the code.

    """

    OK = 0
    DEPENDENCY_CONFLICT = 1
    MISSING_PYPROJECT = 2
    PYPROJECT_UNREADABLE = 3
    INVALID_TOML = 4
    INVALID_PYPROJECT = 5
    UV_NOT_FOUND = 6


class GlueToolkitError(Exception):
    """Expected CLI failure with a user-facing message and exit code.

    Attributes:
        exit_code: Process exit code (:class:`GtkExitCode` as ``int``).
        error_message: Text printed via :data:`app.error_console` by
            :func:`gtk_command`.

    """

    def __init__(self, exit_code: GtkExitCode, error_message: str) -> None:
        """Store ``exit_code`` and ``error_message`` for the CLI shell.

        Args:
            exit_code: Exit status for Cyclopts (see :class:`GtkExitCode`).
            error_message: Plain text for :data:`app.error_console`.

        """
        self.exit_code = int(exit_code)
        self.error_message = error_message
        super().__init__()


app = App(
    help="AWS Glue development lifecycle toolkit.",
    result_action="print_non_int_sys_exit",
)


def gtk_command(fn: Callable[P, int]) -> Callable[P, int]:
    """Register on :data:`app` with :class:`GlueToolkitError` handling.

    Wraps ``fn`` so :class:`GlueToolkitError` prints
    :attr:`GlueToolkitError.error_message` on :data:`app.error_console` and
    returns :attr:`GlueToolkitError.exit_code`; otherwise returns ``fn``'s
    ``int``.

    Args:
        fn: Command that returns a process exit code.

    Returns:
        Registered command (via :meth:`App.command`).

    """

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return fn(*args, **kwargs)
        except GlueToolkitError as exc:
            app.error_console.print(exc.error_message)
            return exc.exit_code

    return cast("Callable[P, int]", app.command(wrapper))


def read_pyproject(project_dir: Path) -> str:
    """Read ``project_dir/pyproject.toml`` as UTF-8 text.

    Args:
        project_dir: Glue job root directory.

    Returns:
        Raw file contents.

    Raises:
        GlueToolkitError: Missing file (:attr:`GtkExitCode.MISSING_PYPROJECT`)
            or OS read error (:attr:`GtkExitCode.PYPROJECT_UNREADABLE`).

    """
    path = project_dir / "pyproject.toml"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise GlueToolkitError(
            GtkExitCode.MISSING_PYPROJECT,
            f"no pyproject.toml at {path}",
        ) from e
    except OSError as e:
        raise GlueToolkitError(
            GtkExitCode.PYPROJECT_UNREADABLE,
            f"cannot read pyproject.toml at {path}: {e}",
        ) from e


def parse_pyproject(text: str) -> PyProject:
    """Parse TOML into :class:`~aws_glue_toolkit.glue_pyproject.PyProject`.

    Args:
        text: Raw ``pyproject.toml`` body.

    Returns:
        Validated project model.

    Raises:
        GlueToolkitError: Invalid TOML (:attr:`GtkExitCode.INVALID_TOML`) or
            schema (:attr:`GtkExitCode.INVALID_PYPROJECT`), including bad
            ``glue_version``.

    """
    try:
        return PyProject.model_validate(loads(text))
    except TOMLDecodeError as e:
        raise GlueToolkitError(
            GtkExitCode.INVALID_TOML,
            f"invalid TOML: {e}",
        ) from e
    except ValidationError as e:
        raise GlueToolkitError(
            GtkExitCode.INVALID_PYPROJECT,
            f"invalid pyproject: {e}",
        ) from e


def get_uv_executable() -> str:
    """Locate the ``uv`` executable on ``PATH``.

    Returns:
        Absolute path to ``uv``.

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


@gtk_command
def check(project_dir: DirectoryPath = Path()) -> int:
    """Check job dependencies against bundled Glue runtime pins.

    Reads ``pyproject.toml`` under ``project_dir`` and runs
    :func:`~aws_glue_toolkit.glue_resolve.resolve_glue_dependencies`.

    Args:
        project_dir: Glue job root (contains ``pyproject.toml``). Defaults to
            the current working directory.

    Returns:
        :attr:`GtkExitCode.OK` when compile succeeds (green Rich panel on
        :data:`app.console`). :attr:`GtkExitCode.DEPENDENCY_CONFLICT` when
        requirements are unsatisfiable (red panel on :data:`app.error_console`;
        ``uv`` stderr is not shown).

    Raises:
        GlueToolkitError: Missing or invalid project file, or ``uv`` not on
            ``PATH`` (exit codes :attr:`GtkExitCode.MISSING_PYPROJECT` through
            :attr:`GtkExitCode.UV_NOT_FOUND` via :func:`gtk_command`).

    """
    pyproject = parse_pyproject(read_pyproject(project_dir.resolve()))
    try:
        resolve_glue_dependencies(pyproject, uv_exe=get_uv_executable())
    except UvPipCompileError:
        app.error_console.print(
            Panel(
                "Requirements are unsatisfiable with bundled Glue pins.",
                title="[bold red]Conflicts detected[/]",
                border_style="red",
            ),
        )
        return GtkExitCode.DEPENDENCY_CONFLICT
    app.console.print(
        Panel(
            "Dependencies resolve against Glue runtime pins.",
            title="[bold green]No conflicts[/]",
            border_style="green",
        ),
    )
    return GtkExitCode.OK
