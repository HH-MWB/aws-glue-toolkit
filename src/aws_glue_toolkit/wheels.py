"""Build AWS Glue ``.gluewheels.zip`` wheel archives.

For Glue 5.0+ ``--additional-python-modules``, packages extra Python libraries
into a zip artifact per `AWS Glue Appendix A
<https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html>`_:

::

    wheels/
      requirements.txt   # resolved ``name==version`` pins
      *.whl

Pipeline (see :func:`build_gluewheels_zip`):

1. :class:`~aws_glue_toolkit.runtime.GlueRuntimeMetadata` supplies
   constraints, Python, and platform.
2. Resolve packages to bundle.
3. Assemble gluewheels zip.

Public API: :func:`build_gluewheels_zip`.

Always writes ``destination``, including when there are zero wheels to bundle.

"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from zipfile import ZIP_DEFLATED, ZipFile

from aws_glue_toolkit.pip import download_wheels, resolve_packages

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from aws_glue_toolkit.runtime import GlueRuntimeMetadata

__all__ = [
    "build_gluewheels_zip",
]


@contextmanager
def _gluewheels_staging(destination: Path) -> Iterator[Path]:
    """Stage ``wheels/`` in a temp directory; zip the tree on context exit.

    Creates ``<temp>/wheels/`` for population during assembly. After the
    ``with`` block, zips every file under the temp root (arcnames relative to
    that root) and writes ``destination``.

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


def build_gluewheels_zip(
    requirements: Sequence[str],
    runtime: GlueRuntimeMetadata,
    destination: Path,
) -> None:
    """Create a ``.gluewheels.zip`` at ``destination``.

    1. **Resolve packages to bundle** — resolve ``requirements`` with Glue
       runtime pins as constraints; omit packages whose resolved version
       matches a built-in pin.
    2. **Assemble gluewheels zip** — write ``wheels/requirements.txt``,
       download ``*.whl`` files, and zip the staging tree to
       ``destination``.

    Args:
        requirements: Direct dependency requirements (PEP 508 strings).
        runtime: Bundled Glue runtime metadata (constraints, Python, platform).
        destination: Final path for the ``.gluewheels.zip`` file.

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
        )
