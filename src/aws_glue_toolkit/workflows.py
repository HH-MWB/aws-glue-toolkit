"""Workflow orchestration for ``gtk`` use cases.

One function per user-facing workflow: ``check``, ``build``, ``run``,
``test``. No Rich panels, Cyclopts, or exit codes — callers in
:mod:`aws_glue_toolkit.cli` map exceptions to user-facing output.

This module wires :mod:`aws_glue_toolkit.pip` to
:mod:`aws_glue_toolkit.docker` for pip-in-container execution.

"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_glue_toolkit.artifacts import (
    build_dependencies_zip,
    stage_gluewheels_zip,
    write_gluewheels_tree,
)
from aws_glue_toolkit.docker import (
    GLUEWHEELS_STAGING_MOUNT,
    PIP_WORK_MOUNT,
    run_job,
    run_pip_in_container,
    run_tests,
)
from aws_glue_toolkit.pip import (
    PipRunner,
    download_wheels,
    pip_error_from_returncode,
    resolve_packages,
    resolve_packages_to_bundle,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from aws_glue_toolkit.job import GlueJobProject

__all__ = [
    "build",
    "check",
    "run",
    "test",
]


def _docker_pip_runner(job: GlueJobProject) -> PipRunner:
    """Return a :class:`~aws_glue_toolkit.pip.PipRunner` for ``job``."""

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
        if result.returncode != 0:
            raise pip_error_from_returncode(
                result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    return execute_pip


def _resolve_in_docker(job: GlueJobProject) -> dict[str, str]:
    return resolve_packages(
        job.dependencies,
        job.runtime,
        runner=_docker_pip_runner(job),
        pip_work_mount=PIP_WORK_MOUNT,
    )


def _build_gluewheels_zip(job: GlueJobProject) -> None:
    runner = _docker_pip_runner(job)
    packages = resolve_packages_to_bundle(
        job.dependencies,
        job.runtime,
        runner=runner,
        pip_work_mount=PIP_WORK_MOUNT,
    )
    destination = job.project_dir / job.gluewheels_zip_filename
    with stage_gluewheels_zip(destination) as wheels_dir:
        write_gluewheels_tree(wheels_dir, packages)
        download_wheels(
            packages,
            wheels_dir,
            job.runtime,
            runner=runner,
            gluewheels_staging_mount=GLUEWHEELS_STAGING_MOUNT,
        )


def check(job: GlueJobProject) -> None:
    """Verify job dependencies resolve against Glue runtime pins.

    Raises:
        PipError: Requirements are unsatisfiable.

    """
    _resolve_in_docker(job)


def build(job: GlueJobProject) -> Path:
    """Build dependencies and gluewheels zips under the job directory.

    Returns:
        ``job.project_dir``.

    Raises:
        OSError: Host zip I/O failed.
        PipError: Wheel resolution or download failed.

    """
    build_dependencies_zip(
        job.source_dir,
        job.script,
        job.project_dir / job.dependencies_zip_filename,
    )
    _build_gluewheels_zip(job)
    return job.project_dir


def run(job: GlueJobProject, *job_args: str) -> int:
    """Run the job via ``spark-submit`` in the Glue Docker image."""
    return run_job(job, *job_args)


def test(job: GlueJobProject, *pytest_args: str) -> int:
    """Run pytest in the Glue Docker image.

    Raises:
        ValueError: Configured tests directory does not exist.

    """
    return run_tests(job, *pytest_args)
