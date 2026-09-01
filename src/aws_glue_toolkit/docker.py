"""Run Glue jobs, tests, and pip in the official AWS Glue local Docker image.

Also exposes :func:`host_pip_runner` for host-side pip (``gtk build
--mode host``).

Pure builders produce ``docker run``, ``spark-submit``, ``pytest``, and pip
argv lists; :func:`run_container` and :func:`run_container_capture` are the
subprocess shells. :func:`run_job` mounts
:mod:`aws_glue_toolkit.run_wrapper` so ``gtk run`` returns after the job
finishes.

Public API: :func:`build_run_argv`, :func:`build_spark_submit_argv`,
:func:`build_pytest_argv`, :func:`build_pip_argv`, :func:`run_container`,
:func:`run_container_capture`, :func:`run_pip_in_container`,
:func:`run_job`, :func:`run_tests`, :func:`pip_runner`,
:func:`host_pip_runner`, :exc:`DockerError`.

Container mount paths live in :mod:`aws_glue_toolkit.paths`. Host pip
config is snapshotted to :data:`~aws_glue_toolkit.paths.PIP_CONFIG_MOUNT`
inside the container.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`run_container`, :func:`run_container_capture`, and host
    ``pip config list`` for the container pip.conf snapshot.

"""

from __future__ import annotations

from ast import literal_eval
from collections import defaultdict
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from shlex import join as shlex_join
from shlex import quote as shlex_quote
from shutil import which
from subprocess import CompletedProcess, run  # nosec B404
from sys import executable
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from aws_glue_toolkit.dependencies import (
    PipError,
    PipRunner,
    pip_error_from_returncode,
    pip_install_target_args,
)
from aws_glue_toolkit.paths import (
    PIP_CONFIG_MOUNT,
    PYTHON_TARGET_MOUNT,
    RUN_WRAPPER_MOUNT,
    WORKSPACE_MOUNT,
    project_path,
    rewrite_path_arg,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from aws_glue_toolkit.job import GlueJobProject

__all__ = [
    "DockerError",
    "build_pip_argv",
    "build_pytest_argv",
    "build_run_argv",
    "build_spark_submit_argv",
    "host_pip_runner",
    "pip_runner",
    "run_container",
    "run_container_capture",
    "run_job",
    "run_pip_in_container",
    "run_tests",
]

_DOCKER_PLATFORM = "linux/amd64"
# Host-local / path-bound options omitted from the container pip.conf.
_PIP_CONFIG_SKIP_KEYS = frozenset(
    {
        "cache-dir",
        "cert",
        "client-cert",
        "log",
        "prefix",
        "python",
        "root",
        "src",
        "target",
    },
)


# --- Exceptions ---


class DockerError(Exception):
    """Docker is unavailable or the container failed to launch."""


# --- Pure builders ---


def build_spark_submit_argv(
    project_dir: Path,
    script: Path,
    job_name: str,
    job_args: Sequence[str],
    *,
    wrapper_path: str = RUN_WRAPPER_MOUNT,
) -> list[str]:
    """Build ``spark-submit`` tokens for a Glue job inside the container.

    Submits :mod:`aws_glue_toolkit.run_wrapper` as the entry script so the
    container returns after the job finishes. The real script path is the
    first argument to the wrapper.
    """
    script_path = project_path(project_dir, script)

    # Inject --JOB_NAME unless the caller already set it.
    if any(
        token == "--JOB_NAME"  # noqa: S105  # nosec B105
        or token.startswith("--JOB_NAME=")
        for token in job_args
    ):
        args = list(job_args)
    else:
        args = ["--JOB_NAME", job_name, *job_args]

    return ["spark-submit", wrapper_path, script_path, *args]


def _pytest_targets(
    project_dir: Path,
    tests_dir: Path,
    pytest_args: Sequence[str],
) -> Sequence[str]:
    """Return pytest path/option tokens for the container command."""
    tests_path = tests_dir.relative_to(project_dir).as_posix()
    if pytest_args and not pytest_args[0].startswith("-"):
        return pytest_args
    return [tests_path, *pytest_args]


def build_pytest_argv(
    project_dir: Path,
    source_dir: Path,
    tests_dir: Path,
    pytest_args: Sequence[str],
) -> list[str]:
    """Build ``-c`` tokens to run ``pytest`` in the Glue image entrypoint."""
    source_mount = project_path(project_dir, source_dir)
    targets = _pytest_targets(project_dir, tests_dir, pytest_args)

    cmd = (
        f"export PYTHONPATH={shlex_quote(source_mount)}:$PYTHONPATH; "
        f"python3 -m pytest {shlex_join(targets)}"
    )
    return ["-c", cmd]


def _pip_install_prefix() -> str:
    """Shell prefix that installs deps into :data:`PYTHON_TARGET_MOUNT`."""
    return f"python3 -m pip {shlex_join(pip_install_target_args())}"


def build_install_and_spark_submit_argv(
    project_dir: Path,
    script: Path,
    job_name: str,
    job_args: Sequence[str],
    *,
    wrapper_path: str = RUN_WRAPPER_MOUNT,
) -> list[str]:
    """Build ``-c`` tokens: pip install ``--target``, then ``spark-submit``."""
    submit = shlex_join(
        build_spark_submit_argv(
            project_dir,
            script,
            job_name,
            job_args,
            wrapper_path=wrapper_path,
        ),
    )
    cmd = (
        f"{_pip_install_prefix()} && "
        f"export PYTHONPATH={shlex_quote(PYTHON_TARGET_MOUNT)}:$PYTHONPATH && "
        f"{submit}"
    )
    return ["-c", cmd]


def build_install_and_pytest_argv(
    project_dir: Path,
    source_dir: Path,
    tests_dir: Path,
    pytest_args: Sequence[str],
) -> list[str]:
    """Build ``-c`` tokens: pip install ``--target``, then ``pytest``."""
    source_mount = project_path(project_dir, source_dir)
    targets = _pytest_targets(project_dir, tests_dir, pytest_args)
    cmd = (
        f"{_pip_install_prefix()} && "
        f"export PYTHONPATH={shlex_quote(PYTHON_TARGET_MOUNT)}:"
        f"{shlex_quote(source_mount)}:$PYTHONPATH; "
        f"python3 -m pytest {shlex_join(targets)}"
    )
    return ["-c", cmd]


def build_pip_argv(pip_args: Sequence[str]) -> list[str]:
    """Build ``-c`` tokens to run ``pip`` in the Glue image entrypoint."""
    cmd = f"python3 -m pip {shlex_join(pip_args)}"
    return ["-c", cmd]


def build_run_argv(
    image: str,
    project_dir: Path,
    container_command: Sequence[str],
    *,
    extra_volumes: Sequence[tuple[Path, str]] = (),
    env_forwards: Sequence[str] = (),
) -> list[str]:
    """Build a ``docker run`` argv list for one Glue local container."""
    host_dir = project_dir.resolve()
    return [
        # Base: ephemeral, interactive, linux/amd64 (Glue local images).
        "docker",
        "run",
        "--rm",
        "-i",
        "--platform",
        _DOCKER_PLATFORM,
        # -e VAR copies from host; -e VAR=value sets an explicit value.
        *[flag for var in env_forwards for flag in ("-e", var)],
        # Job root mounted read-write at the Glue workspace path.
        "-v",
        f"{host_dir}:{WORKSPACE_MOUNT}/",
        # Extra binds: pip work dir, wheel staging, file: dependency paths.
        *[
            flag
            for host_path, container_path in extra_volumes
            for flag in ("-v", f"{host_path.resolve()}:{container_path}")
        ],
        # Image entrypoint (spark-submit, pytest, pip shell command, …).
        "--workdir",
        WORKSPACE_MOUNT,
        image,
        *container_command,
    ]


# --- Host pip config snapshot ---


def _parse_pip_config_list(stdout: str) -> dict[str, dict[str, str]]:
    """Parse ``pip config list`` output into section → key → value.

    ``:env:`` keys overwrite onto ``[global]`` (last write wins). Host-local
    option names in :data:`_PIP_CONFIG_SKIP_KEYS` are omitted.
    """
    settings: dict[str, dict[str, str]] = defaultdict(dict)
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if "=" not in line:
            continue
        left, _, right = line.partition("=")
        section, _, key = left.replace(":env:.", "global.", 1).partition(".")
        if key not in _PIP_CONFIG_SKIP_KEYS:
            settings[section][key] = literal_eval(right)
    return settings


def _pip_settings_to_ini(settings: dict[str, dict[str, str]]) -> str:
    """Render section → key → value settings as pip.conf INI text."""
    if not settings:
        return ""
    parts: list[str] = []
    for section in sorted(settings):
        parts.append(f"[{section}]")
        parts.extend(
            f"{key} = {value}"
            for key, value in sorted(settings[section].items())
        )
        parts.append("")
    return "\n".join(parts)


def _host_pip_config_ini() -> str:
    """Return filtered INI text from the host's effective pip config."""
    try:
        result = run(
            [executable, "-m", "pip", "config", "list"],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )  # nosec B603
    except OSError as exc:
        raise DockerError(str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DockerError(
            f"pip config list failed: {detail}"
            if detail
            else "pip config list failed",
        )
    return _pip_settings_to_ini(_parse_pip_config_list(result.stdout))


@contextmanager
def _host_pip_config_bind() -> Iterator[
    tuple[tuple[Path, str], tuple[str, ...]]
]:
    """Yield a pip.conf volume mount and ``PIP_CONFIG_FILE`` env token."""
    ini = _host_pip_config_ini()
    with TemporaryDirectory() as tmp:
        conf_path = Path(tmp) / "pip.conf"
        conf_path.write_text(ini, encoding="utf-8")
        yield (
            (conf_path, PIP_CONFIG_MOUNT),
            (f"PIP_CONFIG_FILE={PIP_CONFIG_MOUNT}",),
        )


# --- Subprocess shell ---


def _ensure_docker_available() -> None:
    if which("docker") is None:
        msg = "docker not found in PATH"
        raise DockerError(msg)


def run_container(argv: Sequence[str]) -> int:
    """Run ``docker`` and return the exit code.

    Container stdout and stderr pass through unchanged.

    Args:
        argv: Full ``docker run …`` argv list from :func:`build_run_argv`.

    Returns:
        Container process exit code.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    _ensure_docker_available()
    try:
        return run(  # noqa: S603
            argv,
            check=False,
        ).returncode  # nosec B603
    except OSError as exc:
        raise DockerError(str(exc)) from exc


def run_container_capture(argv: Sequence[str]) -> CompletedProcess[str]:
    """Run ``docker`` and capture stdout and stderr.

    Args:
        argv: Full ``docker run …`` argv list from :func:`build_run_argv`.

    Returns:
        Completed subprocess result with captured stdout and stderr.

    Raises:
        DockerError: ``docker`` is not on ``PATH`` or could not be started.

    """
    _ensure_docker_available()
    try:
        return run(  # noqa: S603
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )  # nosec B603
    except OSError as exc:
        raise DockerError(str(exc)) from exc


def run_pip_in_container(
    image: str,
    project_dir: Path,
    pip_args: Sequence[str],
    *,
    volume_mounts: Sequence[tuple[Path, str]],
) -> CompletedProcess[str]:
    """Run ``python3 -m pip`` inside the Glue container.

    Rewrites absolute host paths in ``pip_args`` that fall under
    ``volume_mounts`` to their container paths. Mounts a snapshot of the
    host's effective pip config at
    :data:`~aws_glue_toolkit.paths.PIP_CONFIG_MOUNT` and sets
    ``PIP_CONFIG_FILE`` to that path.

    Args:
        image: Glue local Docker image reference.
        project_dir: Job root directory mounted at
            :data:`~aws_glue_toolkit.paths.WORKSPACE_MOUNT`.
        pip_args: Arguments passed to ``python3 -m pip``.
        volume_mounts: Extra ``(host_path, container_mount)`` pairs for
            this run.

    Returns:
        Completed subprocess result with captured stdout and stderr.

    Raises:
        DockerError: ``docker`` is not on ``PATH``, could not be started,
            or host ``pip config list`` failed.

    """
    resolved_mounts = [
        (host_path.resolve(), container_path)
        for host_path, container_path in volume_mounts
    ]

    # Map host paths in pip argv to container mount paths.
    mapped_args = [rewrite_path_arg(arg, resolved_mounts) for arg in pip_args]

    with _host_pip_config_bind() as (pip_conf_mount, env_forwards):
        argv = build_run_argv(
            image,
            project_dir,
            build_pip_argv(mapped_args),
            extra_volumes=[*resolved_mounts, pip_conf_mount],
            env_forwards=env_forwards,
        )
        return run_container_capture(argv)


def pip_runner(job: GlueJobProject) -> PipRunner:
    """Return a Docker :class:`~aws_glue_toolkit.dependencies.PipRunner`."""

    def execute_pip(
        args: Sequence[str],
        volume_mounts: Sequence[tuple[Path, str]],
    ) -> None:
        result = run_pip_in_container(
            job.runtime.docker_image,
            job.project_dir,
            args,
            volume_mounts=volume_mounts,
        )

        # Translate non-zero pip exit codes to PipError.
        if result.returncode != 0:
            raise pip_error_from_returncode(
                result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    return execute_pip


def host_pip_runner() -> PipRunner:
    """Return a host :class:`~aws_glue_toolkit.dependencies.PipRunner`.

    Runs ``python -m pip`` with the current interpreter. Volume mounts are
    ignored; host paths in ``args`` are used as-is.
    """

    def execute_pip(
        args: Sequence[str],
        volume_mounts: Sequence[tuple[Path, str]],
    ) -> None:
        del volume_mounts  # Host paths in args; mounts are Docker-only.
        try:
            result = run(  # noqa: S603
                [executable, "-m", "pip", *args],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
            )  # nosec B603
        except OSError as exc:
            raise PipError(str(exc)) from exc

        if result.returncode != 0:
            raise pip_error_from_returncode(
                result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    return execute_pip


def run_job(
    job: GlueJobProject,
    *job_args: str,
    extra_volumes: Sequence[tuple[Path, str]] = (),
    install_deps: bool = False,
) -> int:
    """Run the job via ``spark-submit`` in the Glue Docker image.

    Mounts :mod:`aws_glue_toolkit.run_wrapper` as the ``spark-submit`` entry
    so the container returns after the job script finishes.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        *job_args: Tokens forwarded to ``spark-submit`` after ``--JOB_NAME``.
        extra_volumes: Extra host→container binds (pip work, ``file:`` deps).
        install_deps: When true, pip-install job deps into the ephemeral
            container before ``spark-submit`` and mount host pip config.

    Returns:
        Container exit code.

    """
    wrapper = files("aws_glue_toolkit").joinpath("run_wrapper.py")
    with as_file(wrapper) as wrapper_host:
        volumes: list[tuple[Path, str]] = [
            *extra_volumes,
            (Path(wrapper_host), RUN_WRAPPER_MOUNT),
        ]
        if install_deps:
            container_command = build_install_and_spark_submit_argv(
                job.project_dir,
                job.script,
                job.name,
                job_args,
            )
            with _host_pip_config_bind() as (pip_conf_mount, env_forwards):
                return run_container(
                    build_run_argv(
                        job.runtime.docker_image,
                        job.project_dir,
                        container_command,
                        extra_volumes=[*volumes, pip_conf_mount],
                        env_forwards=env_forwards,
                    ),
                )

        container_command = build_spark_submit_argv(
            job.project_dir,
            job.script,
            job.name,
            job_args,
        )
        return run_container(
            build_run_argv(
                job.runtime.docker_image,
                job.project_dir,
                container_command,
                extra_volumes=volumes,
            ),
        )


def run_tests(
    job: GlueJobProject,
    *pytest_args: str,
    extra_volumes: Sequence[tuple[Path, str]] = (),
    install_deps: bool = False,
) -> int:
    """Run pytest in the Glue Docker image.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        *pytest_args: Tokens forwarded to ``pytest``.
        extra_volumes: Extra host→container binds (pip work, ``file:`` deps).
        install_deps: When true, pip-install job deps into the ephemeral
            container before pytest and mount host pip config.

    Returns:
        Container exit code.

    Raises:
        ValueError: Configured tests directory does not exist.

    """
    if not job.tests_dir.is_dir():
        msg = f"tests directory not found: {job.tests_dir}"
        raise ValueError(msg)

    if install_deps:
        container_command = build_install_and_pytest_argv(
            job.project_dir,
            job.source_dir,
            job.tests_dir,
            pytest_args,
        )
        with _host_pip_config_bind() as (pip_conf_mount, env_forwards):
            return run_container(
                build_run_argv(
                    job.runtime.docker_image,
                    job.project_dir,
                    container_command,
                    extra_volumes=[*extra_volumes, pip_conf_mount],
                    env_forwards=env_forwards,
                ),
            )

    container_command = build_pytest_argv(
        job.project_dir,
        job.source_dir,
        job.tests_dir,
        pytest_args,
    )
    return run_container(
        build_run_argv(
            job.runtime.docker_image,
            job.project_dir,
            container_command,
            extra_volumes=extra_volumes,
        ),
    )
