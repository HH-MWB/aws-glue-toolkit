"""Pydantic models for a Glue job ``pyproject.toml``.

Example:
    from pathlib import Path
    from tomllib import loads

    from aws_glue_toolkit.glue_pyproject import PyProject

    data = loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    root = PyProject.model_validate(data)

Unknown keys are ignored. ``project.name`` / ``project.version`` default to
``None``; ``project.dependencies`` to ``[]``. ``tool.aws_glue_toolkit`` is
always set; missing ``glue_version`` defaults to ``5.1``. ``glue_version``
must match a bundled release (see
:data:`~aws_glue_toolkit.glue_runtime.SUPPORTED_GLUE_VERSIONS`).

"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aws_glue_toolkit.glue_runtime import SUPPORTED_GLUE_VERSIONS

__all__ = [
    "AwsGlueToolkit",
    "Project",
    "PyProject",
    "Tool",
]


class Project(BaseModel):
    """PEP 621 ``[project]`` fields used by the toolkit.

    Example:
        [project]
        name = "my-job"
        version = "0.1.0"
        dependencies = ["pandas"]

    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    version: str | None = None
    dependencies: list[str] = Field(default_factory=list)


class AwsGlueToolkit(BaseModel):
    """Settings under ``[tool.aws-glue-toolkit]``.

    ``glue_version`` must be a key in bundled runtime metadata (same set as
    :data:`~aws_glue_toolkit.glue_runtime.SUPPORTED_GLUE_VERSIONS`).

    Example:
        [tool.aws-glue-toolkit]
        glue_version = "5.0"

    """

    model_config = ConfigDict(extra="ignore")

    glue_version: Literal[  # type: ignore[valid-type]
        *SUPPORTED_GLUE_VERSIONS,
    ] = "5.1"


class Tool(BaseModel):
    """PEP 518 ``[tool]`` table.

    TOML key ``aws-glue-toolkit`` maps to ``aws_glue_toolkit``.

    Example:
        [tool.aws-glue-toolkit]
        glue_version = "5.0"

    """

    model_config = ConfigDict(extra="ignore")

    aws_glue_toolkit: AwsGlueToolkit = Field(
        default_factory=AwsGlueToolkit,
        validation_alias="aws-glue-toolkit",
    )


class PyProject(BaseModel):
    """Root of ``pyproject.toml`` (``project`` and ``tool``).

    Example:
        [project]
        name = "my-job"
        version = "0.1.0"
        dependencies = ["pandas"]

        [tool.aws-glue-toolkit]
        glue_version = "5.0"

    """

    model_config = ConfigDict(extra="ignore")

    project: Project = Field(default_factory=Project)
    tool: Tool = Field(default_factory=Tool)
