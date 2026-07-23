"""Container mount paths and host-to-container path mapping.

Single source for workspace and temporary mount constants used by
:mod:`aws_glue_toolkit.dependencies` and :mod:`aws_glue_toolkit.docker`.

:data:`RUN_WRAPPER_MOUNT` is the container path of the ``gtk run`` entry
script (:mod:`aws_glue_toolkit.run_wrapper`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "EXT_MOUNT_PREFIX",
    "GLUEWHEELS_STAGING_MOUNT",
    "PIP_WORK_MOUNT",
    "PYTHON_TARGET_MOUNT",
    "RUN_WRAPPER_MOUNT",
    "WORKSPACE_MOUNT",
    "map_host_to_container",
    "project_path",
    "rewrite_path_arg",
]

WORKSPACE_MOUNT = "/home/hadoop/workspace"
PIP_WORK_MOUNT = "/tmp/gtk-pip-work"  # noqa: S108  # nosec B108
GLUEWHEELS_STAGING_MOUNT = "/tmp/gtk-staging"  # noqa: S108  # nosec B108
PYTHON_TARGET_MOUNT = "/tmp/gtk-python"  # noqa: S108  # nosec B108
RUN_WRAPPER_MOUNT = "/tmp/gtk-run-wrapper.py"  # noqa: S108  # nosec B108
EXT_MOUNT_PREFIX = "/tmp/gtk-ext"  # noqa: S108  # nosec B108


def project_path(project_dir: Path, path: Path) -> str:
    """Return the container path for ``path`` under the workspace mount.

    Args:
        project_dir: Job root directory mounted at :data:`WORKSPACE_MOUNT`.
        path: Absolute or project-relative path under ``project_dir``.

    Returns:
        Container path under ``WORKSPACE_MOUNT``.

    Raises:
        ValueError: ``path`` is not under ``project_dir``.

    """
    rel = path.relative_to(project_dir)
    return f"{WORKSPACE_MOUNT}/{rel.as_posix()}"


def map_host_to_container(
    path: Path,
    host_mount: Path,
    container_mount: str,
) -> str | None:
    """Map ``path`` under ``host_mount`` to a container path, if applicable.

    Args:
        path: Host path to map (resolved before comparison).
        host_mount: Host directory bound to ``container_mount``.
        container_mount: Container mount point for ``host_mount``.

    Returns:
        Mapped container path, or ``None`` when ``path`` is outside
        ``host_mount``.

    """
    try:
        rel = path.relative_to(host_mount.resolve())
    except ValueError:
        return None
    suffix = rel.as_posix()
    if suffix and suffix != ".":
        return f"{container_mount}/{suffix}"
    return container_mount


def rewrite_path_arg(
    arg: str,
    volume_mounts: Sequence[tuple[Path, str]],
) -> str:
    """Map a host path argument to its container mount path, if applicable.

    Args:
        arg: CLI argument that may be a host filesystem path.
        volume_mounts: ``(host_path, container_mount)`` pairs for the run.

    Returns:
        Mapped container path when ``arg`` resolves under a mount; otherwise
        ``arg`` unchanged.

    """
    try:
        resolved = Path(arg).resolve()
    except (OSError, ValueError):
        return arg

    # Prefer the longest matching host mount prefix.
    mounts = sorted(
        volume_mounts,
        key=lambda item: len(str(item[0].resolve())),
        reverse=True,
    )
    for host_mount, container_mount in mounts:
        mapped = map_host_to_container(
            resolved,
            host_mount,
            container_mount,
        )
        if mapped is not None:
            return mapped
    return arg
