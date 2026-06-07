"""Build AWS Glue ``.gluewheels.zip`` wheel archives.

For Glue 5.0+ ``--additional-python-modules``, packages extra Python libraries
into a zip artifact per `AWS Glue Appendix A
<https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html>`_:

::

    wheels/
      requirements.txt   # resolved ``name==version`` pins
      *.whl

Pipeline:

1. :func:`~aws_glue_toolkit.runtime.load_runtime` for constraints, Python,
   and platform.
2. Resolve requirements and omit Glue built-ins at the same pinned version.
3. Stage ``wheels/``, write ``requirements.txt``, download wheels.
4. Zip the staging tree to ``destination``.

Public API: :func:`build_gluewheels_zip`, :exc:`GlueWheelsBuildError`.

Always writes ``destination``, including when there are zero wheels to bundle.

"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from zipfile import ZIP_DEFLATED, ZipFile

from aws_glue_toolkit.pip import PipError, download_wheels, resolve_packages
from aws_glue_toolkit.runtime import load_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "GlueWheelsBuildError",
    "build_gluewheels_zip",
]


# --- Errors ---


class GlueWheelsBuildError(Exception):
    """Building a ``.gluewheels.zip`` failed.

    Resolution and download failures from :mod:`aws_glue_toolkit.pip` wrap
    :exc:`~aws_glue_toolkit.pip.PipError` for callers and the CLI.
    """


# --- Resolution ---


def _resolve_packages_to_bundle(
    requirements: Sequence[str],
    runtime_package_pins: Mapping[str, str],
    *,
    python_version: str,
    platform: str,
) -> dict[str, str]:
    """Resolve requirements and return packages to download into the archive.

    Uses :func:`~aws_glue_toolkit.pip.resolve_packages`, then drops packages
    whose resolved version matches a Glue built-in pin (constraints limit
    versions during resolution but do not remove packages from the report).

    Args:
        requirements: Direct dependency requirements (PEP 508 strings).
        runtime_package_pins: Bundled Glue runtime pins used as ``pip``
            constraints and for built-in filtering.
        python_version: Target Python (e.g. ``"3.11"``).
        platform: ``pip --platform`` tag for Glue workers.

    Returns:
        Package name → version pins to bundle.

    Raises:
        GlueWheelsBuildError: ``pip`` resolution failed.

    """
    try:
        resolved = resolve_packages(
            requirements,
            runtime_package_pins,
            python_version=python_version,
            platform=platform,
        )
    except PipError as exc:
        raise GlueWheelsBuildError(str(exc)) from exc
    return {
        name: version
        for name, version in resolved.items()
        if runtime_package_pins.get(name) != version
    }


# --- I/O ---


@contextmanager
def _gluewheels_staging(destination: Path) -> Iterator[Path]:
    """Yield a temp ``wheels/`` directory; zip the tree on context exit.

    Creates ``<temp>/wheels/``. After the ``with`` block, zips every file under
    the temp root (arcnames relative to that root) and writes ``destination``.

    Args:
        destination: Final path for the ``.gluewheels.zip`` file.

    Yields:
        Path to the ``wheels/`` subdirectory for population.

    """
    with TemporaryDirectory() as tmp:
        staging_root = Path(tmp)
        wheels_dir = staging_root / "wheels"
        wheels_dir.mkdir()
        yield wheels_dir
        destination.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
            for path in (p for p in staging_root.rglob("*") if p.is_file()):
                archive.write(path, arcname=path.relative_to(staging_root))


def _assemble_gluewheels_zip(
    packages: Mapping[str, str],
    destination: Path,
    *,
    python_version: str,
    platform: str,
) -> None:
    """Write ``wheels/`` contents and produce ``destination``.

    Writes ``wheels/requirements.txt`` with resolved pins, downloads wheels via
    :func:`~aws_glue_toolkit.pip.download_wheels`, then zips via
    :func:`_gluewheels_staging`.

    Args:
        packages: Resolved name → version pins to bundle.
        destination: Final path for the ``.gluewheels.zip`` file.
        python_version: Target Python (e.g. ``"3.11"``).
        platform: ``pip --platform`` tag for Glue workers.

    Raises:
        GlueWheelsBuildError: ``pip`` download failed.

    """
    with _gluewheels_staging(destination) as wheels_dir:
        (wheels_dir / "requirements.txt").write_text(
            "\n".join(
                f"{name}=={version}"
                for name, version in sorted(packages.items())
            ),
            encoding="utf-8",
        )
        try:
            download_wheels(
                packages,
                wheels_dir,
                python_version=python_version,
                platform=platform,
            )
        except PipError as exc:
            raise GlueWheelsBuildError(str(exc)) from exc


# --- Public API ---


def build_gluewheels_zip(
    requirements: Sequence[str],
    glue_version: str,
    destination: Path,
) -> None:
    """Create a ``.gluewheels.zip`` at ``destination``.

    Args:
        requirements: Direct dependency requirements (PEP 508 strings).
        glue_version: Glue version string (e.g. ``"5.1"``); passed to
            :func:`~aws_glue_toolkit.runtime.load_runtime`.
        destination: Final path for the ``.gluewheels.zip`` file.

    Raises:
        GlueWheelsBuildError: ``pip`` resolution or download failed.
        UnsupportedGlueVersionError: No bundled metadata for ``glue_version``.

    """
    runtime = load_runtime(glue_version)
    packages = _resolve_packages_to_bundle(
        requirements,
        runtime.python_packages,
        python_version=runtime.core_engines.python,
        platform=runtime.pip_platform,
    )
    _assemble_gluewheels_zip(
        packages,
        destination,
        python_version=runtime.core_engines.python,
        platform=runtime.pip_platform,
    )
