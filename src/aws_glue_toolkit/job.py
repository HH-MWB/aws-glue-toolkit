"""Glue job ``pyproject.toml``: TOML validation and :class:`GlueJobProject`.

Pydantic models (:class:`PyProject`, etc.) mirror the file layout and validate
input. :func:`load_pyproject` returns :class:`GlueJobProject` — the object
other modules should use (resolved paths, defaults, and runtime metadata).

Raises :class:`PyProjectError` when the file is missing, unreadable, invalid
TOML, or fails validation.

Schema rules (unknown keys ignored):

- ``project.name`` — required
- ``project.version`` — optional; defaults to :data:`DEFAULT_PACKAGE_VERSION`
- ``project.dependencies`` — optional; defaults to ``[]``
- ``tool.aws-glue-toolkit.glue_version`` — optional; defaults to ``"5.1"``;
  must match a bundled ``glue-{version}.json`` release

Example::

    from pathlib import Path

    from aws_glue_toolkit.job import load_pyproject

    job = load_pyproject(Path("./my-glue-job"))

"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # runtime paths in load/read API
from tomllib import TOMLDecodeError, loads
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aws_glue_toolkit.runtime import (
    GlueRuntimeMetadata,
    UnsupportedGlueVersionError,
    load_runtime,
)

__all__ = [
    "DEFAULT_PACKAGE_VERSION",
    "AwsGlueToolkit",
    "GlueJobProject",
    "InvalidPyProjectError",
    "InvalidTomlError",
    "MissingPyProjectError",
    "Project",
    "PyProject",
    "PyProjectError",
    "PyProjectUnreadableError",
    "Tool",
    "load_pyproject",
    "parse_pyproject_text",
    "read_pyproject_text",
]

DEFAULT_PACKAGE_VERSION: Final[str] = "0.0.0"

# --- Resolved job config ---


@dataclass(frozen=True, slots=True)
class GlueJobProject:
    """Resolved Glue job configuration for ``pip``, ``wheels``, and ``cli``.

    Built only by :func:`load_pyproject`. Fields are fully resolved (no
    ``None`` for :attr:`version`).

    Attributes:
        project_dir: Directory containing ``pyproject.toml``.
        name: ``[project].name``.
        version: ``[project].version`` or :data:`DEFAULT_PACKAGE_VERSION`.
        dependencies: ``[project].dependencies`` as an immutable tuple.
        glue_version: ``[tool.aws-glue-toolkit].glue_version``.
        runtime_metadata: Bundled runtime metadata for :attr:`glue_version`.

    """

    project_dir: Path
    name: str
    version: str
    dependencies: tuple[str, ...]
    glue_version: str
    runtime_metadata: GlueRuntimeMetadata

    @property
    def glue_wheels_zip_name(self) -> str:
        """File name ``{name}-{version}.gluewheels.zip``."""
        return f"{self.name}-{self.version}.gluewheels.zip"

    @property
    def glue_wheels_zip_path(self) -> Path:
        """Default artifact path under :attr:`project_dir`."""
        return self.project_dir / self.glue_wheels_zip_name


# --- Pydantic models (TOML schema; not passed to other modules) ---


class Project(BaseModel):
    """``[project]`` table (PEP 621 subset)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    version: str | None = None
    dependencies: list[str] = Field(default_factory=list)


class AwsGlueToolkit(BaseModel):
    """``[tool.aws-glue-toolkit]`` table."""

    model_config = ConfigDict(extra="ignore")

    glue_version: str = "5.1"


class Tool(BaseModel):
    """``[tool]`` table."""

    model_config = ConfigDict(extra="ignore")

    aws_glue_toolkit: AwsGlueToolkit = Field(
        default_factory=AwsGlueToolkit,
        validation_alias="aws-glue-toolkit",
    )


class PyProject(BaseModel):
    """Root model for one ``pyproject.toml`` (validation only)."""

    model_config = ConfigDict(extra="ignore")

    project: Project
    tool: Tool = Field(default_factory=Tool)


# --- Errors ---


class PyProjectError(Exception):
    """Base error for reading or validating a job ``pyproject.toml``."""


class MissingPyProjectError(PyProjectError):
    """No ``pyproject.toml`` at the expected path."""

    def __init__(self, path: Path) -> None:
        """Record the path that was missing."""
        self.path = path
        super().__init__(f"no pyproject.toml at {path}")


class PyProjectUnreadableError(PyProjectError):
    """``pyproject.toml`` exists but could not be read."""


class InvalidTomlError(PyProjectError):
    """File is not valid TOML."""


class InvalidPyProjectError(PyProjectError):
    """Invalid ``pyproject.toml`` (schema or unsupported ``glue_version``)."""


# --- Read, parse, and load ---


def read_pyproject_text(project_dir: Path) -> str:
    """Read ``project_dir/pyproject.toml`` as UTF-8.

    Raises:
        MissingPyProjectError: File not found.
        PyProjectUnreadableError: OS read error.

    """
    path = project_dir / "pyproject.toml"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise MissingPyProjectError(path) from e
    except OSError as e:
        msg = f"cannot read pyproject.toml at {path}: {e}"
        raise PyProjectUnreadableError(msg) from e


def parse_pyproject_text(text: str) -> PyProject:
    """Parse and validate TOML text into :class:`PyProject`.

    Does not load runtime metadata or build :class:`GlueJobProject`.

    Raises:
        InvalidTomlError: TOML syntax error.
        InvalidPyProjectError: Schema validation error.

    """
    try:
        return PyProject.model_validate(loads(text))
    except TOMLDecodeError as e:
        msg = f"invalid TOML: {e}"
        raise InvalidTomlError(msg) from e
    except ValidationError as e:
        msg = f"invalid pyproject: {e}"
        raise InvalidPyProjectError(msg) from e


def load_pyproject(project_dir: Path) -> GlueJobProject:
    """Load ``project_dir/pyproject.toml`` and return resolved job config.

    Loads bundled runtime metadata via
    :func:`~aws_glue_toolkit.runtime.load_runtime`.

    Raises:
        PyProjectError: Missing file, read error, invalid TOML/schema, or
            unsupported ``glue_version``.

    """
    root = project_dir.resolve()

    pyproject = parse_pyproject_text(read_pyproject_text(root))

    glue_version = pyproject.tool.aws_glue_toolkit.glue_version
    try:
        runtime_metadata = load_runtime(glue_version)
    except UnsupportedGlueVersionError as e:
        raise InvalidPyProjectError(str(e)) from e
    return GlueJobProject(
        project_dir=root,
        name=pyproject.project.name,
        version=pyproject.project.version or DEFAULT_PACKAGE_VERSION,
        dependencies=tuple(pyproject.project.dependencies),
        glue_version=glue_version,
        runtime_metadata=runtime_metadata,
    )
