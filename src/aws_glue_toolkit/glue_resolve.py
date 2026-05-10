"""Glue job dependency resolution using bundled pins and ``uv pip compile``.

Joins ``pyproject.toml`` direct requirements with ``python_packages`` pins from
bundled Glue version JSON, then resolves transitively via PyPI metadata (no
local install). Invokes ``uv`` through ``subprocess.run`` with a fixed argv—no
shell—using file paths under a temporary workspace.

Note:
    ``# nosec B404`` marks the ``subprocess`` import as reviewed (Bandit flags
    the module wholesale). Usage is only in ``uv_pip_compile``; see its Note
    for argv/shell handling.

"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from shutil import which
from subprocess import CalledProcessError, run  # nosec B404
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final, NamedTuple

from aws_glue_toolkit.glue_runtime import load_glue_runtime_metadata

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_glue_toolkit.glue_pyproject import PyProject

__all__ = [
    "UvCompileWorkspacePaths",
    "UvPipCompileError",
    "resolve_glue_dependencies",
    "uv_compile_workspace",
    "uv_pip_compile",
]

# Default ``--python-platform``: x86_64 manylinux2014 (AWS docs).
# Token spelling matches ``uv``. Use ``aarch64-manylinux2014`` on Graviton.
_DEFAULT_PYTHON_PLATFORM: Final[str] = "x86_64-manylinux2014"


class UvPipCompileError(Exception):
    """Non-zero ``uv pip compile`` exit; stderr or stdout (trimmed)."""


class UvCompileWorkspacePaths(NamedTuple):
    """Paths for ``uv pip compile`` inputs and ``-o`` under one temp directory.

    Attributes:
        requirements_in_path: Path to ``requirements.in`` (compile target).
        constraints_txt_path: Path to ``constraints.txt`` (``-c``).
        requirements_txt_path: Path to ``requirements.txt`` (``-o``).

    """

    requirements_in_path: Path
    constraints_txt_path: Path
    requirements_txt_path: Path


@contextmanager
def uv_compile_workspace(
    requirements_in: str,
    constraints_txt: str,
) -> Iterator[UvCompileWorkspacePaths]:
    """Materialize pip-compile inputs in a temporary directory.

    Args:
        requirements_in: Contents written to ``requirements.in``.
        constraints_txt: Contents written to ``constraints.txt``.

    Yields:
        ``UvCompileWorkspacePaths``: written inputs at ``requirements_in_path``
        and ``constraints_txt_path``; ``requirements_txt_path`` is the ``-o``
        argument for ``uv pip compile``.

    """
    # Keep compile scratch off the repo; remove it when the context ends.
    with TemporaryDirectory() as tmp:
        work = Path(tmp)

        # Pip-tools names so ``uv`` argv matches common docs and tooling.
        requirements_in_path = work / "requirements.in"
        constraints_txt_path = work / "constraints.txt"
        requirements_txt_path = work / "requirements.txt"

        # ``uv`` reads inputs from paths—materialize bodies instead of piping.
        requirements_in_path.write_text(requirements_in, encoding="utf-8")
        constraints_txt_path.write_text(constraints_txt, encoding="utf-8")

        # Caller invokes ``uv pip compile`` while these paths remain valid.
        yield UvCompileWorkspacePaths(
            requirements_in_path,
            constraints_txt_path,
            requirements_txt_path,
        )


def uv_pip_compile(
    workspace: UvCompileWorkspacePaths,
    *,
    uv_exe: str,
    python_version: str,
    python_platform: str,
) -> None:
    """Run ``uv pip compile`` and write to ``workspace.requirements_txt_path``.

    Args:
        workspace: ``requirements.in``, ``constraints.txt``, and ``-o`` paths
            (for example from ``uv_compile_workspace``).
        uv_exe: Path to the ``uv`` executable.
        python_version: Passed as ``--python-version``.
        python_platform: Passed as ``--python-platform``.

    Raises:
        UvPipCompileError: Non-zero exit; message from ``uv`` stderr or stdout.

    Note:
        ``# noqa: S603`` (Ruff, from flake8-bandit): warns when
        ``subprocess.run`` uses a non-literal program path; here ``cmd[0]`` is
        ``uv_exe`` from the caller, so the rule fires unless suppressed after
        review.
        ``# nosec B603`` (Bandit): ``subprocess_without_shell_equals_true``
        reports LOW severity on subprocess calls that do not go through a
        shell—Bandit still asks you to validate argv. Suppression records that
        review: ``shell=False`` below, ``cmd`` is an argv list (no shell
        parsing), and path strings come from ``workspace`` plus ``uv_exe``.

    """
    # Build argv for ``uv``; execute without a shell (see Note).
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
        python_platform,
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
        raise UvPipCompileError(msg) from None


def resolve_glue_dependencies(
    pyproject: PyProject,
    *,
    uv_path: str | None = None,
    python_platform: str = _DEFAULT_PYTHON_PLATFORM,
) -> dict[str, str]:
    """Resolve job dependencies including transitives for the Glue version.

    Supplies ``project.dependencies`` as requirements and bundled runtime
    ``python_packages`` as constraints to ``uv pip compile``.

    Args:
        pyproject: Validated Glue job ``pyproject.toml``.
        uv_path: Explicit path to ``uv``, or ``None`` to resolve via ``PATH``.
        python_platform: ``uv pip compile --python-platform``. Defaults to
            x86_64 manylinux2014; use ``aarch64-manylinux2014`` on Graviton.

    Returns:
        Package name to pinned version for each compile-output line that
        contains ``==``. Empty when there are no such lines.

    Raises:
        FileNotFoundError: ``uv_path`` was not set and ``uv`` was not found on
            ``PATH``.
        ValueError: ``glue_version`` is not in the supported bundled set.
        UvPipCompileError: ``uv pip compile`` exited non-zero.

    """
    # Find ``uv``: use ``uv_path`` or locate ``uv`` on ``PATH``.
    uv_exe = uv_path if uv_path is not None else which("uv")
    if uv_exe is None:
        msg = "uv executable not found; install uv or pass uv_path="
        raise FileNotFoundError(msg)

    # Load pins and engine metadata for ``glue_version``.
    metadata = load_glue_runtime_metadata(
        pyproject.tool.aws_glue_toolkit.glue_version,
    )

    # Compile against bundled pins; read the ``-o`` file for parsing.
    with uv_compile_workspace(
        requirements_in="\n".join(pyproject.project.dependencies),
        constraints_txt="\n".join(
            f"{name}=={version}"
            for name, version in metadata.python_packages.items()
        ),
    ) as workspace:
        uv_pip_compile(
            workspace,
            uv_exe=uv_exe,
            python_version=metadata.core_engines.python,
            python_platform=python_platform,
        )
        text = workspace.requirements_txt_path.read_text(encoding="utf-8")

    # Build ``name -> version`` from lines containing ``==``.
    return dict(
        line.partition("==")[::2] for line in text.splitlines() if "==" in line
    )
