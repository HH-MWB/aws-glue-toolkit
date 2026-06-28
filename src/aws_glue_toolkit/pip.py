"""Run ``pip`` for Glue job dependency check and wheel bundling.

Requires ``pip>=22.2`` (see ``pyproject.toml``) for ``--dry-run --report``.

Pass a :class:`PipRunner` from :mod:`aws_glue_toolkit.workflows` to run pip
inside the official Glue local Docker image; omit ``runner`` to use host pip.

Public API: :class:`PipInContainerConfig`, :class:`PipRunner`,
:func:`resolve_packages`, :func:`bundle_wheels`, :exc:`PipError`.

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

from packaging.utils import parse_wheel_filename
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_glue_toolkit.requirements import PreparedRequirements
    from aws_glue_toolkit.runtime import GlueRuntimeMetadata

PipRunner = Callable[[Sequence[str], Sequence[tuple[Path, str]]], None]

__all__ = [
    "PipError",
    "PipInContainerConfig",
    "PipRunner",
    "bundle_wheels",
    "pip_error_from_returncode",
    "resolve_packages",
]

_PIP_WORK_MOUNT = "/tmp/gtk-pip-work"  # noqa: S108  # nosec B108
_GLUEWHEELS_STAGING_MOUNT = "/tmp/gtk-staging"  # noqa: S108  # nosec B108


# --- Exceptions ---


class PipError(Exception):
    """``pip`` subprocess failed during resolution or wheel bundling.

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
    """Provide a temporary directory prepared for ``pip``."""
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


def _volume_mounts_for(
    work: Path,
    pip_work_mount: str,
    prepared: PreparedRequirements,
    *,
    staging_parent: Path | None = None,
    gluewheels_staging_mount: str | None = None,
) -> list[tuple[Path, str]]:
    mounts: list[tuple[Path, str]] = [(work, pip_work_mount)]
    if staging_parent is not None and gluewheels_staging_mount is not None:
        mounts.append((staging_parent, gluewheels_staging_mount))
    mounts.extend(prepared.extra_mounts)
    return mounts


def _dry_run_args(
    work: Path,
    runtime: GlueRuntimeMetadata,
    *,
    runner: PipRunner | None,
) -> list[str]:
    args = [
        "install",
        "--requirement",
        str(work / "requirements.in"),
        "--constraint",
        str(work / "constraints.txt"),
        "--dry-run",
        "--ignore-installed",
        "--quiet",
        "--report",
        str(work / "report.json"),
    ]
    if runner is None:
        python_version = runtime.core_engines.python
        args.extend(
            [
                "--platform",
                runtime.pip_platform,
                "--python-version",
                python_version.replace(".", ""),
            ],
        )
    return args


def _container_settings(
    container: PipInContainerConfig | None,
) -> tuple[PipRunner | None, str, str]:
    if container is None:
        return None, _PIP_WORK_MOUNT, _GLUEWHEELS_STAGING_MOUNT
    return (
        container.runner,
        container.pip_work_mount,
        container.gluewheels_staging_mount,
    )


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


@dataclass(frozen=True, slots=True)
class PipInContainerConfig:
    """Paths and runner for pip inside the Glue Docker image."""

    runner: PipRunner
    pip_work_mount: str = _PIP_WORK_MOUNT
    gluewheels_staging_mount: str = _GLUEWHEELS_STAGING_MOUNT


def resolve_packages(
    prepared: PreparedRequirements,
    runtime: GlueRuntimeMetadata,
    *,
    container: PipInContainerConfig | None = None,
) -> dict[str, str]:
    """Run ``pip install --dry-run --report`` and return resolved packages.

    Args:
        prepared: Rewritten requirements and extra Docker mounts.
        runtime: Bundled Glue runtime metadata (pins, Python, platform).
        container: When set, invoke pip via Docker using these mount paths.

    Returns:
        Resolved packages (``name`` → ``version``).

    Raises:
        PipError: ``pip install --dry-run`` exited with an error.
        ValidationError: Installation report JSON did not match the expected
            shape.

    """
    runner, pip_work_mount, _ = _container_settings(container)
    with _pip_workspace(
        prepared.rewritten_specs,
        runtime.python_packages,
    ) as work:
        volume_mounts = _volume_mounts_for(work, pip_work_mount, prepared)
        _pip_run(
            *_dry_run_args(work, runtime, runner=runner),
            runner=runner,
            volume_mounts=volume_mounts,
        )
        report_text = (work / "report.json").read_text(encoding="utf-8")

    parsed = _InstallationReport.model_validate(loads(report_text))
    return {
        item.metadata.name: item.metadata.version for item in parsed.install
    }


def _filter_bundled_wheels(
    wheels_dir: Path,
    runtime_pins: Mapping[str, str],
) -> dict[str, str]:
    """Drop image-pinned wheels and return pins for the remainder."""
    kept: dict[str, str] = {}
    for wheel_path in wheels_dir.glob("*.whl"):
        name, version, _, _ = parse_wheel_filename(wheel_path.name)
        version_text = str(version)
        if runtime_pins.get(name) == version_text:
            wheel_path.unlink()
            continue
        kept[name] = version_text
    return kept


def bundle_wheels(
    prepared: PreparedRequirements,
    runtime: GlueRuntimeMetadata,
    dest: Path,
    *,
    container: PipInContainerConfig | None = None,
) -> dict[str, str]:
    """Build wheels for requirements and omit Glue image pins.

    Runs ``pip wheel`` with prepared ``requirements.in`` and Glue
    ``constraints.txt``, then deletes wheels whose ``name==version`` matches
    bundled image pins.

    Args:
        prepared: Rewritten requirements and extra Docker mounts.
        runtime: Bundled Glue runtime metadata (pins, Python, platform).
        dest: Directory to write ``.whl`` files into (created if missing).
        container: When set, invoke pip via Docker using these mount paths.

    Returns:
        Package pins (``name`` → ``version``) for wheels kept in ``dest``.

    Raises:
        PipError: ``pip wheel`` exited with an error.

    """
    runner, pip_work_mount, gluewheels_staging_mount = _container_settings(
        container,
    )
    dest.mkdir(parents=True, exist_ok=True)
    with _pip_workspace(
        prepared.rewritten_specs,
        runtime.python_packages,
    ) as work:
        volume_mounts = _volume_mounts_for(
            work,
            pip_work_mount,
            prepared,
            staging_parent=dest.parent,
            gluewheels_staging_mount=gluewheels_staging_mount,
        )
        _pip_run(
            "wheel",
            "--requirement",
            str(work / "requirements.in"),
            "--constraint",
            str(work / "constraints.txt"),
            "--wheel-dir",
            str(dest),
            "--no-cache-dir",
            runner=runner,
            volume_mounts=volume_mounts,
        )
    return _filter_bundled_wheels(dest, runtime.python_packages)
