"""Run ``pip`` for Glue job dependency check and wheel download.

Requires ``pip>=22.2`` (see ``pyproject.toml``) for ``--dry-run --report``.

Callers supply ``platform``, ``python_version``, and package pins from
:class:`~aws_glue_toolkit.runtime.GlueRuntimeMetadata` (for example
:attr:`~aws_glue_toolkit.job.GlueJobProject.runtime` in the CLI). The CLI
``check`` command maps :exc:`PipError` to
:attr:`~aws_glue_toolkit.cli.GtkExitCode.DATAERR`; ``build`` maps it to
:attr:`~aws_glue_toolkit.cli.GtkExitCode.SOFTWARE`.

Public API: :func:`resolve_packages`, :func:`download_wheels`, :exc:`PipError`.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`_pip_run`.

"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from json import loads
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec B404
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "PipError",
    "download_wheels",
    "resolve_packages",
]


class PipError(Exception):
    """``pip`` subprocess failed during resolution or download.

    Attributes:
        returncode: Subprocess exit code, if the failure came from
            :func:`_pip_run`.
        stdout: Captured standard output, if any.
        stderr: Captured standard error, if any.

    """

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        """Initialize from a message and optional subprocess fields."""
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(message)


@contextmanager
def _pip_workspace(
    requirement_specs: Sequence[str],
    runtime_package_pins: Mapping[str, str],
) -> Iterator[Path]:
    """Provide a temporary directory prepared for ``pip`` resolution."""
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "requirements.in").write_text(
            "\n".join(requirement_specs),
            encoding="utf-8",
        )
        (work / "constraints.txt").write_text(
            "\n".join(
                f"{name}=={version}"
                for name, version in runtime_package_pins.items()
            ),
            encoding="utf-8",
        )
        yield work


def _pip_run(*args: str) -> None:
    """Run ``python -m pip`` with ``args``.

    Raises:
        PipError: The subprocess exited with a non-zero status.

    """
    try:
        run(  # noqa: S603
            [sys.executable, "-m", "pip", *args],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )  # nosec B603
    except CalledProcessError as exc:
        message = (
            (exc.stderr or "").strip()
            or (exc.stdout or "").strip()
            or f"command exited with status {exc.returncode}"
        )
        raise PipError(
            message,
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
        ) from exc


class _InstallMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str


class _InstallItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _InstallMetadata


class _InstallationReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    install: list[_InstallItem]


def resolve_packages(
    requirement_specs: Sequence[str],
    runtime_package_pins: Mapping[str, str],
    *,
    python_version: str,
    platform: str,
) -> dict[str, str]:
    """Run ``pip install --dry-run --report`` and return resolved packages.

    Writes a temp ``requirements.in`` and ``constraints.txt``, then parses
    ``install[].metadata`` from pip's installation report JSON.

    Args:
        requirement_specs: Direct dependency requirements (PEP 508 strings).
        runtime_package_pins: Bundled Glue runtime pins
            (``name`` → ``version``).
        python_version: Target Python (e.g. ``"3.11"``); dots are stripped for
            ``--python-version``.
        platform: ``pip install --platform`` value (e.g.
            ``"manylinux2014_x86_64"``).

    Returns:
        Resolved packages (``name`` → ``version``).

    Raises:
        PipError: ``pip install --dry-run`` exited with an error.
        ValidationError: Installation report JSON did not match the expected
            shape.

    """
    with _pip_workspace(requirement_specs, runtime_package_pins) as work:
        _pip_run(
            "install",
            "--requirement",
            str(work / "requirements.in"),
            "--constraint",
            str(work / "constraints.txt"),
            "--dry-run",
            "--ignore-installed",
            "--platform",
            platform,
            "--python-version",
            python_version.replace(".", ""),
            "--only-binary=:all:",
            "--quiet",
            "--report",
            str(work / "report.json"),
        )
        report_text = (work / "report.json").read_text(encoding="utf-8")

    parsed = _InstallationReport.model_validate(loads(report_text))
    return {
        item.metadata.name: item.metadata.version for item in parsed.install
    }


def download_wheels(
    packages: Mapping[str, str],
    dest: Path,
    *,
    python_version: str,
    platform: str,
) -> None:
    """Download pinned wheels for all packages into ``dest``.

    Runs ``pip download --no-deps --only-binary=:all:`` once per package,
    sequentially.

    Args:
        packages: Package name → version pins.
        dest: Directory to write ``.whl`` files into (created if missing).
        python_version: Target Python (e.g. ``"3.11"``); dots are stripped for
            ``--python-version``.
        platform: ``pip download --platform`` value.

    Raises:
        PipError: Any ``pip download`` subprocess exited with an error.

    """
    dest.mkdir(parents=True, exist_ok=True)
    for name, version in packages.items():
        _pip_run(
            "download",
            "--no-deps",
            "--only-binary=:all:",
            "--dest",
            str(dest),
            "--platform",
            platform,
            "--python-version",
            python_version.replace(".", ""),
            f"{name}=={version}",
        )
