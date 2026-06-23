"""Run ``pip`` for Glue job dependency check and wheel download.

Requires ``pip>=22.2`` (see ``pyproject.toml``) for ``--dry-run --report``.

Callers supply ``platform``, ``python_version``, and package pins from
:class:`~aws_glue_toolkit.runtime.GlueRuntimeMetadata` (for example
:attr:`~aws_glue_toolkit.job.GlueJobProject.runtime` in the CLI). The CLI
runs pip inside the official Glue local Docker image via
:class:`PipExecutionContext`. ``check`` maps :exc:`PipError` to
:attr:`~aws_glue_toolkit.cli.GtkExitCode.DATAERR`; ``build`` maps it to
:attr:`~aws_glue_toolkit.cli.GtkExitCode.SOFTWARE`.

Public API: :class:`PipExecutionContext`, :func:`resolve_packages`,
:func:`download_wheels`, :exc:`PipError`.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`_pip_run_host`.

"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from json import loads
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec B404
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from aws_glue_toolkit.docker import (
    GLUEWHEELS_STAGING_MOUNT,
    PIP_WORK_MOUNT,
    run_pip_in_container,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "PipError",
    "PipExecutionContext",
    "download_wheels",
    "resolve_packages",
]


# --- Execution context ---


@dataclass(frozen=True, slots=True)
class PipExecutionContext:
    """Run pip inside the official Glue local Docker image."""

    docker_image: str
    project_dir: Path


# --- Exceptions ---


class PipError(Exception):
    """``pip`` subprocess failed during resolution or download.

    Attributes:
        returncode: Subprocess exit code, if the failure came from pip.
        stdout: Captured standard output, if any.
        stderr: Captured standard error, if any.

    """

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        """Initialize from a message and optional subprocess fields."""
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(message)


# --- pip subprocess ---


@contextmanager
def _pip_workspace(
    requirement_specs: Sequence[str],
    runtime_package_pins: Mapping[str, str],
) -> Iterator[Path]:
    """Provide a temporary directory prepared for ``pip`` resolution."""
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "requirements.in").write_text(
            "\n".join(requirement_specs),
            encoding="utf-8",
        )
        (work / "constraints.txt").write_text(
            "\n".join(
                f"{name}=={version}"
                for name, version in runtime_package_pins.items()
            ),
            encoding="utf-8",
        )
        yield work


def _pip_run_host(*args: str) -> None:
    """Run host ``python -m pip`` with ``args``.

    Raises:
        PipError: The subprocess exited with a non-zero status.

    """
    try:
        run(  # noqa: S603
            [sys.executable, "-m", "pip", *args],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )  # nosec B603
    except CalledProcessError as exc:
        message = (
            (exc.stderr or "").strip()
            or (exc.stdout or "").strip()
            or f"command exited with status {exc.returncode}"
        )
        raise PipError(
            message,
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
        ) from exc


def _pip_run_docker(
    execution: PipExecutionContext,
    volume_mounts: Sequence[tuple[Path, str]],
    *args: str,
) -> None:
    """Run ``python3 -m pip`` in the Glue container.

    Raises:
        PipError: Pip exited with a non-zero status.

    """
    result = run_pip_in_container(
        execution.docker_image,
        execution.project_dir,
        args,
        volume_mounts=volume_mounts,
    )
    if result.returncode != 0:
        message = (
            (result.stderr or "").strip()
            or (result.stdout or "").strip()
            or f"command exited with status {result.returncode}"
        )
        raise PipError(
            message,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def _pip_run(
    *args: str,
    execution: PipExecutionContext | None = None,
    volume_mounts: Sequence[tuple[Path, str]] = (),
) -> None:
    """Run pip on the host or inside the Glue Docker image."""
    if execution is None:
        _pip_run_host(*args)
        return
    _pip_run_docker(execution, volume_mounts, *args)


# --- Install report models ---


class _InstallMetadata(BaseModel):
    """``install[].metadata`` in a ``pip install --report`` JSON file."""

    model_config = ConfigDict(extra="ignore")

    name: str
    version: str


class _InstallItem(BaseModel):
    """Single ``install[]`` entry in a pip installation report."""

    model_config = ConfigDict(extra="ignore")

    metadata: _InstallMetadata


class _InstallationReport(BaseModel):
    """Top-level ``pip install --report`` JSON document."""

    model_config = ConfigDict(extra="ignore")

    install: list[_InstallItem]


# --- Public API ---


def resolve_packages(
    requirement_specs: Sequence[str],
    runtime_package_pins: Mapping[str, str],
    *,
    python_version: str,
    platform: str,
    execution: PipExecutionContext | None = None,
) -> dict[str, str]:
    """Run ``pip install --dry-run --report`` and return resolved packages.

    Writes a temp ``requirements.in`` and ``constraints.txt``, then parses
    ``install[].metadata`` from pip's installation report JSON.

    Args:
        requirement_specs: Direct dependency requirements (PEP 508 strings).
        runtime_package_pins: Bundled Glue runtime pins
            (``name`` → ``version``).
        python_version: Target Python (e.g. ``"3.11"``); dots are stripped for
            ``--python-version``.
        platform: ``pip install --platform`` value (e.g.
            ``"manylinux2014_x86_64"``).
        execution: When set, run pip in the Glue Docker image.

    Returns:
        Resolved packages (``name`` → ``version``).

    Raises:
        PipError: ``pip install --dry-run`` exited with an error.
        ValidationError: Installation report JSON did not match the expected
            shape.

    """
    with _pip_workspace(requirement_specs, runtime_package_pins) as work:
        volume_mounts = (
            [(work, PIP_WORK_MOUNT)] if execution is not None else ()
        )
        _pip_run(
            "install",
            "--requirement",
            str(work / "requirements.in"),
            "--constraint",
            str(work / "constraints.txt"),
            "--dry-run",
            "--ignore-installed",
            "--platform",
            platform,
            "--python-version",
            python_version.replace(".", ""),
            "--only-binary=:all:",
            "--quiet",
            "--report",
            str(work / "report.json"),
            execution=execution,
            volume_mounts=volume_mounts,
        )
        report_text = (work / "report.json").read_text(encoding="utf-8")

    parsed = _InstallationReport.model_validate(loads(report_text))
    return {
        item.metadata.name: item.metadata.version for item in parsed.install
    }


def download_wheels(
    packages: Mapping[str, str],
    dest: Path,
    *,
    python_version: str,
    platform: str,
    execution: PipExecutionContext | None = None,
) -> None:
    """Download pinned wheels for all packages into ``dest``.

    Runs ``pip download --no-deps --only-binary=:all:`` once per package,
    sequentially.

    Args:
        packages: Package name → version pins.
        dest: Directory to write ``.whl`` files into (created if missing).
        python_version: Target Python (e.g. ``"3.11"``); dots are stripped for
            ``--python-version``.
        platform: ``pip download --platform`` value.
        execution: When set, run pip in the Glue Docker image and mount
            ``dest.parent`` at
            :data:`~aws_glue_toolkit.docker.GLUEWHEELS_STAGING_MOUNT`.

    Raises:
        PipError: Any ``pip download`` subprocess exited with an error.

    """
    dest.mkdir(parents=True, exist_ok=True)
    volume_mounts = (
        [(dest.parent, GLUEWHEELS_STAGING_MOUNT)]
        if execution is not None
        else ()
    )
    for name, version in packages.items():
        _pip_run(
            "download",
            "--no-deps",
            "--only-binary=:all:",
            "--dest",
            str(dest),
            "--platform",
            platform,
            "--python-version",
            python_version.replace(".", ""),
            f"{name}=={version}",
            execution=execution,
            volume_mounts=volume_mounts,
        )
