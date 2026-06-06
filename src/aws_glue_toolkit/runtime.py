"""Bundled AWS Glue runtime metadata per release.

JSON under ``aws_glue_toolkit/versions/`` describes engine versions, open
table formats, the ``pip`` wheel platform tag, and preinstalled Python package
pins for each supported Glue version.

:data:`SUPPORTED_GLUE_VERSIONS` lists bundled releases.
:func:`load_glue_runtime_metadata` loads one release.
:class:`~aws_glue_toolkit.job.GlueJobProject` embeds the result when
a job ``pyproject.toml`` is loaded.

Used by :mod:`aws_glue_toolkit.pip` for constraints, platform, and Python
version, and by :mod:`aws_glue_toolkit.wheels` to skip built-in packages.

"""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from json import loads
from typing import Final, Literal

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

SUPPORTED_GLUE_VERSIONS: Final[frozenset[str]] = frozenset(_FILES)


@dataclass(frozen=True, slots=True, kw_only=True)
class GlueCoreEngines:
    """Spark, Python, and Scala versions for one Glue release."""

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
    """Runtime snapshot for one Glue release.

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


def load_glue_runtime_metadata(
    glue_version: Literal[  # type: ignore[valid-type]
        *SUPPORTED_GLUE_VERSIONS,
    ],
) -> GlueRuntimeMetadata:
    """Load bundled metadata for one supported Glue release.

    Args:
        glue_version: Must be a key in :data:`SUPPORTED_GLUE_VERSIONS`.

    Returns:
        Frozen metadata from ``aws_glue_toolkit/versions/glue_*.json``.

    Raises:
        KeyError: Unknown ``glue_version``.

    """
    resource = _FILES[glue_version]
    raw = resource.read_text(encoding="utf-8")
    payload = loads(raw)
    return GlueRuntimeMetadata(
        glue_version=glue_version,
        core_engines=GlueCoreEngines(**payload["core_engines"]),
        table_formats=GlueTableFormats(**payload["table_formats"]),
        pip_platform=payload["pip_platform"],
        python_packages=payload["python_packages"],
    )
