"""Application orchestration for ``gtk`` check, build, run, and test.

Wires :mod:`aws_glue_toolkit.dependencies` to
:mod:`aws_glue_toolkit.docker` (container pip, or host pip for
``build --mode fast``). No Rich panels, Cyclopts, or exit codes — callers
in :mod:`aws_glue_toolkit.cli` map exceptions to user-facing output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from aws_glue_toolkit.artifacts import (
    build_dependencies_zip,
    stage_gluewheels_zip,
    write_gluewheels_tree,
)
from aws_glue_toolkit.dependencies import (
    bundle_wheels,
    prepare_requirements,
    resolve_packages,
    staged_requirements,
)
from aws_glue_toolkit.docker import (
    host_pip_runner,
    pip_runner,
    run_job,
    run_tests,
)

if TYPE_CHECKING:
    from pathlib import Path

    from aws_glue_toolkit.job import GlueJobProject

__all__ = [
    "build",
    "check",
    "run",
    "test",
]


def check(job: GlueJobProject) -> None:
    """Verify job dependencies resolve against Glue runtime pins.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.

    Raises:
        PipError: Requirements are unsatisfiable.
        RequirementPreparationError: A dependency spec could not be prepared.

    """
    resolve_packages(
        prepared=prepare_requirements(job.dependencies, job.project_dir),
        runtime=job.runtime,
        runner=pip_runner(job),
    )


def build(
    job: GlueJobProject,
    *,
    mode: Literal["fast"] | None = None,
) -> Path:
    """Build dependencies and gluewheels zips under the job directory.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        mode: ``"fast"`` packages on the host; omit for ``pip wheel`` in
            the Glue Docker image.

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

    fast = mode == "fast"
    # Gluewheels zip via Docker or host (--mode fast).
    with stage_gluewheels_zip(
        job.project_dir / job.gluewheels_zip_filename,
    ) as wheels_dir:
        packages = bundle_wheels(
            prepared=prepare_requirements(
                job.dependencies,
                job.project_dir,
                for_container=not fast,
            ),
            runtime=job.runtime,
            dest=wheels_dir,
            runner=host_pip_runner() if fast else pip_runner(job),
            cross_platform=fast,
        )
        write_gluewheels_tree(wheels_dir, packages)
    return job.project_dir


def run(job: GlueJobProject, *job_args: str) -> int:
    """Run the job in the Glue Docker image, installing deps when needed.

    When ``job.dependencies`` is non-empty, stages requirements and runs
    ``pip install --target`` in the same ephemeral container before
    ``spark-submit``. Returns when the job finishes.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        *job_args: Tokens forwarded to the job after ``--JOB_NAME``.

    Returns:
        Container exit code.

    Raises:
        RequirementPreparationError: A dependency spec could not be prepared.
        DockerError: Docker is unavailable or the container failed to launch.

    """
    if not job.dependencies:
        return run_job(job, *job_args)

    prepared = prepare_requirements(job.dependencies, job.project_dir)
    with staged_requirements(prepared, job.runtime) as (_work, mounts):
        return run_job(
            job,
            *job_args,
            extra_volumes=mounts,
            install_deps=True,
        )


def test(job: GlueJobProject, *pytest_args: str) -> int:
    """Run pytest in the Glue Docker image, installing deps when needed.

    When ``job.dependencies`` is non-empty, stages requirements and runs
    ``pip install --target`` in the same ephemeral container before pytest.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        *pytest_args: Tokens forwarded to ``pytest``.

    Returns:
        Container exit code.

    Raises:
        ValueError: Configured tests directory does not exist.
        RequirementPreparationError: A dependency spec could not be prepared.
        DockerError: Docker is unavailable or the container failed to launch.

    """
    if not job.dependencies:
        return run_tests(job, *pytest_args)

    prepared = prepare_requirements(job.dependencies, job.project_dir)
    with staged_requirements(prepared, job.runtime) as (_work, mounts):
        return run_tests(
            job,
            *pytest_args,
            extra_volumes=mounts,
            install_deps=True,
        )
