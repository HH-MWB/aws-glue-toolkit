"""Run Glue jobs, tests, and pip in the official AWS Glue local Docker image.

Pure builders produce ``docker run``, ``spark-submit``, ``pytest``, and pip
argv lists; :func:`run_container` and :func:`run_container_capture` are the
subprocess shells.

Public API: :func:`build_run_argv`, :func:`build_spark_submit_argv`,
:func:`build_pytest_argv`, :func:`build_pip_argv`, :func:`run_container`,
:func:`run_container_capture`, :func:`run_pip_in_container`,
:data:`PIP_WORK_MOUNT`, :data:`GLUEWHEELS_STAGING_MOUNT`, :exc:`DockerError`.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`run_container` and :func:`run_container_capture`.

"""

from __future__ import annotations

from pathlib import Path
from shlex import join as shlex_join
from shlex import quote as shlex_quote
from shutil import which
from subprocess import CompletedProcess, run  # nosec B404
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "GLUEWHEELS_STAGING_MOUNT",
    "PIP_WORK_MOUNT",
    "DockerError",
    "build_pip_argv",
    "build_pytest_argv",
    "build_run_argv",
    "build_spark_submit_argv",
    "run_container",
    "run_container_capture",
    "run_pip_in_container",
]

_WORKSPACE_MOUNT = "/home/hadoop/workspace"
_DOCKER_PLATFORM = "linux/amd64"
PIP_WORK_MOUNT = "/tmp/gtk-pip-work"  # noqa: S108  # nosec B108
GLUEWHEELS_STAGING_MOUNT = "/tmp/gtk-staging"  # noqa: S108  # nosec B108


# --- Exceptions ---


class DockerError(Exception):
    """Docker is unavailable or the container failed to launch."""


# --- Pure builders ---


def _container_project_path(project_dir: Path, path: Path) -> str:
    """Return the container path for ``path`` under the workspace mount."""
    rel = path.relative_to(project_dir)
    return f"{_WORKSPACE_MOUNT}/{rel.as_posix()}"


def _container_path_for_mount(
    resolved: Path,
    host_mount: Path,
    container_mount: str,
) -> str | None:
    try:
        rel = resolved.relative_to(host_mount.resolve())
    except ValueError:
        return None
    suffix = rel.as_posix()
    if suffix:
        return f"{container_mount}/{suffix}"
    return container_mount


def _rewrite_pip_arg(
    arg: str,
    volume_mounts: Sequence[tuple[Path, str]],
) -> str:
    """Map a host path argument to its container mount path, if applicable."""
    try:
        resolved = Path(arg).resolve()
    except (OSError, ValueError):
        return arg
    mounts = sorted(
        volume_mounts,
        key=lambda item: len(str(item[0].resolve())),
        reverse=True,
    )
    for host_mount, container_mount in mounts:
        mapped = _container_path_for_mount(
            resolved,
            host_mount,
            container_mount,
        )
        if mapped is not None:
            return mapped
    return arg


def build_spark_submit_argv(
    project_dir: Path,
    script: Path,
    job_name: str,
    job_args: Sequence[str],
) -> list[str]:
    """Build ``spark-submit`` tokens for a Glue job inside the container."""
    script_path = _container_project_path(project_dir, script)
    if any(
        token == "--JOB_NAME"  # noqa: S105  # nosec B105
        or token.startswith("--JOB_NAME=")
        for token in job_args
    ):
        args = list(job_args)
    else:
        args = ["--JOB_NAME", job_name, *job_args]
    return ["spark-submit", script_path, *args]


def build_pytest_argv(
    project_dir: Path,
    source_dir: Path,
    tests_dir: Path,
    pytest_args: Sequence[str],
) -> list[str]:
    """Build ``-c`` tokens to run ``pytest`` in the Glue image entrypoint."""
    source_mount = _container_project_path(project_dir, source_dir)
    tests_path = tests_dir.relative_to(project_dir).as_posix()
    targets = (
        pytest_args
        if pytest_args and not pytest_args[0].startswith("-")
        else [tests_path, *pytest_args]
    )
    cmd = (
        f"export PYTHONPATH={shlex_quote(source_mount)}:$PYTHONPATH; "
        f"python3 -m pytest {shlex_join(targets)}"
    )
    return ["-c", cmd]


def build_pip_argv(pip_args: Sequence[str]) -> list[str]:
    """Build ``-c`` tokens to run ``pip`` in the Glue image entrypoint."""
    cmd = f"python3 -m pip {shlex_join(pip_args)}"
    return ["-c", cmd]


def build_run_argv(
    image: str,
    project_dir: Path,
    container_command: Sequence[str],
    *,
    extra_volumes: Sequence[tuple[Path, str]] = (),
) -> list[str]:
    """Build a ``docker run`` argv list for one Glue local container."""
    host_dir = project_dir.resolve()
    argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--platform",
        _DOCKER_PLATFORM,
        "-v",
        f"{host_dir}:{_WORKSPACE_MOUNT}/",
    ]
    for host_path, container_path in extra_volumes:
        argv.extend(
            ["-v", f"{host_path.resolve()}:{container_path}"],
        )
    argv.extend(
        [
            "--workdir",
            _WORKSPACE_MOUNT,
            image,
            *container_command,
        ],
    )
    return argv


# --- Subprocess shell ---


def _ensure_docker_available() -> None:
    if which("docker") is None:
        msg = "docker not found in PATH"
        raise DockerError(msg)


def run_container(argv: Sequence[str]) -> int:
    """Run ``docker`` and return the exit code.

    Container stdout and stderr pass through unchanged.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    _ensure_docker_available()
    try:
        return run(  # noqa: S603
            list(argv),
            check=False,
        ).returncode  # nosec B603
    except OSError as exc:
        raise DockerError(str(exc)) from exc


def run_container_capture(argv: Sequence[str]) -> CompletedProcess[str]:
    """Run ``docker`` and capture stdout and stderr.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    _ensure_docker_available()
    try:
        return run(  # noqa: S603
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )  # nosec B603
    except OSError as exc:
        raise DockerError(str(exc)) from exc


def run_pip_in_container(
    image: str,
    project_dir: Path,
    pip_args: Sequence[str],
    *,
    volume_mounts: Sequence[tuple[Path, str]],
) -> CompletedProcess[str]:
    """Run ``python3 -m pip`` inside the Glue container.

    Rewrites absolute host paths in ``pip_args`` that fall under
    ``volume_mounts`` to their container paths.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    resolved_mounts = [
        (host_path.resolve(), container_path)
        for host_path, container_path in volume_mounts
    ]
    mapped_args = [_rewrite_pip_arg(arg, resolved_mounts) for arg in pip_args]
    argv = build_run_argv(
        image,
        project_dir,
        build_pip_argv(mapped_args),
        extra_volumes=resolved_mounts,
    )
    return run_container_capture(argv)
