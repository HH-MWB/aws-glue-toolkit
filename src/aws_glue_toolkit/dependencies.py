"""Prepare and resolve Glue job dependencies via pip in Docker.

Rewrites ``file:`` PEP 508 specs for container paths, runs ``pip install
--dry-run --report`` and ``pip wheel``, and filters wheels already pinned
on the Glue image. Pass a :class:`PipRunner` from
:mod:`aws_glue_toolkit.docker` to execute pip inside the official Glue
local Docker image.

Public API: :class:`PreparedRequirements`, :func:`prepare_requirements`,
:func:`resolve_packages`, :func:`bundle_wheels`, :class:`PipRunner`,
:exc:`PipError`, :exc:`RequirementPreparationError`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from packaging.requirements import Requirement
from packaging.utils import parse_wheel_filename

from aws_glue_toolkit.paths import (
    EXT_MOUNT_PREFIX,
    GLUEWHEELS_STAGING_MOUNT,
    PIP_WORK_MOUNT,
    WORKSPACE_MOUNT,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_glue_toolkit.runtime import GlueRuntimeMetadata

PipRunner = Callable[[Sequence[str], Sequence[tuple[Path, str]]], None]

__all__ = [
    "PipError",
    "PipRunner",
    "PreparedRequirements",
    "RequirementPreparationError",
    "bundle_wheels",
    "pip_error_from_returncode",
    "prepare_requirements",
    "resolve_packages",
]


# --- Exceptions ---


class RequirementPreparationError(ValueError):
    """A dependency spec could not be prepared for container pip."""


class PipError(Exception):
    """``pip`` subprocess failed during resolution or wheel bundling."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        """Initialize from a message and optional subprocess fields.

        Args:
            message: Human-readable failure summary.
            returncode: Subprocess exit code, when available.
            stdout: Captured stdout from the pip invocation.
            stderr: Captured stderr from the pip invocation.

        """
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
    """Build :exc:`PipError` from a non-zero subprocess result.

    Args:
        returncode: Subprocess exit code.
        stdout: Captured stdout from the pip invocation.
        stderr: Captured stderr from the pip invocation.

    Returns:
        :exc:`PipError` with the best available message text.

    """
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


# --- Requirement preparation ---


@dataclass(frozen=True, slots=True)
class PreparedRequirements:
    """Dependency specs rewritten for pip inside the Glue container."""

    rewritten_specs: tuple[str, ...]
    extra_mounts: tuple[tuple[Path, str], ...]


def prepare_requirements(
    specs: Sequence[str],
    project_dir: Path,
) -> PreparedRequirements:
    """Rewrite ``file:`` deps to container paths and collect extra mounts.

    Args:
        specs: PEP 508 requirement strings from ``project.dependencies``.
        project_dir: Job root directory mounted at
            :data:`~aws_glue_toolkit.paths.WORKSPACE_MOUNT`.

    Returns:
        Rewritten specs and extra host→container mounts for paths outside
        the workspace.

    Raises:
        RequirementPreparationError: Editable installs or missing ``file:``
            paths.

    """
    resolved_project = project_dir.resolve()
    mount_by_host: dict[Path, str] = {}
    rewritten: list[str] = []

    # Rewrite each spec; reject editable installs.
    for spec in specs:
        stripped = spec.strip()
        if stripped.startswith(("-e ", "--editable")):
            msg = "editable installs are not supported for Glue deployment"
            raise RequirementPreparationError(msg)
        rewritten.append(
            _rewrite_file_spec(stripped, resolved_project, mount_by_host),
        )

    # Stable mount order for reproducible docker -v flags.
    extra_mounts = tuple(
        (host_path, container_path)
        for host_path, container_path in sorted(
            mount_by_host.items(),
            key=lambda item: item[1],
        )
    )
    return PreparedRequirements(
        rewritten_specs=tuple(rewritten),
        extra_mounts=extra_mounts,
    )


def _rewrite_file_spec(
    spec: str,
    project_dir: Path,
    mount_by_host: dict[Path, str],
) -> str:
    """Rewrite one ``file:`` PEP 508 spec to a container ``file:`` URL."""
    req = Requirement(spec)
    if req.url is None or not req.url.startswith("file:"):
        return spec

    # Resolve host path and fail fast when the target is missing.
    host_path = _resolve_file_url(req.url, project_dir)
    if not host_path.exists():
        msg = f"dependency path does not exist: {host_path}"
        raise RequirementPreparationError(msg)

    container_url = _container_file_url(
        host_path,
        project_dir,
        mount_by_host,
    )
    return spec.replace(req.url, container_url, 1)


def _resolve_file_url(url: str, project_dir: Path) -> Path:
    """Resolve a ``file:`` URL to an absolute host path."""
    if url.startswith("file://"):
        raw = unquote(urlparse(url).path)
        return Path(raw).resolve()
    relative = url.removeprefix("file:")
    return (project_dir / relative).resolve()


def _container_file_url(
    host_path: Path,
    project_dir: Path,
    mount_by_host: dict[Path, str],
) -> str:
    """Build a container ``file:`` URL for ``host_path``."""
    # Paths under the job workspace use WORKSPACE_MOUNT.
    try:
        relative = host_path.relative_to(project_dir)
        return f"file://{WORKSPACE_MOUNT}/{relative.as_posix()}"
    except ValueError:
        pass

    # External paths get a dedicated EXT_MOUNT_PREFIX bind mount.
    mount_root, target = _mount_target(host_path)
    container_path = _mount_point_for(mount_root, mount_by_host)
    in_mount = target.relative_to(mount_root)
    suffix = in_mount.as_posix()
    if suffix == ".":
        return f"file://{container_path}"
    return f"file://{container_path}/{suffix}"


def _mount_target(path: Path) -> tuple[Path, Path]:
    """Return mount root and target path for a ``file:`` dependency."""
    if path.is_file():
        return path.parent.resolve(), path.resolve()
    return path.resolve(), path.resolve()


def _mount_point_for(
    mount_root: Path,
    mount_by_host: dict[Path, str],
) -> str:
    """Return a stable container mount path for ``mount_root``."""
    if mount_root in mount_by_host:
        return mount_by_host[mount_root]
    index = len(mount_by_host)
    container_path = f"{EXT_MOUNT_PREFIX}/{index}"
    mount_by_host[mount_root] = container_path
    return container_path


# --- pip workspace ---


@contextmanager
def _pip_workspace(
    requirement_specs: Sequence[str],
    runtime_package_pins: Mapping[str, str],
) -> Iterator[Path]:
    """Provide a temporary directory prepared for ``pip``."""
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        work.chmod(0o777)

        # Input files consumed by pip inside the container.
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


def _volume_mounts_for(
    work: Path,
    prepared: PreparedRequirements,
    *,
    staging_parent: Path | None = None,
) -> list[tuple[Path, str]]:
    """Build docker ``-v`` pairs for pip work, staging, and extra deps."""
    mounts: list[tuple[Path, str]] = [(work, PIP_WORK_MOUNT)]
    if staging_parent is not None:
        mounts.append((staging_parent, GLUEWHEELS_STAGING_MOUNT))
    mounts.extend(prepared.extra_mounts)
    return mounts


# --- Public pip API ---


def resolve_packages(
    prepared: PreparedRequirements,
    runtime: GlueRuntimeMetadata,
    *,
    runner: PipRunner,
) -> dict[str, str]:
    """Run ``pip install --dry-run --report`` and return resolved packages.

    Args:
        prepared: Dependency specs rewritten for container pip.
        runtime: Glue runtime metadata (image pins used as constraints).
        runner: Callable that runs pip inside the Glue Docker image.

    Returns:
        Resolved package name → version for the dry-run install plan.

    Raises:
        PipError: ``pip`` exited non-zero during the dry run.

    """
    with _pip_workspace(
        prepared.rewritten_specs,
        runtime.python_packages,
    ) as work:
        runner(
            [
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
            ],
            _volume_mounts_for(work, prepared),
        )

        # Parse pip's JSON install report.
        data = loads((work / "report.json").read_text(encoding="utf-8"))
        return {
            item["metadata"]["name"]: item["metadata"]["version"]
            for item in data["install"]
        }


def bundle_wheels(
    prepared: PreparedRequirements,
    runtime: GlueRuntimeMetadata,
    dest: Path,
    *,
    runner: PipRunner,
) -> dict[str, str]:
    """Build wheels for requirements and omit Glue image pins.

    Args:
        prepared: Dependency specs rewritten for container pip.
        runtime: Glue runtime metadata (image pins used as constraints).
        dest: Existing wheel output directory. ``gtk build`` passes the
            ``wheels/`` directory created by
            :func:`~aws_glue_toolkit.artifacts.stage_gluewheels_zip`
            before yield.
        runner: Callable that runs pip inside the Glue Docker image.

    Returns:
        Package name → version for wheels kept in ``dest`` (image pins
        removed).

    Raises:
        PipError: ``pip wheel`` exited non-zero.

    """
    pins = runtime.python_packages
    with _pip_workspace(prepared.rewritten_specs, pins) as work:
        runner(
            [
                "wheel",
                "--requirement",
                str(work / "requirements.in"),
                "--constraint",
                str(work / "constraints.txt"),
                "--wheel-dir",
                str(dest),
                "--no-cache-dir",
            ],
            _volume_mounts_for(
                work,
                prepared,
                staging_parent=dest.parent,
            ),
        )

    # Drop wheels that match preinstalled Glue image pins.
    kept: dict[str, str] = {}
    for wheel_path in dest.glob("*.whl"):
        name, version, _, _ = parse_wheel_filename(wheel_path.name)
        if pins.get(name) == str(version):
            wheel_path.unlink()
        else:
            kept[name] = str(version)
    return kept
