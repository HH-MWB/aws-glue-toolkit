"""Build zip artifacts produced by ``gtk build`` for AWS Glue job deployment.

Two artifacts, two Glue job parameters:

- ``{name}-{version}.dependencies.zip`` — for ``--extra-py-files``. Zips
  every ``.py`` file under the job ``source`` tree except the entry
  ``script``, preserving paths relative to ``source``.

- ``{name}-{version}.gluewheels.zip`` — for ``--additional-python-modules``
  (Glue 5.0+). Resolves job dependencies against bundled runtime pins,
  downloads wheels, and bundles them per `AWS Glue Appendix A
  <https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html>`_::

      wheels/
        requirements.txt   # resolved ``name==version`` pins
        *.whl

Public API: :func:`build_dependencies_zip`, :func:`build_gluewheels_zip`.

Both builders always write ``destination``, even when there is nothing to
bundle.

"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from zipfile import ZIP_DEFLATED, ZipFile

from aws_glue_toolkit.pip import (
    PipExecutionContext,
    download_wheels,
    resolve_packages,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from aws_glue_toolkit.runtime import GlueRuntimeMetadata

__all__ = [
    "build_dependencies_zip",
    "build_gluewheels_zip",
]

# --- Dependencies zip ---


def build_dependencies_zip(
    source_dir: Path,
    entry_script: Path,
    destination: Path,
) -> None:
    """Create a dependencies zip at ``destination``.

    Zips every ``.py`` file under ``source_dir`` except ``entry_script``,
    preserving paths relative to ``source_dir``. Always writes
    ``destination`` (the zip may be empty).

    Args:
        source_dir: Job Python source root.
        entry_script: Resolved entry script path under ``source_dir``.
        destination: Final path for the zip file (typically
            ``{name}-{version}.dependencies.zip``).

    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for path in source_dir.rglob("*.py"):
            if path == entry_script:
                continue
            archive.write(path, arcname=str(path.relative_to(source_dir)))


# --- Gluewheels zip ---


@contextmanager
def _gluewheels_staging(destination: Path) -> Iterator[Path]:
    """Stage ``wheels/`` in a temp directory; zip the tree on context exit.

    Creates ``<temp>/wheels/`` for population during assembly. After the
    ``with`` block, zips every file under the temp root (arcnames relative to
    that root) and writes ``destination``.

    Args:
        destination: Final path for the zip file (typically
            ``{name}-{version}.gluewheels.zip``).

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


def build_gluewheels_zip(
    requirements: Sequence[str],
    runtime: GlueRuntimeMetadata,
    destination: Path,
    *,
    execution: PipExecutionContext | None = None,
) -> None:
    """Create a gluewheels zip at ``destination``.

    1. **Resolve packages to bundle** — resolve ``requirements`` with Glue
       runtime pins as constraints; omit packages whose resolved version
       matches a built-in pin.
    2. **Assemble gluewheels zip** — write ``wheels/requirements.txt``,
       download ``*.whl`` files, and zip the staging tree to
       ``destination``.

    Always writes ``destination`` (the zip may be empty).

    Args:
        requirements: Direct dependency requirements (PEP 508 strings).
        runtime: Bundled Glue runtime metadata (constraints, Python, platform).
        destination: Final path for the zip file (typically
            ``{name}-{version}.gluewheels.zip``).
        execution: When set, run pip in the Glue Docker image.

    Raises:
        :exc:`~aws_glue_toolkit.pip.PipError`: ``pip`` resolution or
        download failed.

    """
    # Resolve packages to bundle.
    resolved = resolve_packages(
        requirements,
        runtime.python_packages,
        python_version=runtime.core_engines.python,
        platform=runtime.pip_platform,
        execution=execution,
    )
    packages = {
        name: version
        for name, version in resolved.items()
        if runtime.python_packages.get(name) != version
    }

    # Assemble gluewheels zip.
    with _gluewheels_staging(destination) as wheels_dir:
        (wheels_dir / "requirements.txt").write_text(
            "\n".join(
                f"{name}=={version}"
                for name, version in sorted(packages.items())
            ),
            encoding="utf-8",
        )
        download_wheels(
            packages,
            wheels_dir,
            python_version=runtime.core_engines.python,
            platform=runtime.pip_platform,
            execution=execution,
        )
