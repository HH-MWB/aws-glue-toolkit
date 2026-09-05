"""``gtk`` — CLI for Glue job dependency check and wheel packaging.

Commands:

- ``check`` / ``build`` — ``--mode host`` (default) or ``container``
- ``run`` / ``test`` — ``--platform native`` (default) or ``worker``

``build|check --mode`` chooses where pip runs: ``host`` (default) local
pip for worker-oriented resolve/packaging (no Docker); ``container``
worker-arch Glue/build container (may need QEMU on ARM).
``run|test --platform`` chooses which Glue image arch to run: ``native``
(default) matches your machine; ``worker`` matches Glue job workers
(may need QEMU on ARM).

Example::

    gtk check ./my-glue-job
    gtk check ./my-glue-job --mode container
    gtk build ./my-glue-job
    gtk build ./my-glue-job --mode container
    gtk run ./my-glue-job
    gtk run ./my-glue-job --platform worker
    gtk test ./my-glue-job
    gtk test ./my-glue-job --platform worker

"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import TYPE_CHECKING, Annotated, Literal, TypeVar

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
from aws_glue_toolkit.job import load_pyproject
from aws_glue_toolkit.runtime import UnsupportedGlueVersionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from aws_glue_toolkit.job import GlueJobProject

__all__ = ["app"]

T = TypeVar("T")

# --- App ---

app = App(
    help=(
        "AWS Glue development lifecycle toolkit. "
        "build|check --mode chooses where pip runs (host|container); "
        "run|test --platform chooses Glue image arch (native|worker)."
    ),
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
        title: Short panel header (rendered bold red).
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


def _docker_unavailable(
    err: DockerError,
    *,
    worker_platform: str | None = None,
) -> GtkCommandError:
    """Map :exc:`DockerError` to a CLI failure panel."""
    message = str(err)
    if worker_platform is not None:
        message = (
            f"{message}\n"
            f"Could not run worker-arch Docker ({worker_platform}). "
            "On ARM hosts this usually needs QEMU (or an amd64 machine)."
        )
    return GtkCommandError(
        GtkExitCode.UNAVAILABLE,
        "Docker unavailable",
        message,
    )


def _invalid_dependency(err: RequirementPreparationError) -> GtkCommandError:
    """Map :exc:`RequirementPreparationError` to a CLI failure panel."""
    return GtkCommandError(
        GtkExitCode.CONFIG,
        "Invalid dependency",
        str(err),
    )


def _handle_dependency_errors(
    fn: Callable[[], T],
    *,
    worker_platform: str | None = None,
) -> T:
    """Run ``fn``; map Docker and dep prep errors to GtkCommandError."""
    try:
        return fn()
    except DockerError as err:
        raise _docker_unavailable(
            err,
            worker_platform=worker_platform,
        ) from None
    except RequirementPreparationError as err:
        raise _invalid_dependency(err) from None


def _run_or_test(  # noqa: PLR0915  # pylint: disable=too-many-statements
    job_dir: Path,
    platform: Literal["native", "worker"],
    invoke: Callable[[GlueJobProject], int],
) -> int:
    """Load job, invoke run/test, map errors to panels / exit codes."""
    try:
        job = _load_job_project(job_dir)
        worker_platform = (
            job.runtime.worker_docker_platform
            if platform == "worker"
            else None
        )
        try:
            return _handle_dependency_errors(
                lambda: invoke(job),
                worker_platform=worker_platform,
            )
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
    except GtkCommandError as err:
        _print_command_error(err)
        return int(err.exit_code)


# --- Commands ---


@app.command
def check(
    job_dir: DirectoryPath = Path(),
    mode: Annotated[
        Literal["host", "container"],
        Parameter(
            name="--mode",
            help=(
                "host (default; no Docker) or container dry-run "
                "(worker-arch; needs Docker)."
            ),
        ),
    ] = "host",
) -> Panel | int:
    """Verify dependencies against Glue runtime pins.

    ``--mode host`` (default): same gluewheels recipe as ``build --mode
    host`` (temp dir, discarded). ``--mode container``: dry-run in the Glue
    image. Does not write zip artifacts.

    """
    try:
        job = _load_job_project(job_dir)
        worker_platform = (
            job.runtime.worker_docker_platform if mode == "container" else None
        )
        try:
            _handle_dependency_errors(
                lambda: check_job(job, mode=mode),
                worker_platform=worker_platform,
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
    except GtkCommandError as err:
        _print_command_error(err)
        return int(err.exit_code)


@app.command
def build(
    job_dir: DirectoryPath = Path(),
    mode: Annotated[
        Literal["host", "container"],
        Parameter(
            name="--mode",
            help=(
                "host pip (default) or Glue container "
                "(worker-arch; needs Docker)."
            ),
        ),
    ] = "host",
) -> Panel | int:
    """Build gluewheels and dependencies zips under the job directory.

    Writes ``{name}-{version}.dependencies.zip`` and
    ``{name}-{version}.gluewheels.zip``. Default ``--mode host``: host
    ``pip wheel --no-deps`` for path/VCS; ``pip download --platform`` or
    sdist→wheel for other packages (portable tags only). ``--mode
    container``: ``pip wheel`` in the Glue image (worker-arch). Omits Glue
    image pins.

    """
    try:
        job = _load_job_project(job_dir)
        worker_platform = (
            job.runtime.worker_docker_platform if mode == "container" else None
        )
        try:
            project_dir = _handle_dependency_errors(
                lambda: build_job(job, mode=mode),
                worker_platform=worker_platform,
            )
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
    except GtkCommandError as err:
        _print_command_error(err)
        return int(err.exit_code)


@app.command
def run(  # pylint: disable=keyword-arg-before-vararg
    job_dir: DirectoryPath = Path(),
    platform: Annotated[
        Literal["native", "worker"],
        Parameter(
            name="--platform",
            help=(
                "native (default; host arch via Docker multi-arch) or "
                "worker (Glue job workers; may need QEMU on ARM)."
            ),
        ),
    ] = "native",
    *job_args: Annotated[str, Parameter(allow_leading_hyphen=True)],
) -> int:
    """Run the job in the official AWS Glue local Docker image.

    Installs job dependencies into the ephemeral container when present.
    Passes ``--JOB_NAME`` from ``project.name`` unless overridden.
    Forwards additional tokens after ``job_dir`` to the job for
    ``getResolvedOptions``. Returns when the job finishes; container stdout
    and stderr pass through unchanged.

    """
    return _run_or_test(
        job_dir,
        platform,
        lambda job: run_job(job, *job_args, platform=platform),
    )


@app.command(name="test")
def pytest_command(  # pylint: disable=keyword-arg-before-vararg
    job_dir: DirectoryPath = Path(),
    platform: Annotated[
        Literal["native", "worker"],
        Parameter(
            name="--platform",
            help=(
                "native (default; host arch via Docker multi-arch) or "
                "worker (Glue job workers; may need QEMU on ARM)."
            ),
        ),
    ] = "native",
    *pytest_args: Annotated[str, Parameter(allow_leading_hyphen=True)],
) -> int:
    """Run pytest in the official AWS Glue local Docker image.

    Installs job dependencies into the ephemeral container when present.
    Uses ``tool.aws-glue-toolkit.tests`` and sets ``PYTHONPATH`` to
    ``source``. Forwards additional tokens after ``job_dir`` to ``pytest``.
    Container stdout and stderr pass through unchanged.

    """
    return _run_or_test(
        job_dir,
        platform,
        lambda job: test_job(job, *pytest_args, platform=platform),
    )
