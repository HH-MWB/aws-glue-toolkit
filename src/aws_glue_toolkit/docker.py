"""Run Glue jobs and tests in the official AWS Glue local Docker image.

Pure builders produce ``docker run``, ``spark-submit``, and ``pytest`` argv
lists; :func:`run_container` is the subprocess shell.

Public API: :func:`build_run_argv`, :func:`build_spark_submit_argv`,
:func:`build_pytest_argv`, :func:`run_container`, :exc:`DockerError`.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`run_container`.

"""

from __future__ import annotations

from shlex import join as shlex_join
from shlex import quote as shlex_quote
from shutil import which
from subprocess import run  # nosec B404
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "DockerError",
    "build_pytest_argv",
    "build_run_argv",
    "build_spark_submit_argv",
    "run_container",
]

_WORKSPACE_MOUNT = "/home/hadoop/workspace"


# --- Exceptions ---


class DockerError(Exception):
    """Docker is unavailable or the container failed to launch."""


# --- Pure builders ---


def _container_project_path(project_dir: Path, path: Path) -> str:
    """Return the container path for ``path`` under the workspace mount."""
    rel = path.relative_to(project_dir)
    return f"{_WORKSPACE_MOUNT}/{rel.as_posix()}"


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
