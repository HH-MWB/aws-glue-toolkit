"""Run the Astral ``uv`` CLI via subprocess.

Wraps ``uv`` on ``PATH`` (see :func:`get_executable`) for ``uv pip …`` calls.
On failure, :func:`uv` raises :class:`UvCommandError` with captured stdout,
stderr, and exit status instead of :exc:`subprocess.CalledProcessError`.

Public API: :func:`get_executable`, :func:`uv`, :exc:`UvNotFoundError`,
:exc:`UvCommandError`.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in :func:`uv`.

"""

from __future__ import annotations

from shutil import which
from subprocess import CalledProcessError, CompletedProcess, run  # nosec B404
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "UvCommandError",
    "UvNotFoundError",
    "get_executable",
    "uv",
]


class UvNotFoundError(Exception):
    """``uv`` executable not found on ``PATH``.

    Raised by :func:`get_executable` and :func:`uv` before any subprocess
    runs.
    """


class UvCommandError(Exception):
    """``uv`` subprocess exited with a non-zero status.

    Attributes:
        returncode: Process exit code.
        stdout: Captured standard output, or ``None``.
        stderr: Captured standard error, or ``None``.

    The exception message is the first non-empty value among
    :attr:`stderr`, :attr:`stdout`, then a fallback citing :attr:`returncode`.

    """

    def __init__(
        self,
        returncode: int,
        stdout: str | None,
        stderr: str | None,
    ) -> None:
        """Initialize from subprocess failure fields.

        Args:
            returncode: Process exit code.
            stdout: Captured standard output, or ``None``.
            stderr: Captured standard error, or ``None``.

        """
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        message = (
            (stderr or "").strip()
            or (stdout or "").strip()
            or f"uv exited with status {returncode}"
        )
        super().__init__(message)


def get_executable() -> str:
    """Return the ``uv`` executable on ``PATH``.

    Raises:
        UvNotFoundError: ``uv`` is not on ``PATH``.

    """
    executable = which("uv")
    if not executable:
        raise UvNotFoundError
    return executable


def uv(args: Sequence[str]) -> CompletedProcess[str]:
    """Run ``uv`` with ``args`` as the argv tail after the executable.

    For pip subcommands, ``args`` must start with ``"pip"`` (e.g.
    ``["pip", "compile", …]``). Output is captured as text; callers read
    :attr:`~subprocess.CompletedProcess.stdout` when needed.

    Args:
        args: Arguments after the ``uv`` executable name.

    Returns:
        Completed subprocess result (stdout and stderr populated).

    Raises:
        UvNotFoundError: ``uv`` is not on ``PATH``.
        UvCommandError: ``uv`` exited with a non-zero status.

    """
    cmd = [get_executable(), *args]
    try:
        return run(  # noqa: S603
            cmd,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )  # nosec B603
    except CalledProcessError as exc:
        raise UvCommandError(
            exc.returncode,
            exc.stdout,
            exc.stderr,
        ) from exc
