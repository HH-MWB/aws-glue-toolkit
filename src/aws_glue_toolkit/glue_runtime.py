"""AWS Glue ETL runtime version pins for supported ``GlueVersion`` values.

This module exposes frozen records describing what ships inside a Glue Spark
job for a given API version string (e.g. Spark, Python, Scala, Hudi, Iceberg,
Delta Lake, and preinstalled Python wheels). Values are read from JSON files
bundled under ``aws_glue_toolkit/versions/``—one file per supported version,
selected by the internal ``_FILES`` map.

"""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from json import loads
from typing import Final

__all__ = [
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
            configuration). Must match a key in ``_FILES`` exactly (no
            stripping or aliases).

    Returns:
        Frozen snapshot of engine, table-format, and Python package versions.

    Raises:
        ValueError: ``glue_version`` is not in the supported set.

    """
    # 1. Resolve packaged file (unknown ``glue_version`` → ``KeyError``).
    try:
        resource = _FILES[glue_version]
    except KeyError:
        msg = f"unsupported Glue version {glue_version!r}"
        raise ValueError(msg) from None

    # 2. Read UTF-8, parse JSON (schema matches versions/*.json).
    raw = resource.read_text(encoding="utf-8")
    payload = loads(raw)

    # 3. Build frozen dataclasses from payload (versions/*.json schema).
    return GlueRuntimeMetadata(
        glue_version=glue_version,
        core_engines=GlueCoreEngines(**payload["core_engines"]),
        table_formats=GlueTableFormats(**payload["table_formats"]),
        python_packages=payload["python_packages"],
    )
