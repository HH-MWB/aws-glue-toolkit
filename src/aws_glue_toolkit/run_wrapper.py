"""Ensure ``gtk run`` returns cleanly after the job script finishes.

Used as the ``spark-submit`` entry: runs the job with the expected
``sys.argv``, then shuts down the driver JVM via the Py4J gateway so the
container exits.
"""

from __future__ import annotations

import runpy
import sys
from os import _exit, environ
from pathlib import Path
from traceback import print_exc

_MIN_ARGV = 2
_USAGE_EXIT = 2


def _exit_code(value: object) -> int:
    """Normalize ``SystemExit.code`` to an integer exit status."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return 1


def _jvm_exit(code: int) -> None:
    """Shut down the spark-submit driver JVM via the Py4J gateway."""
    port = environ.get("PYSPARK_GATEWAY_PORT")
    if port is None:
        return
    try:
        py4j = __import__(
            "py4j.java_gateway",
            fromlist=["GatewayParameters", "JavaGateway"],
        )
    except ImportError:
        return
    gateway = py4j.JavaGateway(
        gateway_parameters=py4j.GatewayParameters(
            port=int(port),
            auth_token=environ.get("PYSPARK_GATEWAY_SECRET"),
            auto_convert=True,
        ),
    )
    gateway.jvm.System.exit(code)


def _run_script(script: str) -> int:
    """Execute ``script`` as ``__main__`` and return an exit code."""
    # Match spark-submit: put the script directory on sys.path[0].
    script_dir = str(Path(script).resolve().parent)
    if sys.path[:1] != [script_dir]:
        sys.path.insert(0, script_dir)

    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        return _exit_code(exc.code)
    # Propagate script failures as a non-zero exit for spark-submit.
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print_exc()
        return 1
    return 0


def main() -> None:
    """Run the job script, then shut down the driver so the container exits."""
    if len(sys.argv) < _MIN_ARGV:
        sys.stderr.write("usage: run_wrapper.py SCRIPT [args...]\n")
        _exit(_USAGE_EXIT)

    script = sys.argv[1]
    sys.argv = [script, *sys.argv[2:]]
    code = _run_script(script)
    _jvm_exit(code)
    _exit(code)


if __name__ == "__main__":
    main()
