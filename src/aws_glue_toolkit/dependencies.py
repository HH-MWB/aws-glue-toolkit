"""Resolve Glue job dependencies against bundled runtime pins via ``uv``.

Uses :class:`~aws_glue_toolkit.pyproject.GlueJobProject` (from
:func:`~aws_glue_toolkit.pyproject.load_pyproject`) and
:class:`~aws_glue_toolkit.runtime.GlueRuntimeMetadata` as constraints.

Public API: :func:`resolve_dependencies` → :class:`ResolvedDependencies`.

For wheel builds, pass ``exclude_builtins=True`` to drop packages already
installed in the Glue runtime at the same pinned version.

Raises :class:`DependencyConflictError` when requirements cannot be satisfied.

Resolution targets x86_64 manylinux2014 (``uv pip compile --python-platform``).

"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError  # nosec B404
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final, NamedTuple

from aws_glue_toolkit.uv import uv

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from aws_glue_toolkit.pyproject import GlueJobProject

__all__ = [
    "DependencyConflictError",
    "ResolvedDependencies",
    "resolve_dependencies",
]

# Glue worker platform for ``uv pip compile`` (x86_64 manylinux2014).
_UV_PYTHON_PLATFORM: Final[str] = "x86_64-manylinux2014"


class DependencyConflictError(Exception):
    """``uv pip compile`` could not satisfy requirements with Glue pins."""


@dataclass(frozen=True, slots=True)
class ResolvedDependencies:
    """Pinned dependencies for ``gtk check`` or :mod:`aws_glue_toolkit.wheels`.

    Attributes:
        packages: Package name → pinned version.
        requirements_txt: ``requirements.txt`` body (may be empty).
        python_version: Glue runtime Python (e.g. ``"3.11"``).

    """

    packages: dict[str, str]
    requirements_txt: str
    python_version: str


class UvCompileWorkspacePaths(NamedTuple):
    """Temp paths for one ``uv pip compile`` invocation."""

    requirements_in_path: Path
    constraints_txt_path: Path
    requirements_txt_path: Path


def _parse_requirements_txt(requirements_txt: str) -> dict[str, str]:
    """Parse ``name==version`` lines into a dict."""
    return dict(
        line.partition("==")[::2]
        for line in requirements_txt.splitlines()
        if "==" in line
    )


def _format_constraints_txt(python_packages: Mapping[str, str]) -> str:
    """Format bundled runtime pins as a ``constraints.txt`` body."""
    return "\n".join(
        f"{name}=={version}" for name, version in python_packages.items()
    )


def _filter_non_builtin(
    requirements_txt: str,
    python_packages: Mapping[str, str],
) -> str:
    """Omit packages already on the Glue image at the same version."""
    lines: list[str] = []
    for line in requirements_txt.splitlines():
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        if python_packages.get(name) == version:
            continue
        lines.append(line)
    return "\n".join(lines)


@contextmanager
def _uv_compile_workspace(
    requirements_in: str,
    constraints_txt: str,
) -> Iterator[UvCompileWorkspacePaths]:
    """Write compile inputs to a temp dir and yield their paths."""
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


def _pip_compile(
    workspace: UvCompileWorkspacePaths,
    *,
    python_version: str,
) -> None:
    """Run ``uv pip compile``; write ``requirements.txt`` to the workspace."""
    try:
        uv(
            [
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
            ],
        )
    except CalledProcessError as e:
        msg = (e.stderr or e.stdout).strip()
        raise DependencyConflictError(msg) from None


def _compile_requirements_txt(job: GlueJobProject) -> tuple[str, str]:
    """Compile job deps with Glue pins; return requirements text and Python."""
    metadata = job.runtime_metadata
    with _uv_compile_workspace(
        requirements_in="\n".join(job.dependencies),
        constraints_txt=_format_constraints_txt(metadata.python_packages),
    ) as workspace:
        _pip_compile(
            workspace,
            python_version=metadata.core_engines.python,
        )
        text = workspace.requirements_txt_path.read_text(encoding="utf-8")
    return text, metadata.core_engines.python


def resolve_dependencies(
    job: GlueJobProject,
    *,
    exclude_builtins: bool = False,
) -> ResolvedDependencies:
    """Resolve direct and transitive dependencies for the job Glue version.

    Args:
        job: Resolved config from
            :func:`~aws_glue_toolkit.pyproject.load_pyproject`.
        exclude_builtins: Omit runtime-built-in packages (for wheel builds).

    Returns:
        Pinned packages and ``requirements.txt`` text.

    Raises:
        DependencyConflictError: Requirements are unsatisfiable.

    """
    metadata = job.runtime_metadata
    python_version = metadata.core_engines.python
    if not job.dependencies:
        return ResolvedDependencies(
            packages={},
            requirements_txt="",
            python_version=python_version,
        )

    requirements_txt, python_version = _compile_requirements_txt(job)
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
