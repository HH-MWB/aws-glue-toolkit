"""Bundled AWS Glue runtime metadata.

Loads version-specific JSON from ``aws_glue_toolkit/versions/`` and exposes
frozen records for each supported Glue release: engine versions (Spark, Python,
Scala), open table formats, and preinstalled Python package pins.

:data:`SUPPORTED_GLUE_VERSIONS` and :func:`load_glue_runtime_metadata` are the
single source of truth for what ships in a Glue job environment. Used by
:mod:`aws_glue_toolkit.dependencies` as compile constraints and as the
built-in package list when filtering wheel artifacts.

"""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from json import loads
from typing import Final

__all__ = [
    "SUPPORTED_GLUE_VERSIONS",
    "GlueCoreEngines",
    "GlueRuntimeMetadata",
    "GlueTableFormats",
    "load_glue_runtime_metadata",
]

_VERSIONS: Final[Traversable] = files("aws_glue_toolkit").joinpath(
    "versions",
)
_FILES: Final[dict[str, Traversable]] = {
    "5.0": _VERSIONS.joinpath("glue_5_0.json"),
    "5.1": _VERSIONS.joinpath("glue_5_1.json"),
}

# Keys match ``_FILES`` exactly (bundled JSON per Glue API version).
SUPPORTED_GLUE_VERSIONS: Final[frozenset[str]] = frozenset(_FILES)


@dataclass(frozen=True, slots=True, kw_only=True)
class GlueCoreEngines:
    """Spark, Python, and Scala runtime versions."""

    spark: str
    python: str
    scala: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GlueTableFormats:
    """Open table format library versions."""

    hudi: str
    iceberg: str
    delta_lake: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GlueRuntimeMetadata:
    """Versions for one AWS Glue release."""

    glue_version: str
    core_engines: GlueCoreEngines
    table_formats: GlueTableFormats
    python_packages: Mapping[str, str]


def load_glue_runtime_metadata(glue_version: str) -> GlueRuntimeMetadata:
    """Load AWS Glue runtime metadata for one supported release.

    Packaged JSON under ``aws_glue_toolkit/versions/`` (one file per version).
    Each file must expose ``core_engines``, ``table_formats``, and
    ``python_packages`` for ``GlueRuntimeMetadata``.

    Steps:
        1. Resolve the bundled file for ``glue_version`` (map lookup).
        2. Read UTF-8 and parse with ``loads``.
        3. Construct ``GlueRuntimeMetadata`` and nested dataclasses.

    Args:
        glue_version: Glue ``GlueVersion`` string (same value as job
            configuration). Must be a key in ``_FILES``; callers that build
            from :class:`~aws_glue_toolkit.pyproject.PyProject` already
            enforce this via
            :data:`~aws_glue_toolkit.runtime.SUPPORTED_GLUE_VERSIONS`.

    Returns:
        Frozen snapshot of engine, table-format, and Python package versions.

    Raises:
        KeyError: ``glue_version`` is not a key in ``_FILES``.

    """
    resource = _FILES[glue_version]
    raw = resource.read_text(encoding="utf-8")
    payload = loads(raw)
    return GlueRuntimeMetadata(
        glue_version=glue_version,
        core_engines=GlueCoreEngines(**payload["core_engines"]),
        table_formats=GlueTableFormats(**payload["table_formats"]),
        python_packages=payload["python_packages"],
    )
