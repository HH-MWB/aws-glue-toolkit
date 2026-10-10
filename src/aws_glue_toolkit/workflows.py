"""Application orchestration for ``gtk`` build, run, and test.

Wires :mod:`aws_glue_toolkit.requirements` and :mod:`aws_glue_toolkit.wheels`
to :mod:`aws_glue_toolkit.container` (host pip for ``build --mode host``,
container pip for ``build --mode container``, ``run``, and ``test``).
No Rich panels, Cyclopts, or exit codes — callers in
:mod:`aws_glue_toolkit.cli` map exceptions to user-facing output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from aws_glue_toolkit.artifacts import (
    build_dependencies_zip,
    stage_gluewheels_zip,
    write_gluewheels_tree,
)
from aws_glue_toolkit.container import (
    PytestContainerEntry,
    SparkContainerEntry,
    host_pip_runner,
    pip_runner,
    run_in_glue_container,
)
from aws_glue_toolkit.requirements import (
    open_dependency_session,
    staged_requirements,
)
from aws_glue_toolkit.wheels import bundle_wheels

if TYPE_CHECKING:
    from pathlib import Path

    from aws_glue_toolkit.job import GlueJobProject

__all__ = [
    "build",
    "run",
    "test",
]


def _bundle_wheels_for_mode(
    job: GlueJobProject,
    dest: Path,
    *,
    mode: Literal["host", "container"],
) -> dict[str, str]:
    """Prepare deps and run :func:`bundle_wheels` for ``mode``."""
    host = mode == "host"
    session = open_dependency_session(
        job,
        in_container=not host,
        runner=host_pip_runner() if host else pip_runner(job),
    )
    return bundle_wheels(
        prepared=session.prepared,
        policy=session.policy,
        dest=dest,
        runner=session.runner,
        cross_platform=host,
    )


def build(
    job: GlueJobProject,
    *,
    mode: Literal["host", "container"],
) -> Path:
    """Build dependencies and gluewheels zips under the job directory.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        mode: ``"host"`` packages with host pip; ``"container"`` runs
            ``pip wheel`` in the Glue Docker image (worker-arch).

    Returns:
        ``job.project_dir``.

    Raises:
        OSError: Host zip I/O failed.
        PipError: Wheel resolution or bundling failed.
        RequirementPreparationError: A dependency spec could not be prepared.

    """
    # Dependencies zip: every .py under source except the entry script.
    build_dependencies_zip(
        job.source_dir,
        job.script,
        job.project_dir / job.dependencies_zip_filename,
    )

    # Gluewheels zip via host pip (--mode host) or Docker (--mode container).
    with stage_gluewheels_zip(
        job.project_dir / job.gluewheels_zip_filename,
    ) as wheels_dir:
        packages = _bundle_wheels_for_mode(job, wheels_dir, mode=mode)
        write_gluewheels_tree(wheels_dir, packages)
    return job.project_dir


def run(
    job: GlueJobProject,
    *job_args: str,
    platform: Literal["native", "worker"] = "native",
) -> int:
    """Run the job in the Glue Docker image, installing deps when needed.

    When ``job.dependencies`` is non-empty, stages requirements and runs
    ``pip install --target`` in the same ephemeral container before
    ``spark-submit``. Returns when the job finishes.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        *job_args: Tokens forwarded to the job after ``--JOB_NAME``.
        platform: ``"native"`` uses the host multi-arch Glue image;
            ``"worker"`` uses ``runtime.worker_docker_platform``.

    Returns:
        Container exit code.

    Raises:
        RequirementPreparationError: A dependency spec could not be prepared.
        DockerError: Docker is unavailable or the container failed to launch.

    """
    docker_platform = (
        None if platform == "native" else job.runtime.worker_docker_platform
    )
    entry = SparkContainerEntry(job_args=job_args)
    if not job.dependencies:
        return run_in_glue_container(
            job,
            platform=docker_platform,
            entry=entry,
            dependency_session=None,
        )

    session = open_dependency_session(
        job,
        in_container=True,
        runner=pip_runner(job),
    )
    with staged_requirements(session.prepared, session.policy) as (
        _work,
        mounts,
    ):
        return run_in_glue_container(
            job,
            platform=docker_platform,
            entry=entry,
            dependency_session=session,
            extra_volumes=mounts,
        )


def test(
    job: GlueJobProject,
    *pytest_args: str,
    platform: Literal["native", "worker"] = "native",  # noqa: PT028
) -> int:
    """Run pytest in the Glue Docker image, installing deps when needed.

    When ``job.dependencies`` is non-empty, stages requirements and runs
    ``pip install --target`` in the same ephemeral container before pytest.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        *pytest_args: Tokens forwarded to ``pytest``.
        platform: ``"native"`` uses the host multi-arch Glue image;
            ``"worker"`` uses ``runtime.worker_docker_platform``.

    Returns:
        Container exit code.

    Raises:
        ValueError: Configured tests directory does not exist.
        RequirementPreparationError: A dependency spec could not be prepared.
        DockerError: Docker is unavailable or the container failed to launch.

    """
    if not job.tests_dir.is_dir():
        msg = f"tests directory not found: {job.tests_dir}"
        raise ValueError(msg)

    docker_platform = (
        None if platform == "native" else job.runtime.worker_docker_platform
    )
    entry = PytestContainerEntry(pytest_args=pytest_args)
    if not job.dependencies:
        return run_in_glue_container(
            job,
            platform=docker_platform,
            entry=entry,
            dependency_session=None,
        )

    session = open_dependency_session(
        job,
        in_container=True,
        runner=pip_runner(job),
    )
    with staged_requirements(session.prepared, session.policy) as (
        _work,
        mounts,
    ):
        return run_in_glue_container(
            job,
            platform=docker_platform,
            entry=entry,
            dependency_session=session,
            extra_volumes=mounts,
        )
