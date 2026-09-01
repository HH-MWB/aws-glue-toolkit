"""Prepare and resolve Glue job dependencies via pip.

Rewrites ``file:`` PEP 508 specs for container or host paths, runs
``pip install --dry-run --report``, ``pip wheel``, and ``pip download``,
and filters wheels already pinned on the Glue image. Pass a
:class:`PipRunner` from :mod:`aws_glue_toolkit.docker` to execute pip.

Public API: :class:`PreparedRequirements`, :func:`prepare_requirements`,
:func:`staged_requirements`, :func:`pip_install_target_args`,
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
from packaging.utils import canonicalize_name, parse_wheel_filename

from aws_glue_toolkit.paths import (
    EXT_MOUNT_PREFIX,
    GLUEWHEELS_STAGING_MOUNT,
    PIP_WORK_MOUNT,
    PYTHON_TARGET_MOUNT,
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
    "pip_install_target_args",
    "prepare_requirements",
    "resolve_packages",
    "staged_requirements",
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
        specs: PEP 508 requirement strings from ``project.dependencies``.
        project_dir: Job root directory. When ``for_container``, mounted at
            :data:`~aws_glue_toolkit.paths.WORKSPACE_MOUNT`.
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
    runtime: GlueRuntimeMetadata,
) -> Iterator[tuple[Path, Sequence[tuple[Path, str]]]]:
    """Stage requirements files and yield pip work dir plus volume mounts.

    Args:
        prepared: Dependency specs rewritten for container pip.
        runtime: Glue runtime metadata (image pins used as constraints).

    Yields:
        ``(work, mounts)`` where ``work`` holds ``requirements.in`` and
        ``constraints.txt``, and ``mounts`` are docker ``-v`` pairs for
        the pip work dir and any external ``file:`` dependency paths.

    """
    with _pip_workspace(
        prepared.rewritten_specs,
        runtime.python_packages,
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
        return _dry_run_install_report(work, prepared, runner=runner)


def _dry_run_install_report(
    work: Path,
    prepared: PreparedRequirements,
    *,
    runner: PipRunner,
) -> dict[str, str]:
    """Run ``pip install --dry-run --report``; return name → version."""
    report_path = work / "report.json"
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
            str(report_path),
        ],
        _volume_mounts_for(work, prepared),
    )
    data = loads(report_path.read_text(encoding="utf-8"))
    return {
        item["metadata"]["name"]: item["metadata"]["version"]
        for item in data["install"]
    }


def _assert_wheels_portable(dest: Path, pip_platform: str) -> None:
    """Raise :exc:`PipError` if any wheel in ``dest`` is not portable."""
    for wheel_path in sorted(dest.glob("*.whl")):
        _, _, _, tags = parse_wheel_filename(wheel_path.name)
        platforms = frozenset(tag.platform for tag in tags)
        if "any" in platforms or pip_platform in platforms:
            continue
        msg = (
            f"wheel {wheel_path.name} has platform tag(s) "
            f"{', '.join(sorted(platforms))}; "
            f"expected 'any' or {pip_platform!r} for --mode host"
        )
        raise PipError(msg)


def _wheel_direct_url_deps(
    work: Path,
    dest: Path,
    specs: Sequence[str],
    *,
    runner: PipRunner,
    mounts: Sequence[tuple[Path, str]],
) -> None:
    """Wheel every path/VCS dep in ``specs`` into ``dest`` (no-op if none)."""
    url_specs = tuple(
        spec for spec in specs if Requirement(spec).url is not None
    )
    if not url_specs:
        return
    direct_in = work / "direct.in"
    direct_in.write_text("\n".join(url_specs), encoding="utf-8")
    runner(
        [
            "wheel",
            "--no-deps",
            "--requirement",
            str(direct_in),
            "--constraint",
            str(work / "constraints.txt"),
            "--wheel-dir",
            str(dest),
            "--no-cache-dir",
        ],
        mounts,
    )


def _pins_missing_from_dest(
    report: Mapping[str, str],
    dest: Path,
) -> list[str]:
    """Return report pins that have no matching wheel in ``dest``."""
    already = frozenset(
        canonicalize_name(parse_wheel_filename(path.name)[0])
        for path in dest.glob("*.whl")
    )
    return [
        f"{name}=={version}"
        for name, version in sorted(report.items())
        if canonicalize_name(name) not in already
    ]


def _wheel_pin_from_sdist(
    work: Path,
    dest: Path,
    pin_in: Path,
    *,
    runner: PipRunner,
) -> None:
    """Download one pin without platform tags and wheel any sdist."""
    runner(
        [
            "download",
            "--no-deps",
            "--requirement",
            str(pin_in),
            "--constraint",
            str(work / "constraints.txt"),
            "--dest",
            str(dest),
            "--no-cache-dir",
        ],
        (),
    )
    for sdist in sorted(dest.glob("*.tar.gz")):
        runner(
            [
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(dest),
                "--no-cache-dir",
                str(sdist),
            ],
            (),
        )
        sdist.unlink()


def _ensure_pin_wheel(
    work: Path,
    dest: Path,
    pin: str,
    runtime: GlueRuntimeMetadata,
    *,
    runner: PipRunner,
) -> None:
    """Ensure one pin has a wheel in ``dest`` (binary, else sdist→wheel)."""
    pin_in = work / "pin.in"
    pin_in.write_text(pin + "\n", encoding="utf-8")
    try:
        runner(
            [
                "download",
                "--no-deps",
                "--requirement",
                str(pin_in),
                "--constraint",
                str(work / "constraints.txt"),
                "--dest",
                str(dest),
                "--platform",
                runtime.pip_platform,
                "--python-version",
                "".join(runtime.core_engines.python.split(".")),
                "--only-binary=:all:",
                "--no-cache-dir",
            ],
            (),
        )
    except PipError as err:
        detail = f"{err.stderr or ''}{err.stdout or ''}{err}".lower()
        if "no matching distribution found" not in detail:
            raise
        _wheel_pin_from_sdist(work, dest, pin_in, runner=runner)


def _bundle_cross_platform(
    prepared: PreparedRequirements,
    runtime: GlueRuntimeMetadata,
    dest: Path,
    *,
    runner: PipRunner,
) -> None:
    """Wheel path/VCS deps, then ensure a portable wheel per resolved pin."""
    with _pip_workspace(
        prepared.rewritten_specs,
        runtime.python_packages,
    ) as work:
        mounts = _volume_mounts_for(
            work,
            prepared,
            staging_parent=dest.parent,
        )
        _wheel_direct_url_deps(
            work,
            dest,
            prepared.rewritten_specs,
            runner=runner,
            mounts=mounts,
        )
        report = _dry_run_install_report(work, prepared, runner=runner)
        for pin in _pins_missing_from_dest(report, dest):
            _ensure_pin_wheel(
                work,
                dest,
                pin,
                runtime,
                runner=runner,
            )
        _assert_wheels_portable(dest, runtime.pip_platform)


def _bundle_in_container(
    prepared: PreparedRequirements,
    runtime: GlueRuntimeMetadata,
    dest: Path,
    *,
    runner: PipRunner,
) -> None:
    """Build all wheels with ``pip wheel`` in the Glue container."""
    with _pip_workspace(
        prepared.rewritten_specs,
        runtime.python_packages,
    ) as work:
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


def _omit_pinned_wheels(
    dest: Path,
    pins: Mapping[str, str],
) -> dict[str, str]:
    """Delete Glue-pinned wheels in ``dest``; return kept name→version."""
    kept: dict[str, str] = {}
    for wheel_path in dest.glob("*.whl"):
        name, version, _, _ = parse_wheel_filename(wheel_path.name)
        if pins.get(name) == str(version):
            wheel_path.unlink()
        else:
            kept[name] = str(version)
    return kept


def bundle_wheels(
    prepared: PreparedRequirements,
    runtime: GlueRuntimeMetadata,
    dest: Path,
    *,
    runner: PipRunner,
    cross_platform: bool = False,
) -> dict[str, str]:
    """Build or download wheels for requirements and omit Glue image pins.

    Args:
        prepared: Dependency specs rewritten for container or host pip.
        runtime: Glue runtime metadata (constraints; ``pip_platform`` /
            Python version when ``cross_platform``).
        dest: Existing wheel output directory from
            :func:`~aws_glue_toolkit.artifacts.stage_gluewheels_zip`.
        runner: Callable that runs pip (container or host).
        cross_platform: When true, path/VCS via ``pip wheel --no-deps``,
            then per pin ``pip download --platform`` or sdist→wheel.
            When false, ``pip wheel`` only.

    Returns:
        Package name → version for wheels kept in ``dest`` (image pins
        removed).

    Raises:
        PipError: ``pip`` failed, or a wheel is not portable for Glue.

    """
    if cross_platform:
        _bundle_cross_platform(prepared, runtime, dest, runner=runner)
    else:
        _bundle_in_container(prepared, runtime, dest, runner=runner)
    return _omit_pinned_wheels(dest, runtime.python_packages)
