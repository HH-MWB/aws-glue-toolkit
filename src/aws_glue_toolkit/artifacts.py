"""Build zip artifacts produced by ``gtk build`` for AWS Glue job deployment.

Two artifacts, two Glue job parameters:

- ``{name}-{version}.dependencies.zip`` — for ``--extra-py-files``. Zips
  every ``.py`` file under the job ``source`` tree except the entry
  ``script``, preserving paths relative to ``source``.

- ``{name}-{version}.gluewheels.zip`` — for ``--additional-python-modules``
  (Glue 5.0+). Bundles a ``wheels/`` tree per `AWS Glue Appendix A
  <https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html>`_::

      wheels/
        requirements.txt   # resolved ``name==version`` pins
        *.whl

Public API: :func:`build_dependencies_zip`, :func:`stage_gluewheels_zip`,
:func:`write_gluewheels_tree`.

Orchestration (prepare requirements, bundle wheels, zip) lives in
:mod:`aws_glue_toolkit.workflows`.

"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from zipfile import ZIP_DEFLATED, ZipFile

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = [
    "build_dependencies_zip",
    "stage_gluewheels_zip",
    "write_gluewheels_tree",
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
def stage_gluewheels_zip(destination: Path) -> Iterator[Path]:
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


def write_gluewheels_tree(
    wheels_dir: Path,
    packages: Mapping[str, str],
) -> None:
    """Write ``wheels/requirements.txt`` from resolved package pins."""
    (wheels_dir / "requirements.txt").write_text(
        "\n".join(
            f"{name}=={version}" for name, version in sorted(packages.items())
        ),
        encoding="utf-8",
    )
