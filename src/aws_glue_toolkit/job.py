"""Glue job ``pyproject.toml``: TOML validation and :class:`GlueJobProject`.

Private Pydantic models mirror the file layout and validate input.
:func:`load_pyproject` reads ``project_dir/pyproject.toml``, resolves bundled
Glue runtime metadata for ``tool.aws-glue-toolkit.glue_version``, and returns
:class:`GlueJobProject` with defaults applied.

Schema rules (unknown keys ignored):

- ``project.name`` — required
- ``project.version`` — optional; defaults to :data:`DEFAULT_PACKAGE_VERSION`
- ``project.dependencies`` — optional; defaults to ``[]``
- ``tool.aws-glue-toolkit.glue_version`` — required; resolved via
  :func:`~aws_glue_toolkit.runtime.load_runtime`

Example::

    from pathlib import Path

    from aws_glue_toolkit.job import load_pyproject

    job = load_pyproject(Path("./my-glue-job"))
    job.runtime.glue_version

"""

from __future__ import annotations

from dataclasses import dataclass
from tomllib import loads
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from aws_glue_toolkit.runtime import GlueRuntimeMetadata, load_runtime

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "DEFAULT_PACKAGE_VERSION",
    "GlueJobProject",
    "load_pyproject",
]

DEFAULT_PACKAGE_VERSION: Final[str] = "0.0.0"

# --- Resolved job config ---


@dataclass(frozen=True, slots=True)
class GlueJobProject:
    """Resolved Glue job configuration for ``pip``, ``wheels``, and ``cli``.

    Built only by :func:`load_pyproject`.
    Fields are fully resolved (no ``None`` for :attr:`version`).

    Attributes:
        project_dir: Directory containing ``pyproject.toml``.
        name: ``[project].name``.
        version: ``[project].version`` (defaults to
            :data:`DEFAULT_PACKAGE_VERSION`).
        dependencies: ``[project].dependencies`` as an immutable tuple.
        runtime: Bundled Glue runtime metadata for
            ``[tool.aws-glue-toolkit].glue_version``.

    """

    project_dir: Path
    name: str
    version: str
    dependencies: tuple[str, ...]
    runtime: GlueRuntimeMetadata

    @property
    def gluewheels_zip_filename(self) -> str:
        """File name ``{name}-{version}.gluewheels.zip``."""
        return f"{self.name}-{self.version}.gluewheels.zip"


# --- Pydantic models (TOML schema; private) ---


class _Project(BaseModel):
    """``[project]`` table (PEP 621 subset)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    version: str = DEFAULT_PACKAGE_VERSION
    dependencies: list[str] = Field(default_factory=list)


class _AwsGlueToolkit(BaseModel):
    """``[tool.aws-glue-toolkit]`` table."""

    model_config = ConfigDict(extra="ignore")

    glue_version: str


class _Tool(BaseModel):
    """``[tool]`` table."""

    model_config = ConfigDict(extra="ignore")

    aws_glue_toolkit: _AwsGlueToolkit = Field(
        validation_alias="aws-glue-toolkit",
    )


class _PyProject(BaseModel):
    """Root model for one ``pyproject.toml`` (validation only)."""

    model_config = ConfigDict(extra="ignore")

    project: _Project
    tool: _Tool


# --- Load ---


def load_pyproject(project_dir: Path) -> GlueJobProject:
    """Load ``project_dir/pyproject.toml`` and return job config.

    Raises:
        FileNotFoundError: No ``pyproject.toml`` in ``project_dir``.
        OSError: ``pyproject.toml`` exists but could not be read.
        TOMLDecodeError: TOML syntax error.
        ValidationError: ``pyproject.toml`` failed schema validation.
        UnsupportedGlueVersionError: No bundled metadata for
            ``tool.aws-glue-toolkit.glue_version``.

    """
    pyproject = _PyProject.model_validate(
        loads((project_dir / "pyproject.toml").read_text(encoding="utf-8")),
    )
    return GlueJobProject(
        project_dir=project_dir,
        name=pyproject.project.name,
        version=pyproject.project.version,
        dependencies=tuple(pyproject.project.dependencies),
        runtime=load_runtime(pyproject.tool.aws_glue_toolkit.glue_version),
    )
