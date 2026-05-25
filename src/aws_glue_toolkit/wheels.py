"""Glue ``.gluewheels.zip`` wheel artifact packaging.

Takes a :class:`~aws_glue_toolkit.dependencies.ResolvedDependencies` value
(typically from :func:`~aws_glue_toolkit.dependencies.resolve_dependencies`
with ``exclude_builtins=True``), downloads x86_64 manylinux2014 wheels with
``pip download``, and writes a ``.gluewheels.zip`` archive per AWS Glue 5.0+
zip-of-wheels guidance.

Public entry point: :func:`build_gluewheels_zip`. Always emits the zip file,
including when there are zero non-built-in wheels to package.

Raises :class:`GlueWheelsBuildError` on wheel download or archive failures.
Dependency conflicts are detected earlier in
:mod:`aws_glue_toolkit.dependencies`.

"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec B404
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final, NamedTuple
from zipfile import ZIP_DEFLATED, ZipFile

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_glue_toolkit.dependencies import ResolvedDependencies
    from aws_glue_toolkit.pyproject import PyProject

__all__ = [
    "GlueWheelsBuildError",
    "GlueWheelsBuildResult",
    "build_gluewheels_zip",
    "glue_wheels_zip_name",
]

# --- Types and naming ---


class GlueWheelsBuildError(Exception):
    """Wheel download or zip packaging failed."""


class GlueWheelsBuildResult(NamedTuple):
    """Output of :func:`build_gluewheels_zip`.

    Attributes:
        output_path: Path to the ``.gluewheels.zip`` file.
        wheel_count: Number of ``.whl`` files in the archive.

    """

    output_path: Path
    wheel_count: int


def glue_wheels_zip_name(pyproject: PyProject) -> str:
    """Return ``{name}-{version}.gluewheels.zip`` from ``[project]`` fields."""
    name = pyproject.project.name or "glue-wheels"
    version = pyproject.project.version or "0.0.0"
    return f"{name}-{version}.gluewheels.zip"


# --- Constants ---

_PIP_PLATFORM: Final[str] = "manylinux2014_x86_64"


def _pip_python_version_tag(python_version: str) -> str:
    return python_version.replace(".", "")


# --- Private I/O ---


@contextmanager
def _wheels_workspace(requirements_txt: str) -> Iterator[Path]:
    """Yield a ``wheels/`` directory containing ``requirements.txt``."""
    with TemporaryDirectory() as tmp:
        wheels_dir = Path(tmp) / "wheels"
        wheels_dir.mkdir()
        (wheels_dir / "requirements.txt").write_text(
            requirements_txt,
            encoding="utf-8",
        )
        yield wheels_dir


def _pip_download_wheels(
    wheels_dir: Path,
    *,
    uv_exe: str,
    python_version: str,
    pip_platform: str,
) -> None:
    """Download wheels into ``wheels_dir`` (includes ``requirements.txt``)."""
    requirements_path = wheels_dir / "requirements.txt"
    cmd = [
        uv_exe,
        "run",
        "--no-project",
        "--with",
        "pip",
        "python",
        "-m",
        "pip",
        "download",
        "-r",
        str(requirements_path),
        "--dest",
        str(wheels_dir),
        "--platform",
        pip_platform,
        "--python-version",
        _pip_python_version_tag(python_version),
        "--only-binary=:all:",
    ]
    try:  # pylint: disable=duplicate-code
        run(  # noqa: S603
            cmd,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )  # nosec B603
    except CalledProcessError as e:
        msg = (e.stderr or e.stdout).strip()
        raise GlueWheelsBuildError(msg) from None


def _create_gluewheels_zip(wheels_dir: Path, output_path: Path) -> int:
    """Zip ``wheels/`` contents; return the number of wheel files included."""
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


# --- Public build API ---


def build_gluewheels_zip(
    resolved: ResolvedDependencies,
    output_path: Path,
    *,
    uv_exe: str,
) -> GlueWheelsBuildResult:
    """Build a ``.gluewheels.zip`` artifact for AWS Glue 5.0+.

    Args:
        resolved: Packageable dependencies from
            :func:`~aws_glue_toolkit.dependencies.resolve_dependencies`
            (with ``exclude_builtins=True``).
        output_path: Destination path for the zip file.
        uv_exe: Path to the ``uv`` executable.

    Returns:
        Output path and wheel count (zero when every dependency is built-in).

    Raises:
        GlueWheelsBuildError: ``pip download`` or packaging failed.

    """
    requirements_txt = resolved.requirements_txt

    with _wheels_workspace(requirements_txt) as wheels_dir:
        if requirements_txt.strip():
            _pip_download_wheels(
                wheels_dir,
                uv_exe=uv_exe,
                python_version=resolved.python_version,
                pip_platform=_PIP_PLATFORM,
            )
        wheel_count = _create_gluewheels_zip(wheels_dir, output_path)

    return GlueWheelsBuildResult(output_path, wheel_count)
