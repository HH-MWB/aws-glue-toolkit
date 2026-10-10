"""Prepare Glue job dependency specs, pin policy, and pip workspaces.

Rewrites ``file:`` PEP 508 specs for container or host paths and stages
requirements for pip. Pass a :class:`PipRunner` from
:mod:`aws_glue_toolkit.container` to execute pip in workflows.

Public API: :class:`DependencySession`, :func:`open_dependency_session`,
:class:`GlueImagePinPolicy`, :func:`glue_image_pin_policy`,
:class:`PreparedRequirements`, :func:`prepare_requirements`,
:func:`staged_requirements`, :func:`pip_install_target_args`,
:class:`PipRunner`, :exc:`PipError`, :exc:`RequirementPreparationError`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from aws_glue_toolkit.mounts import (
    EXT_MOUNT_PREFIX,
    GLUEWHEELS_STAGING_MOUNT,
    PIP_WORK_MOUNT,
    PYTHON_TARGET_MOUNT,
    WORKSPACE_MOUNT,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_glue_toolkit.job import GlueJobProject
    from aws_glue_toolkit.runtime import GlueRuntimeMetadata

PipRunner = Callable[[Sequence[str], Sequence[tuple[Path, str]]], None]

__all__ = [
    "DependencySession",
    "GlueImagePinPolicy",
    "PipError",
    "PipRunner",
    "PreparedRequirements",
    "RequirementPreparationError",
    "glue_image_pin_policy",
    "open_dependency_session",
    "pip_error_from_returncode",
    "pip_install_target_args",
    "prepare_requirements",
    "staged_requirements",
]


# --- Exceptions ---


class RequirementPreparationError(ValueError):
    """A dependency spec could not be prepared for host or container pip."""


class PipError(Exception):
    """``pip`` failed during resolution, install, or wheel bundling."""

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
    """Dependency specs rewritten for container or host pip."""

    rewritten_specs: tuple[str, ...]
    extra_mounts: tuple[tuple[Path, str], ...]


def prepare_requirements(
    specs: Sequence[str],
    project_dir: Path,
    *,
    for_container: bool = True,
) -> PreparedRequirements:
    """Prepare dependency specs for pip (container or host).

    Args:
        specs: PEP 508 requirement strings (merged job dependencies).
        project_dir: Job root directory. When ``for_container``, mounted at
            :data:`~aws_glue_toolkit.mounts.WORKSPACE_MOUNT`.
        for_container: When true (default), rewrite ``file:`` URLs to
            container paths and collect extra mounts. When false, rewrite
            to absolute host ``file://`` URLs with no mounts.

    Returns:
        Rewritten specs and, when ``for_container``, extra host→container
        mounts for paths outside the workspace.

    Raises:
        RequirementPreparationError: Editable installs or missing ``file:``
            paths.

    """
    resolved_project = project_dir.resolve()
    mount_by_host: dict[Path, str] = {}
    rewritten = tuple(
        _prepare_one_spec(
            spec.strip(),
            resolved_project,
            mount_by_host,
            for_container=for_container,
        )
        for spec in specs
    )
    # Stable mount order for reproducible docker ``-v`` flags.
    extra_mounts = tuple(
        (host_path, container_path)
        for host_path, container_path in sorted(
            mount_by_host.items(),
            key=lambda item: item[1],
        )
    )
    return PreparedRequirements(
        rewritten_specs=rewritten,
        extra_mounts=extra_mounts,
    )


def _prepare_one_spec(
    spec: str,
    project_dir: Path,
    mount_by_host: dict[Path, str],
    *,
    for_container: bool,
) -> str:
    """Reject editables and rewrite one dependency spec."""
    if spec.startswith(("-e ", "--editable")):
        msg = "editable installs are not supported for Glue deployment"
        raise RequirementPreparationError(msg)
    if for_container:
        return _rewrite_file_spec_for_container(
            spec,
            project_dir,
            mount_by_host,
        )
    return _rewrite_file_spec_for_host(spec, project_dir)


def _existing_file_dep_path(
    spec: str,
    project_dir: Path,
) -> tuple[Path, str] | None:
    """Return ``(host_path, file_url)`` for a ``file:`` dep, else None.

    Raises:
        RequirementPreparationError: The ``file:`` path does not exist.

    """
    req = Requirement(spec)
    if req.url is None or not req.url.startswith("file:"):
        return None
    host_path = _resolve_file_url(req.url, project_dir)
    if not host_path.exists():
        msg = f"dependency path does not exist: {host_path}"
        raise RequirementPreparationError(msg)
    return host_path, req.url


def _rewrite_file_spec_for_container(
    spec: str,
    project_dir: Path,
    mount_by_host: dict[Path, str],
) -> str:
    """Rewrite one ``file:`` PEP 508 spec to a container ``file:`` URL."""
    resolved = _existing_file_dep_path(spec, project_dir)
    if resolved is None:
        return spec
    host_path, file_url = resolved
    container_url = _container_file_url(
        host_path,
        project_dir,
        mount_by_host,
    )
    return spec.replace(file_url, container_url, 1)


def _rewrite_file_spec_for_host(spec: str, project_dir: Path) -> str:
    """Rewrite one ``file:`` PEP 508 spec to an absolute host ``file:`` URL."""
    resolved = _existing_file_dep_path(spec, project_dir)
    if resolved is None:
        return spec
    host_path, file_url = resolved
    return spec.replace(file_url, host_path.resolve().as_uri(), 1)


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


# --- Glue image pin policy ---


@dataclass(frozen=True, slots=True)
class GlueImagePinPolicy:
    """Pip constraints and gluewheels omit rules for a job resolve."""

    runtime: GlueRuntimeMetadata
    constraint_pins: Mapping[str, str]
    omit_pins: Mapping[str, str]


def _direct_dependency_names(specs: Sequence[str]) -> frozenset[str]:
    """Return canonical names from direct PEP 508 requirement strings."""
    names: set[str] = set()
    for spec in specs:
        stripped = spec.strip()
        if not stripped:
            continue
        try:
            names.add(canonicalize_name(Requirement(stripped).name))
        except InvalidRequirement as err:
            msg = f"invalid dependency spec: {stripped!r}"
            raise RequirementPreparationError(msg) from err
    return frozenset(names)


def glue_image_pin_policy(
    runtime: GlueRuntimeMetadata,
    direct_specs: Sequence[str],
) -> GlueImagePinPolicy:
    """Build constraint and omit pins for build, run, and test.

    Direct dependencies that name a Glue image package are excluded from
    pip ``--constraint`` so the job spec wins; omit still uses full image
    pins (wheels matching the image version are dropped from gluewheels).

    Args:
        runtime: Glue runtime metadata (``python_packages`` catalog).
        direct_specs: Merged ``[project].dependencies`` and tool-section
            deps (PEP 508 strings).

    Returns:
        :class:`GlueImagePinPolicy` for pip workspaces and wheel omit.

    """
    image_pins = runtime.python_packages
    image_by_canonical = {canonicalize_name(name): name for name in image_pins}
    direct_names = _direct_dependency_names(direct_specs)
    overridden = frozenset(
        canonical
        for canonical in direct_names
        if canonical in image_by_canonical
    )
    constraint_pins = {
        name: version
        for name, version in image_pins.items()
        if canonicalize_name(name) not in overridden
    }
    return GlueImagePinPolicy(
        runtime=runtime,
        constraint_pins=constraint_pins,
        omit_pins=image_pins,
    )


@dataclass(frozen=True, slots=True)
class DependencySession:
    """Prepared requirements, Glue pin policy, and pip runner for one step."""

    prepared: PreparedRequirements
    policy: GlueImagePinPolicy
    runner: PipRunner


def open_dependency_session(
    job: GlueJobProject,
    *,
    in_container: bool,
    runner: PipRunner,
) -> DependencySession:
    """Prepare requirements, pin policy, and attach a host or container runner.

    Args:
        job: Resolved job from :func:`~aws_glue_toolkit.job.load_pyproject`.
        in_container: When true, rewrite ``file:`` specs for container pip.
        runner: Callable that runs pip (from
            :mod:`aws_glue_toolkit.container`).

    Returns:
        Everything needed for one ``bundle_wheels`` or staged install step.

    """
    policy = glue_image_pin_policy(job.runtime, job.dependencies)
    prepared = prepare_requirements(
        job.dependencies,
        job.project_dir,
        for_container=in_container,
    )
    return DependencySession(prepared, policy, runner)


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


@contextmanager
def staged_requirements(
    prepared: PreparedRequirements,
    policy: GlueImagePinPolicy,
) -> Iterator[tuple[Path, Sequence[tuple[Path, str]]]]:
    """Stage requirements files and yield pip work dir plus volume mounts.

    Args:
        prepared: Dependency specs rewritten for container pip.
        policy: Image pin policy (``constraint_pins`` for ``constraints.txt``).

    Yields:
        ``(work, mounts)`` where ``work`` holds ``requirements.in`` and
        ``constraints.txt``, and ``mounts`` are docker ``-v`` pairs for
        the pip work dir and any external ``file:`` dependency paths.

    """
    with _pip_workspace(
        prepared.rewritten_specs,
        policy.constraint_pins,
    ) as work:
        yield work, _volume_mounts_for(work, prepared)


def pip_install_target_args() -> list[str]:
    """Return ``pip install --target`` argv using container mount paths."""
    return [
        "install",
        "--target",
        PYTHON_TARGET_MOUNT,
        "--requirement",
        f"{PIP_WORK_MOUNT}/requirements.in",
        "--constraint",
        f"{PIP_WORK_MOUNT}/constraints.txt",
        "--quiet",
    ]
