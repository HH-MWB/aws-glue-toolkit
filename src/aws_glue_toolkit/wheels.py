"""Build AWS Glue ``.gluewheels.zip`` wheel archives.

Downloads x86_64 manylinux2014 wheels with ``uv pip download`` and zips them
for Glue 5.0+ ``--additional-python-modules`` (zip-of-wheels).

Public API: :func:`build_gluewheels_zip` → :class:`GlueWheelsBuildResult`.

Typical flow: :func:`~aws_glue_toolkit.dependencies.resolve_dependencies`
with ``exclude_builtins=True``, then :func:`build_gluewheels_zip`.

Always writes the zip, including when there are zero wheels to package.

Raises :class:`GlueWheelsBuildError` on download failure. Dependency
conflicts are reported by :mod:`aws_glue_toolkit.dependencies` earlier.
Requires ``uv`` on ``PATH`` (see :mod:`aws_glue_toolkit.uv`).

"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final, NamedTuple
from zipfile import ZIP_DEFLATED, ZipFile

from aws_glue_toolkit.uv import UvCommandError, uv

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_glue_toolkit.dependencies import ResolvedDependencies

__all__ = [
    "GlueWheelsBuildError",
    "GlueWheelsBuildResult",
    "build_gluewheels_zip",
]

# ``pip download --platform`` for Glue x86_64 workers (manylinux2014).
_PIP_PLATFORM: Final[str] = "manylinux2014_x86_64"


class GlueWheelsBuildError(Exception):
    """``uv pip download`` or zip creation failed.

    Download failures wrap :exc:`~aws_glue_toolkit.uv.UvCommandError` with a
    domain-specific type for callers and the CLI.
    """


class GlueWheelsBuildResult(NamedTuple):
    """Result of :func:`build_gluewheels_zip`.

    Attributes:
        output_path: Path to the ``.gluewheels.zip`` file.
        wheel_count: Number of ``.whl`` files in the archive.

    """

    output_path: Path
    wheel_count: int


@contextmanager
def _wheels_workspace(requirements_txt: str) -> Iterator[Path]:
    """Yield a temp ``wheels/`` directory containing ``requirements.txt``."""
    with TemporaryDirectory() as tmp:
        wheels_dir = Path(tmp) / "wheels"
        wheels_dir.mkdir()
        (wheels_dir / "requirements.txt").write_text(
            requirements_txt,
            encoding="utf-8",
        )
        yield wheels_dir


def _pip_download_wheels(wheels_dir: Path, *, python_version: str) -> None:
    """Download wheels into ``wheels_dir`` using :data:`_PIP_PLATFORM`.

    Raises:
        GlueWheelsBuildError: ``uv pip download`` failed.

    """
    requirements_path = wheels_dir / "requirements.txt"
    try:
        uv(
            [
                "pip",
                "download",
                "-r",
                str(requirements_path),
                "--dest",
                str(wheels_dir),
                "--platform",
                _PIP_PLATFORM,
                "--python-version",
                python_version.replace(".", ""),
                "--only-binary=:all:",
            ],
        )
    except UvCommandError as e:
        raise GlueWheelsBuildError(str(e)) from e


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


def build_gluewheels_zip(
    resolved: ResolvedDependencies,
    output_path: Path,
) -> GlueWheelsBuildResult:
    """Build a ``.gluewheels.zip`` at ``output_path``.

    Args:
        resolved: Output of
            :func:`~aws_glue_toolkit.dependencies.resolve_dependencies`
            (typically with ``exclude_builtins=True``).
        output_path: Destination zip path.

    Returns:
        Output path and wheel count (may be zero).

    Raises:
        GlueWheelsBuildError: Download or zip step failed.
        UvNotFoundError: Propagated when ``uv`` is not on ``PATH``.

    """
    requirements_txt = resolved.requirements_txt

    with _wheels_workspace(requirements_txt) as wheels_dir:
        if requirements_txt.strip():
            _pip_download_wheels(
                wheels_dir,
                python_version=resolved.python_version,
            )
        wheel_count = _create_gluewheels_zip(wheels_dir, output_path)

    return GlueWheelsBuildResult(output_path, wheel_count)
