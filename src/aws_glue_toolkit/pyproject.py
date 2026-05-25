"""Glue job ``pyproject.toml`` schema, I/O, and validation.

Owns the Pydantic models for a Glue job ``pyproject.toml`` and the functions
that locate, read, and validate that file. Other modules consume a validated
:class:`PyProject` from :func:`load_pyproject` rather than parsing TOML
themselves.

Raises :class:`PyProjectError` subclasses on failure (missing file, unreadable
path, invalid TOML, or schema validation). The CLI maps these to process exit
codes.

Unknown keys are ignored. ``project.name`` / ``project.version`` default to
``None``; ``project.dependencies`` to ``[]``. ``tool.aws_glue_toolkit`` is
always set; missing ``glue_version`` defaults to ``5.1``. ``glue_version``
must match a bundled release (see
:data:`~aws_glue_toolkit.runtime.SUPPORTED_GLUE_VERSIONS`).

Example:
    from pathlib import Path

    from aws_glue_toolkit.pyproject import load_pyproject

    project = load_pyproject(Path("./my-glue-job"))

"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003  # runtime paths in load/read API
from tomllib import TOMLDecodeError, loads
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aws_glue_toolkit.runtime import (  # noqa: TC001
    SUPPORTED_GLUE_VERSIONS,
)

__all__ = [
    "AwsGlueToolkit",
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

# --- Pydantic models ---


class Project(BaseModel):
    """PEP 621 ``[project]`` fields used by the toolkit."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    version: str | None = None
    dependencies: list[str] = Field(default_factory=list)


class AwsGlueToolkit(BaseModel):
    """Settings under ``[tool.aws-glue-toolkit]``."""

    model_config = ConfigDict(extra="ignore")

    glue_version: Literal[  # type: ignore[valid-type]
        *SUPPORTED_GLUE_VERSIONS,
    ] = "5.1"


class Tool(BaseModel):
    """PEP 518 ``[tool]`` table."""

    model_config = ConfigDict(extra="ignore")

    aws_glue_toolkit: AwsGlueToolkit = Field(
        default_factory=AwsGlueToolkit,
        validation_alias="aws-glue-toolkit",
    )


class PyProject(BaseModel):
    """Validated root of a Glue job ``pyproject.toml``."""

    model_config = ConfigDict(extra="ignore")

    project: Project = Field(default_factory=Project)
    tool: Tool = Field(default_factory=Tool)


# --- Errors ---


class PyProjectError(Exception):
    """Failed to read or validate ``pyproject.toml``."""


class MissingPyProjectError(PyProjectError):
    """``pyproject.toml`` does not exist."""

    def __init__(self, path: Path) -> None:
        """Store the expected ``pyproject.toml`` path."""
        self.path = path
        super().__init__(f"no pyproject.toml at {path}")


class PyProjectUnreadableError(PyProjectError):
    """``pyproject.toml`` exists but cannot be read."""


class InvalidTomlError(PyProjectError):
    """``pyproject.toml`` is not valid TOML."""


class InvalidPyProjectError(PyProjectError):
    """``pyproject.toml`` fails schema validation."""


# --- Read, parse, and load API ---


def read_pyproject_text(project_dir: Path) -> str:
    """Read ``project_dir/pyproject.toml`` as UTF-8 text.

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
    """Parse TOML text into :class:`PyProject`.

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


def load_pyproject(project_dir: Path) -> PyProject:
    """Read and validate ``project_dir/pyproject.toml``.

    Returns:
        Validated :class:`PyProject` safe for other modules to use.

    Raises:
        PyProjectError: Missing file, read error, or invalid contents.

    """
    return parse_pyproject_text(read_pyproject_text(project_dir))
