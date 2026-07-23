"""Run Glue jobs, tests, and pip in the official AWS Glue local Docker image.

Pure builders produce ``docker run``, ``spark-submit``, ``pytest``, and pip
argv lists; :func:`run_container` and :func:`run_container_capture` are the
subprocess shells.

Public API: :func:`build_run_argv`, :func:`build_spark_submit_argv`,
:func:`build_pytest_argv`, :func:`build_pip_argv`, :func:`run_container`,
:func:`run_container_capture`, :func:`run_pip_in_container`,
:func:`run_job`, :func:`run_tests`, :func:`pip_runner`,
:exc:`DockerError`.

Mount constants :data:`~aws_glue_toolkit.paths.PIP_WORK_MOUNT` and
:data:`~aws_glue_toolkit.paths.GLUEWHEELS_STAGING_MOUNT` are re-exported
from :mod:`aws_glue_toolkit.paths` for backward compatibility.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`run_container` and :func:`run_container_capture`.

"""

from __future__ import annotations

from os import environ
from shlex import join as shlex_join
from shlex import quote as shlex_quote
from shutil import which
from subprocess import CompletedProcess, run  # nosec B404
from typing import TYPE_CHECKING

from aws_glue_toolkit.dependencies import (
    PipRunner,
    pip_error_from_returncode,
    pip_install_target_args,
)
from aws_glue_toolkit.paths import (
    GLUEWHEELS_STAGING_MOUNT,
    PIP_WORK_MOUNT,
    PYTHON_TARGET_MOUNT,
    WORKSPACE_MOUNT,
    project_path,
    rewrite_path_arg,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from aws_glue_toolkit.job import GlueJobProject

__all__ = [
    "GLUEWHEELS_STAGING_MOUNT",
    "PIP_WORK_MOUNT",
    "DockerError",
    "build_pip_argv",
    "build_pytest_argv",
    "build_run_argv",
    "build_spark_submit_argv",
    "pip_runner",
    "run_container",
    "run_container_capture",
    "run_job",
    "run_pip_in_container",
    "run_tests",
]

_DOCKER_PLATFORM = "linux/amd64"
_PIP_INDEX_ENV = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")


# --- Exceptions ---


class DockerError(Exception):
    """Docker is unavailable or the container failed to launch."""


# --- Pure builders ---


def build_spark_submit_argv(
    project_dir: Path,
    script: Path,
    job_name: str,
    job_args: Sequence[str],
) -> list[str]:
    """Build ``spark-submit`` tokens for a Glue job inside the container."""
    script_path = project_path(project_dir, script)

    # Inject --JOB_NAME unless the caller already set it.
    if any(
        token == "--JOB_NAME"  # noqa: S105  # nosec B105
        or token.startswith("--JOB_NAME=")
        for token in job_args
    ):
        args = list(job_args)
    else:
        args = ["--JOB_NAME", job_name, *job_args]

    return ["spark-submit", script_path, *args]


def _pytest_targets(
    project_dir: Path,
    tests_dir: Path,
    pytest_args: Sequence[str],
) -> Sequence[str]:
    """Return pytest path/option tokens for the container command."""
    tests_path = tests_dir.relative_to(project_dir).as_posix()
    if pytest_args and not pytest_args[0].startswith("-"):
        return pytest_args
    return [tests_path, *pytest_args]


def build_pytest_argv(
    project_dir: Path,
    source_dir: Path,
    tests_dir: Path,
    pytest_args: Sequence[str],
) -> list[str]:
    """Build ``-c`` tokens to run ``pytest`` in the Glue image entrypoint."""
    source_mount = project_path(project_dir, source_dir)
    targets = _pytest_targets(project_dir, tests_dir, pytest_args)

    cmd = (
        f"export PYTHONPATH={shlex_quote(source_mount)}:$PYTHONPATH; "
        f"python3 -m pytest {shlex_join(targets)}"
    )
    return ["-c", cmd]


def _pip_install_prefix() -> str:
    """Shell prefix that installs deps into :data:`PYTHON_TARGET_MOUNT`."""
    return f"python3 -m pip {shlex_join(pip_install_target_args())}"


def build_install_and_spark_submit_argv(
    project_dir: Path,
    script: Path,
    job_name: str,
    job_args: Sequence[str],
) -> list[str]:
    """Build ``-c`` tokens: pip install ``--target``, then ``spark-submit``."""
    submit = shlex_join(
        build_spark_submit_argv(project_dir, script, job_name, job_args),
    )
    cmd = (
        f"{_pip_install_prefix()} && "
        f"export PYTHONPATH={shlex_quote(PYTHON_TARGET_MOUNT)}:$PYTHONPATH && "
        f"{submit}"
    )
    return ["-c", cmd]


def build_install_and_pytest_argv(
    project_dir: Path,
    source_dir: Path,
    tests_dir: Path,
    pytest_args: Sequence[str],
) -> list[str]:
    """Build ``-c`` tokens: pip install ``--target``, then ``pytest``."""
    source_mount = project_path(project_dir, source_dir)
    targets = _pytest_targets(project_dir, tests_dir, pytest_args)
    cmd = (
        f"{_pip_install_prefix()} && "
        f"export PYTHONPATH={shlex_quote(PYTHON_TARGET_MOUNT)}:"
        f"{shlex_quote(source_mount)}:$PYTHONPATH; "
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
    env_forwards: Sequence[str] = (),
) -> list[str]:
    """Build a ``docker run`` argv list for one Glue local container."""
    host_dir = project_dir.resolve()
    return [
        # Base: ephemeral, interactive, linux/amd64 (Glue local images).
        "docker",
        "run",
        "--rm",
        "-i",
        "--platform",
        _DOCKER_PLATFORM,
        # Optional host env pass-through (-e VAR copies value from host).
        *[flag for var in env_forwards for flag in ("-e", var)],
        # Job root mounted read-write at the Glue workspace path.
        "-v",
        f"{host_dir}:{WORKSPACE_MOUNT}/",
        # Extra binds: pip work dir, wheel staging, file: dependency paths.
        *[
            flag
            for host_path, container_path in extra_volumes
            for flag in ("-v", f"{host_path.resolve()}:{container_path}")
        ],
        # Image entrypoint (spark-submit, pytest, pip shell command, …).
        "--workdir",
        WORKSPACE_MOUNT,
        image,
        *container_command,
    ]


# --- Subprocess shell ---


def _ensure_docker_available() -> None:
    if which("docker") is None:
        msg = "docker not found in PATH"
        raise DockerError(msg)


def _pip_index_env_forwards() -> tuple[str, ...]:
    """Host pip index env vars to forward into the container when set."""
    return tuple(v for v in _PIP_INDEX_ENV if v in environ)


def run_container(argv: Sequence[str]) -> int:
    """Run ``docker`` and return the exit code.

    Container stdout and stderr pass through unchanged.

    Args:
        argv: Full ``docker run …`` argv list from :func:`build_run_argv`.

    Returns:
        Container process exit code.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    _ensure_docker_available()
    try:
        return run(  # noqa: S603
            argv,
            check=False,
        ).returncode  # nosec B603
    except OSError as exc:
        raise DockerError(str(exc)) from exc


def run_container_capture(argv: Sequence[str]) -> CompletedProcess[str]:
    """Run ``docker`` and capture stdout and stderr.

    Args:
        argv: Full ``docker run …`` argv list from :func:`build_run_argv`.

    Returns:
        Completed subprocess result with captured stdout and stderr.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    _ensure_docker_available()
    try:
        return run(  # noqa: S603
            argv,
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

    Args:
        image: Glue local Docker image reference.
        project_dir: Job root directory mounted at
            :data:`~aws_glue_toolkit.paths.WORKSPACE_MOUNT`.
        pip_args: Arguments passed to ``python3 -m pip``.
        volume_mounts: Extra ``(host_path, container_mount)`` pairs for
            this run.

    Returns:
        Completed subprocess result with captured stdout and stderr.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    resolved_mounts = [
        (host_path.resolve(), container_path)
        for host_path, container_path in volume_mounts
    ]

    # Map host paths in pip argv to container mount paths.
    mapped_args = [rewrite_path_arg(arg, resolved_mounts) for arg in pip_args]

    argv = build_run_argv(
        image,
        project_dir,
        build_pip_argv(mapped_args),
        extra_volumes=resolved_mounts,
        # Host pip index config (see README “Pip index URLs”).
        env_forwards=_pip_index_env_forwards(),
    )
    return run_container_capture(argv)


def pip_runner(job: GlueJobProject) -> PipRunner:
    """Return a :class:`~aws_glue_toolkit.dependencies.PipRunner`."""

    def execute_pip(
        args: Sequence[str],
        volume_mounts: Sequence[tuple[Path, str]],
    ) -> None:
        result = run_pip_in_container(
            job.runtime.docker_image,
            job.project_dir,
            args,
            volume_mounts=volume_mounts,
        )

        # Translate non-zero pip exit codes to PipError.
        if result.returncode != 0:
            raise pip_error_from_returncode(
                result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    return execute_pip


def run_job(
    job: GlueJobProject,
    *job_args: str,
    extra_volumes: Sequence[tuple[Path, str]] = (),
    install_deps: bool = False,
) -> int:
    """Run the job via ``spark-submit`` in the Glue Docker image.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        *job_args: Tokens forwarded to ``spark-submit`` after ``--JOB_NAME``.
        extra_volumes: Extra host→container binds (pip work, ``file:`` deps).
        install_deps: When true, pip-install job deps into the ephemeral
            container before ``spark-submit`` and forward pip index env vars.

    Returns:
        Container exit code.

    """
    if install_deps:
        container_command = build_install_and_spark_submit_argv(
            job.project_dir,
            job.script,
            job.name,
            job_args,
        )
        env_forwards = _pip_index_env_forwards()
    else:
        container_command = build_spark_submit_argv(
            job.project_dir,
            job.script,
            job.name,
            job_args,
        )
        env_forwards = ()

    return run_container(
        build_run_argv(
            job.runtime.docker_image,
            job.project_dir,
            container_command,
            extra_volumes=extra_volumes,
            env_forwards=env_forwards,
        ),
    )


def run_tests(
    job: GlueJobProject,
    *pytest_args: str,
    extra_volumes: Sequence[tuple[Path, str]] = (),
    install_deps: bool = False,
) -> int:
    """Run pytest in the Glue Docker image.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        *pytest_args: Tokens forwarded to ``pytest``.
        extra_volumes: Extra host→container binds (pip work, ``file:`` deps).
        install_deps: When true, pip-install job deps into the ephemeral
            container before pytest and forward pip index env vars.

    Returns:
        Container exit code.

    Raises:
        ValueError: Configured tests directory does not exist.

    """
    if not job.tests_dir.is_dir():
        msg = f"tests directory not found: {job.tests_dir}"
        raise ValueError(msg)

    if install_deps:
        container_command = build_install_and_pytest_argv(
            job.project_dir,
            job.source_dir,
            job.tests_dir,
            pytest_args,
        )
        env_forwards = _pip_index_env_forwards()
    else:
        container_command = build_pytest_argv(
            job.project_dir,
            job.source_dir,
            job.tests_dir,
            pytest_args,
        )
        env_forwards = ()

    return run_container(
        build_run_argv(
            job.runtime.docker_image,
            job.project_dir,
            container_command,
            extra_volumes=extra_volumes,
            env_forwards=env_forwards,
        ),
    )
