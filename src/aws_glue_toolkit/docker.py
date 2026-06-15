"""Run Glue jobs in the official AWS Glue local Docker image.

Pure builders produce ``docker run`` and ``spark-submit`` argv lists;
:func:`run_container` is the subprocess shell.

Public API: :func:`build_run_argv`, :func:`build_spark_submit_argv`,
:func:`container_script_path`, :func:`run_container`, :exc:`DockerError`.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`run_container`.

"""

from __future__ import annotations

from shutil import which
from subprocess import run  # nosec B404
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "DockerError",
    "build_run_argv",
    "build_spark_submit_argv",
    "container_script_path",
    "run_container",
]

_WORKSPACE_MOUNT = "/home/hadoop/workspace"


# --- Exceptions ---


class DockerError(Exception):
    """Docker is unavailable or the container failed to launch."""


# --- Pure builders ---


def container_script_path(project_dir: Path, script: Path) -> str:
    """Return the container path for ``script`` under the workspace mount."""
    rel = script.relative_to(project_dir)
    return f"{_WORKSPACE_MOUNT}/{rel.as_posix()}"


def build_job_args(job_name: str, job_args: Sequence[str]) -> list[str]:
    """Return ``job_args`` with ``--JOB_NAME`` defaulted from ``job_name``."""
    if any(
        token == "--JOB_NAME"  # noqa: S105  # nosec B105
        or token.startswith("--JOB_NAME=")
        for token in job_args
    ):
        return list(job_args)
    return ["--JOB_NAME", job_name, *job_args]


def build_spark_submit_argv(
    script_path: str,
    job_name: str,
    job_args: Sequence[str],
) -> list[str]:
    """Build ``spark-submit`` tokens for a Glue job inside the container."""
    return [
        "spark-submit",
        script_path,
        *build_job_args(job_name, job_args),
    ]


def build_run_argv(
    image: str,
    project_dir: Path,
    container_command: Sequence[str],
) -> list[str]:
    """Build a ``docker run`` argv list for one Glue local container."""
    host_dir = project_dir.resolve()
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "-v",
        f"{host_dir}:{_WORKSPACE_MOUNT}/",
        "--workdir",
        _WORKSPACE_MOUNT,
        image,
        *container_command,
    ]


# --- Subprocess shell ---


def run_container(argv: Sequence[str]) -> int:
    """Run ``docker`` and return the exit code.

    Container stdout and stderr pass through unchanged.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    if which("docker") is None:
        msg = "docker not found in PATH"
        raise DockerError(msg)
    try:
        return run(  # noqa: S603
            list(argv),
            check=False,
        ).returncode  # nosec B603
    except OSError as exc:
        raise DockerError(str(exc)) from exc
