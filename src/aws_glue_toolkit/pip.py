"""Run ``pip`` for Glue job dependency check and wheel download.

Requires ``pip>=22.2`` (see ``pyproject.toml``) for ``--dry-run --report``.

Pass a :class:`PipRunner` from :mod:`aws_glue_toolkit.workflows` to run pip
inside the official Glue local Docker image; omit ``runner`` to use host pip.

Public API: :class:`PipExecutionContext`, :class:`PipRunner`,
:func:`packages_not_on_image`, :func:`resolve_packages`,
:func:`resolve_packages_to_bundle`, :func:`download_wheels`, :exc:`PipError`.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`_pip_run_host`.

"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from json import loads
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec B404
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_glue_toolkit.runtime import GlueRuntimeMetadata

PipRunner = Callable[[Sequence[str], Sequence[tuple[Path, str]]], None]

__all__ = [
    "PipError",
    "PipExecutionContext",
    "PipRunner",
    "download_wheels",
    "packages_not_on_image",
    "pip_error_from_returncode",
    "resolve_packages",
    "resolve_packages_to_bundle",
]


# --- Execution context ---


@dataclass(frozen=True, slots=True)
class PipExecutionContext:
    """Glue Docker image and job directory for pip-in-container execution."""

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


def pip_error_from_returncode(
    returncode: int,
    *,
    stdout: str | None = None,
    stderr: str | None = None,
) -> PipError:
    """Build :exc:`PipError` from a non-zero subprocess result."""
    message = (
        (stderr or "").strip()
        or (stdout or "").strip()
        or f"command exited with status {returncode}"
    )
    return PipError(
        message,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


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
        raise pip_error_from_returncode(
            exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
        ) from exc


def _pip_run(
    *args: str,
    runner: PipRunner | None = None,
    volume_mounts: Sequence[tuple[Path, str]] = (),
) -> None:
    """Run pip on the host or via an injected runner."""
    if runner is None:
        _pip_run_host(*args)
        return
    runner(args, volume_mounts)


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


def packages_not_on_image(
    resolved: Mapping[str, str],
    runtime_pins: Mapping[str, str],
) -> dict[str, str]:
    """Return resolved packages whose version differs from Glue image pins."""
    return {
        name: version
        for name, version in resolved.items()
        if runtime_pins.get(name) != version
    }


def resolve_packages(
    requirement_specs: Sequence[str],
    runtime: GlueRuntimeMetadata,
    *,
    runner: PipRunner | None = None,
    pip_work_mount: str = "/tmp/gtk-pip-work",  # noqa: S108  # nosec B108
) -> dict[str, str]:
    """Run ``pip install --dry-run --report`` and return resolved packages.

    Writes a temp ``requirements.in`` and ``constraints.txt``, then parses
    ``install[].metadata`` from pip's installation report JSON.

    Args:
        requirement_specs: Direct dependency requirements (PEP 508 strings).
        runtime: Bundled Glue runtime metadata (pins, Python, platform).
        runner: When set, invoke pip via this callback (for example in Docker).
        pip_work_mount: Container path for the pip work directory when
            ``runner`` is set; must match :mod:`aws_glue_toolkit.docker`.

    Returns:
        Resolved packages (``name`` → ``version``).

    Raises:
        PipError: ``pip install --dry-run`` exited with an error.
        ValidationError: Installation report JSON did not match the expected
            shape.

    """
    python_version = runtime.core_engines.python
    with _pip_workspace(requirement_specs, runtime.python_packages) as work:
        volume_mounts = [(work, pip_work_mount)] if runner is not None else ()
        _pip_run(
            "install",
            "--requirement",
            str(work / "requirements.in"),
            "--constraint",
            str(work / "constraints.txt"),
            "--dry-run",
            "--ignore-installed",
            "--platform",
            runtime.pip_platform,
            "--python-version",
            python_version.replace(".", ""),
            "--only-binary=:all:",
            "--quiet",
            "--report",
            str(work / "report.json"),
            runner=runner,
            volume_mounts=volume_mounts,
        )
        report_text = (work / "report.json").read_text(encoding="utf-8")

    parsed = _InstallationReport.model_validate(loads(report_text))
    return {
        item.metadata.name: item.metadata.version for item in parsed.install
    }


def resolve_packages_to_bundle(
    requirement_specs: Sequence[str],
    runtime: GlueRuntimeMetadata,
    *,
    runner: PipRunner | None = None,
    pip_work_mount: str = "/tmp/gtk-pip-work",  # noqa: S108  # nosec B108
) -> dict[str, str]:
    """Resolve requirements and omit packages pinned on the Glue image."""
    resolved = resolve_packages(
        requirement_specs,
        runtime,
        runner=runner,
        pip_work_mount=pip_work_mount,
    )
    return packages_not_on_image(resolved, runtime.python_packages)


def download_wheels(
    packages: Mapping[str, str],
    dest: Path,
    runtime: GlueRuntimeMetadata,
    *,
    runner: PipRunner | None = None,
    gluewheels_staging_mount: str = (
        "/tmp/gtk-staging"  # noqa: S108  # nosec B108
    ),
) -> None:
    """Download pinned wheels for all packages into ``dest``.

    Runs ``pip download --no-deps --only-binary=:all:`` once per package,
    sequentially.

    Args:
        packages: Package name → version pins.
        dest: Directory to write ``.whl`` files into (created if missing).
        runtime: Bundled Glue runtime metadata (Python, platform).
        runner: When set, invoke pip via this callback (for example in Docker).
        gluewheels_staging_mount: Container path for wheel download staging
            when ``runner`` is set; must match :mod:`aws_glue_toolkit.docker`.

    Raises:
        PipError: Any ``pip download`` subprocess exited with an error.

    """
    python_version = runtime.core_engines.python
    dest.mkdir(parents=True, exist_ok=True)
    volume_mounts = (
        [(dest.parent, gluewheels_staging_mount)] if runner is not None else ()
    )
    for name, version in packages.items():
        _pip_run(
            "download",
            "--no-deps",
            "--only-binary=:all:",
            "--dest",
            str(dest),
            "--platform",
            runtime.pip_platform,
            "--python-version",
            python_version.replace(".", ""),
            f"{name}=={version}",
            runner=runner,
            volume_mounts=volume_mounts,
        )
