"""Bundled AWS Glue runtime metadata per release.

Each supported Glue version has a JSON file shipped in the wheel at
``aws_glue_toolkit/versions/glue-{version}.json`` (for example
``glue-5.1.json`` for version ``"5.1"``). The file records:

- ``glue_version`` — Glue release string
- ``core_engines`` — Spark, Python, and Scala versions on the worker image
- ``table_formats`` — Hudi, Iceberg, and Delta Lake library versions
- ``pip_platform`` — ``pip --platform`` tag for manylinux wheels
- ``python_packages`` — preinstalled package name → version pins

Bundled JSON is validated before release.
:func:`load_runtime` is the public entry point; it raises
:exc:`UnsupportedGlueVersionError` when a version file is missing.
:mod:`aws_glue_toolkit.job` calls it when loading ``pyproject.toml`` into
:attr:`~aws_glue_toolkit.job.GlueJobProject.runtime`.
:mod:`aws_glue_toolkit.cli` and :mod:`aws_glue_toolkit.wheels` consume
:class:`GlueRuntimeMetadata` (or one loaded directly via
:func:`load_runtime`).

:mod:`aws_glue_toolkit.pip` uses the pins as constraints;
:mod:`aws_glue_toolkit.wheels` uses them to omit packages already on the
Glue image.

Example::

    from aws_glue_toolkit.runtime import load_runtime

    runtime = load_runtime("5.1")
    runtime.core_engines.python
    runtime.python_packages["pandas"]

"""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from json import loads

__all__ = [
    "GlueCoreEngines",
    "GlueRuntimeMetadata",
    "GlueTableFormats",
    "UnsupportedGlueVersionError",
    "load_runtime",
]

# --- Errors ---


class UnsupportedGlueVersionError(Exception):
    """Raised when no bundled ``glue-{version}.json`` exists.

    Attributes:
        glue_version: The version string that was requested.

    """

    glue_version: str

    def __init__(self, glue_version: str) -> None:
        """Store the unsupported ``glue_version``."""
        self.glue_version = glue_version
        super().__init__(f"unsupported glue version: {glue_version!r}")


# --- Resolved metadata ---


@dataclass(frozen=True, slots=True, kw_only=True)
class GlueCoreEngines:
    """Spark, Python, and Scala versions for one Glue release."""

    spark: str
    python: str
    scala: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GlueTableFormats:
    """Open table format library versions on the Glue worker image."""

    hudi: str
    iceberg: str
    delta_lake: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GlueRuntimeMetadata:
    """Runtime snapshot for one Glue release.

    Returned by :func:`load_runtime`.

    Attributes:
        glue_version: Glue version string (e.g. ``"5.1"``).
        core_engines: Spark, Python, and Scala versions.
        table_formats: Hudi, Iceberg, and Delta Lake versions.
        pip_platform: ``pip --platform`` tag for manylinux wheels on Glue
            workers.
        python_packages: Preinstalled package name → version.

    """

    glue_version: str
    core_engines: GlueCoreEngines
    table_formats: GlueTableFormats
    pip_platform: str
    python_packages: Mapping[str, str]


# --- Load ---


def load_runtime(glue_version: str) -> GlueRuntimeMetadata:
    """Load bundled metadata for one Glue release.

    Looks up ``aws_glue_toolkit/versions/glue-{glue_version}.json`` in the
    installed package and returns a frozen :class:`GlueRuntimeMetadata`.

    Args:
        glue_version: Glue version string (e.g. ``"5.1"``).

    Returns:
        Resolved runtime metadata for ``glue_version``.

    Raises:
        UnsupportedGlueVersionError: No bundled file for ``glue_version``.

    """
    # Locate bundled JSON for this Glue version.
    resource = files("aws_glue_toolkit").joinpath(
        "versions",
        f"glue-{glue_version}.json",
    )
    if not resource.is_file():
        raise UnsupportedGlueVersionError(glue_version)

    # Parse JSON and return frozen metadata.
    data = loads(resource.read_text(encoding="utf-8"))
    return GlueRuntimeMetadata(
        glue_version=data["glue_version"],
        core_engines=GlueCoreEngines(
            spark=data["core_engines"]["spark"],
            python=data["core_engines"]["python"],
            scala=data["core_engines"]["scala"],
        ),
        table_formats=GlueTableFormats(
            hudi=data["table_formats"]["hudi"],
            iceberg=data["table_formats"]["iceberg"],
            delta_lake=data["table_formats"]["delta_lake"],
        ),
        pip_platform=data["pip_platform"],
        python_packages=data["python_packages"],
    )
