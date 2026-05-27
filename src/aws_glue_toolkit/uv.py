"""Run the Astral ``uv`` CLI (``uv pip …`` subprocess helper).

Requires ``uv`` on ``PATH``. This is not a Python package re-export.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in :func:`uv`.

"""

from __future__ import annotations

from shutil import which
from subprocess import CompletedProcess, run  # nosec B404
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "UvNotFoundError",
    "get_executable",
    "uv",
]


class UvNotFoundError(Exception):
    """``uv`` executable not found on ``PATH``."""


def get_executable() -> str:
    """Return the ``uv`` executable on ``PATH``.

    Raises:
        UvNotFoundError: Not found.

    """
    executable = which("uv")
    if not executable:
        raise UvNotFoundError
    return executable


def uv(args: Sequence[str]) -> CompletedProcess[str]:
    """Run ``uv`` with ``args`` as the argv tail after the executable.

    For pip subcommands, ``args`` must start with ``"pip"`` (e.g.
    ``["pip", "compile", …]``).

    """
    cmd = [get_executable(), *args]
    return run(  # noqa: S603
        cmd,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )  # nosec B603
