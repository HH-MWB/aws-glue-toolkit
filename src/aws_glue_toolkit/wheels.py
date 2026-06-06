"""Build AWS Glue ``.gluewheels.zip`` wheel archives.

Resolves dependencies via :mod:`aws_glue_toolkit.pip`, omits packages already
on the Glue image at the same pinned version, downloads wheels for the
runtime ``pip_platform``, and zips them for Glue 5.0+
``--additional-python-modules``.

Public API: :func:`build_gluewheels_zip`, :class:`GlueWheelsBuildResult`,
:exc:`GlueWheelsBuildError`.

Always writes the zip, including when there are zero wheels to package.

"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, NamedTuple
from zipfile import ZIP_DEFLATED, ZipFile

from aws_glue_toolkit.pip import PipError, download_wheels, resolve_packages

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_glue_toolkit.job import GlueJobProject

__all__ = [
    "GlueWheelsBuildError",
    "GlueWheelsBuildResult",
    "build_gluewheels_zip",
]


class GlueWheelsBuildError(Exception):
    """Resolution, download, or zip step failed.

    Resolution and download failures from :mod:`aws_glue_toolkit.pip` wrap
    :exc:`~aws_glue_toolkit.pip.PipError` for callers and the CLI.
    """


class GlueWheelsBuildResult(NamedTuple):
    """Result of :func:`build_gluewheels_zip`.

    Attributes:
        output_path: Path to the ``.gluewheels.zip`` file.
        wheel_count: Number of ``.whl`` files in the archive.

    """

    output_path: Path
    wheel_count: int


def _packages_for_job(job: GlueJobProject) -> dict[str, str]:
    """Resolve job deps and return non-built-in packages."""
    if not job.dependencies:
        return {}
    try:
        packages = resolve_packages(
            job.dependencies,
            job.runtime_metadata.python_packages,
            python_version=job.runtime_metadata.core_engines.python,
            platform=job.runtime_metadata.pip_platform,
        )
    except PipError as exc:
        raise GlueWheelsBuildError(str(exc)) from exc
    runtime = job.runtime_metadata.python_packages
    return {
        name: version
        for name, version in packages.items()
        if runtime.get(name) != version
    }


# --- I/O shell ---


@contextmanager
def _wheels_workspace() -> Iterator[Path]:
    """Yield a temp ``wheels/`` directory."""
    with TemporaryDirectory() as tmp:
        wheels_dir = Path(tmp) / "wheels"
        wheels_dir.mkdir()
        yield wheels_dir


def _create_gluewheels_zip(wheels_dir: Path, output_path: Path) -> int:
    """Zip the workspace tree; return the number of ``.whl`` files included."""
    root = wheels_dir.parent
    wheel_count = 0
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in wheels_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix == ".whl":
                wheel_count += 1
            archive.write(path, arcname=path.relative_to(root))
    return wheel_count


def build_gluewheels_zip(job: GlueJobProject) -> GlueWheelsBuildResult:
    """Build a ``.gluewheels.zip`` at the job's default artifact path.

    Resolves and filters dependencies, downloads wheels, then zips the temp
    workspace. Platform and Python version come from
    ``job.runtime_metadata``.

    Args:
        job: Resolved config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.

    Returns:
        Output path and wheel count (may be zero).

    Raises:
        GlueWheelsBuildError: ``pip`` resolution or download failed.

    """
    packages = _packages_for_job(job)
    runtime = job.runtime_metadata
    python_version = runtime.core_engines.python
    with _wheels_workspace() as wheels_dir:
        try:
            download_wheels(
                packages,
                wheels_dir,
                python_version=python_version,
                platform=runtime.pip_platform,
            )
        except PipError as exc:
            raise GlueWheelsBuildError(str(exc)) from exc
        wheel_count = _create_gluewheels_zip(
            wheels_dir,
            job.glue_wheels_zip_path,
        )

    return GlueWheelsBuildResult(job.glue_wheels_zip_path, wheel_count)
