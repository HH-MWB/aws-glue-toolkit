"""Glue job dependency resolution.

Resolves ``pyproject.toml`` dependencies against bundled Glue runtime pins via
``uv pip compile``, producing pinned package versions and ``requirements.txt``
content suitable for ``gtk check`` or for packaging into a
``.gluewheels.zip`` artifact.

Public entry point: :func:`resolve_dependencies`, which returns
:class:`ResolvedDependencies`. Set ``exclude_builtins=True`` when resolving
for a wheel build so packages already preinstalled in the Glue runtime (at the
same pinned version) are omitted.

Raises :class:`DependencyConflictError` when requirements are unsatisfiable
with bundled pins.

Glue jobs target x86_64 manylinux2014 only; resolution uses that platform
internally for ``uv pip compile``.

Note:
    ``# nosec B404`` marks the ``subprocess`` import as reviewed (Bandit flags
    the module wholesale). Usage is only in :func:`_uv_pip_compile`.

"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec B404
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final, NamedTuple

from aws_glue_toolkit.runtime import load_glue_runtime_metadata

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from aws_glue_toolkit.pyproject import PyProject

__all__ = [
    "DependencyConflictError",
    "ResolvedDependencies",
    "resolve_dependencies",
]

# --- Constants and types ---

_UV_PYTHON_PLATFORM: Final[str] = "x86_64-manylinux2014"


class DependencyConflictError(Exception):
    """Requirements are unsatisfiable with bundled Glue pins."""


@dataclass(frozen=True, slots=True)
class ResolvedDependencies:
    """Pinned packages and metadata for wheel packaging.

    Attributes:
        packages: Package name to pinned version.
        requirements_txt: ``requirements.txt`` body (may be empty).
        python_version: Glue runtime Python (e.g. ``"3.11"``).

    """

    packages: dict[str, str]
    requirements_txt: str
    python_version: str


class UvCompileWorkspacePaths(NamedTuple):
    """Paths for ``uv pip compile`` under one temp directory."""

    requirements_in_path: Path
    constraints_txt_path: Path
    requirements_txt_path: Path


# --- Pure requirements helpers ---


def _parse_requirements_txt(requirements_txt: str) -> dict[str, str]:
    return dict(
        line.partition("==")[::2]
        for line in requirements_txt.splitlines()
        if "==" in line
    )


def _format_constraints_txt(python_packages: Mapping[str, str]) -> str:
    return "\n".join(
        f"{name}=={version}" for name, version in python_packages.items()
    )


def _filter_non_builtin(
    requirements_txt: str,
    python_packages: Mapping[str, str],
) -> str:
    """Drop lines already provided by the Glue runtime at the same version."""
    lines: list[str] = []
    for line in requirements_txt.splitlines():
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        if python_packages.get(name) == version:
            continue
        lines.append(line)
    return "\n".join(lines)


# --- uv subprocess ---


@contextmanager
def _uv_compile_workspace(
    requirements_in: str,
    constraints_txt: str,
) -> Iterator[UvCompileWorkspacePaths]:
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        requirements_in_path = work / "requirements.in"
        constraints_txt_path = work / "constraints.txt"
        requirements_txt_path = work / "requirements.txt"
        requirements_in_path.write_text(requirements_in, encoding="utf-8")
        constraints_txt_path.write_text(constraints_txt, encoding="utf-8")
        yield UvCompileWorkspacePaths(
            requirements_in_path,
            constraints_txt_path,
            requirements_txt_path,
        )


def _uv_pip_compile(
    workspace: UvCompileWorkspacePaths,
    *,
    uv_exe: str,
    python_version: str,
) -> None:
    cmd = [
        uv_exe,
        "pip",
        "compile",
        str(workspace.requirements_in_path),
        "-c",
        str(workspace.constraints_txt_path),
        "--python-version",
        python_version,
        "--python-platform",
        _UV_PYTHON_PLATFORM,
        "--no-header",
        "--no-annotate",
        "-o",
        str(workspace.requirements_txt_path),
    ]
    try:
        run(  # noqa: S603
            cmd,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )  # nosec B603
    except CalledProcessError as e:
        msg = (e.stderr or e.stdout).strip()
        raise DependencyConflictError(msg) from None


def _compile_requirements_txt(
    pyproject: PyProject,
    *,
    uv_exe: str,
) -> tuple[str, str]:
    """Return compiled ``(requirements_txt, python_version)``."""
    metadata = load_glue_runtime_metadata(
        pyproject.tool.aws_glue_toolkit.glue_version,
    )
    with _uv_compile_workspace(
        requirements_in="\n".join(pyproject.project.dependencies),
        constraints_txt=_format_constraints_txt(metadata.python_packages),
    ) as workspace:
        _uv_pip_compile(
            workspace,
            uv_exe=uv_exe,
            python_version=metadata.core_engines.python,
        )
        text = workspace.requirements_txt_path.read_text(encoding="utf-8")
    return text, metadata.core_engines.python


# --- Public resolve API ---


def resolve_dependencies(
    pyproject: PyProject,
    *,
    uv_exe: str,
    exclude_builtins: bool = False,
) -> ResolvedDependencies:
    """Resolve job dependencies including transitives for the Glue version.

    Args:
        pyproject: Validated Glue job ``pyproject.toml``.
        uv_exe: Path to the ``uv`` executable.
        exclude_builtins: When ``True``, omit packages already preinstalled
            in the Glue runtime at the same pinned version (for wheel builds).

    Returns:
        Pinned packages and ``requirements.txt`` content.

    Raises:
        DependencyConflictError: Requirements are unsatisfiable with Glue pins.

    """
    metadata = load_glue_runtime_metadata(
        pyproject.tool.aws_glue_toolkit.glue_version,
    )
    python_version = metadata.core_engines.python
    if not pyproject.project.dependencies:
        return ResolvedDependencies(
            packages={},
            requirements_txt="",
            python_version=python_version,
        )

    requirements_txt, python_version = _compile_requirements_txt(
        pyproject,
        uv_exe=uv_exe,
    )
    if exclude_builtins:
        requirements_txt = _filter_non_builtin(
            requirements_txt,
            metadata.python_packages,
        )
    return ResolvedDependencies(
        packages=_parse_requirements_txt(requirements_txt),
        requirements_txt=requirements_txt,
        python_version=python_version,
    )
