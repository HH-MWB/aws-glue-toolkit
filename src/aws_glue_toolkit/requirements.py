"""Prepare PEP 508 dependency specs for pip inside the Glue Docker image.

Rewrites ``file:`` direct references to container paths and collects extra
volume mounts for local paths outside the job directory.

Public API: :class:`PreparedRequirements`, :func:`prepare_requirements`,
:exc:`RequirementPreparationError`.

"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from packaging.requirements import Requirement

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "PreparedRequirements",
    "RequirementPreparationError",
    "prepare_requirements",
]

# Must match :data:`aws_glue_toolkit.docker` workspace mount.
_CONTAINER_WORKSPACE = "/home/hadoop/workspace"
_EXT_MOUNT_PREFIX = "/tmp/gtk-ext"  # noqa: S108  # nosec B108


class RequirementPreparationError(ValueError):
    """A dependency spec could not be prepared for container pip."""


@dataclass(frozen=True, slots=True)
class PreparedRequirements:
    """Dependency specs rewritten for pip inside the Glue container."""

    rewritten_specs: tuple[str, ...]
    extra_mounts: tuple[tuple[Path, str], ...]


def _rewrite_file_spec(
    spec: str,
    project_dir: Path,
    mount_by_host: dict[Path, str],
) -> str:
    req = Requirement(spec)
    if req.url is None or not req.url.startswith("file:"):
        return spec
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


def prepare_requirements(
    specs: Sequence[str],
    project_dir: Path,
) -> PreparedRequirements:
    """Rewrite ``file:`` deps to container paths and collect extra mounts.

    Args:
        specs: Direct dependency requirements (PEP 508 strings).
        project_dir: Job directory containing ``pyproject.toml``.

    Returns:
        Specs safe for ``requirements.in`` inside Docker, plus host mounts
        for paths outside ``project_dir``.

    Raises:
        RequirementPreparationError: Spec is editable, path is missing, or
            a ``file:`` URL could not be resolved.

    """
    resolved_project = project_dir.resolve()
    mount_by_host: dict[Path, str] = {}
    rewritten: list[str] = []

    for spec in specs:
        stripped = spec.strip()
        if stripped.startswith(("-e ", "--editable")):
            msg = "editable installs are not supported for Glue deployment"
            raise RequirementPreparationError(msg)
        rewritten.append(
            _rewrite_file_spec(stripped, resolved_project, mount_by_host),
        )

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


def _resolve_file_url(url: str, project_dir: Path) -> Path:
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
    resolved = host_path.resolve()
    try:
        relative = resolved.relative_to(project_dir)
        return f"file://{_CONTAINER_WORKSPACE}/{relative.as_posix()}"
    except ValueError:
        mount_root, target = _mount_target(resolved)
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
    resolved = mount_root.resolve()
    if resolved in mount_by_host:
        return mount_by_host[resolved]
    index = len(mount_by_host)
    container_path = f"{_EXT_MOUNT_PREFIX}/{index}"
    mount_by_host[resolved] = container_path
    return container_path
