"""Run Glue jobs, tests, and pip in the official AWS Glue local Docker image.

Also exposes :func:`host_pip_runner` for host-side pip (``gtk build --mode
host``).

Pure builders produce ``docker run``, ``spark-submit``, ``pytest``, and pip
argv lists; :func:`run_container` and :func:`run_container_capture` are the
subprocess shells. :func:`run_in_glue_container` orchestrates ``gtk run`` and
``gtk test``; Spark entries mount :mod:`aws_glue_toolkit.run_wrapper` so the
container returns after the job finishes.

Public API: :class:`ContainerEntry`, :class:`SparkContainerEntry`,
:class:`PytestContainerEntry`, :func:`build_run_argv`,
:func:`build_spark_submit_argv`, :func:`build_pytest_argv`,
:func:`build_pip_argv`, :func:`run_container`, :func:`run_container_capture`,
:func:`run_in_glue_container`, :func:`run_pip_in_container`,
:func:`pip_runner`, :func:`host_pip_runner`, :exc:`DockerError`.

Container mount paths live in :mod:`aws_glue_toolkit.mounts`. Host pip
config is snapshotted to :data:`~aws_glue_toolkit.mounts.PIP_CONFIG_MOUNT`
inside the container.

Note:
    ``# nosec B404`` — reviewed ``subprocess`` import; used only in
    :func:`run_container`, :func:`run_container_capture`, and
    :func:`host_pip_runner`.

"""

from __future__ import annotations

from configparser import ConfigParser
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import as_file, files
from io import StringIO
from pathlib import Path
from shlex import join as shlex_join
from shlex import quote as shlex_quote
from shutil import which
from subprocess import CompletedProcess, run  # nosec B404
from sys import executable
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from pip._internal.configuration import OVERRIDE_ORDER, Configuration
from pip._internal.exceptions import (
    ConfigurationError,
    ConfigurationFileCouldNotBeLoaded,
)

from aws_glue_toolkit.mounts import (
    PIP_CONFIG_MOUNT,
    PYTHON_TARGET_MOUNT,
    RUN_WRAPPER_MOUNT,
    WORKSPACE_MOUNT,
    project_path,
    rewrite_path_arg,
)
from aws_glue_toolkit.requirements import (
    DependencySession,
    PipError,
    PipRunner,
    pip_error_from_returncode,
    pip_install_target_args,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from aws_glue_toolkit.job import GlueJobProject

__all__ = [
    "ContainerEntry",
    "DockerError",
    "PytestContainerEntry",
    "SparkContainerEntry",
    "build_pip_argv",
    "build_pytest_argv",
    "build_run_argv",
    "build_spark_submit_argv",
    "host_pip_runner",
    "pip_runner",
    "run_container",
    "run_container_capture",
    "run_in_glue_container",
    "run_pip_in_container",
]

# --- Container run entry (spark-submit vs pytest) ---


@dataclass(frozen=True)
class SparkContainerEntry:
    """Spark job run inside the Glue image (``gtk run``)."""

    job_args: tuple[str, ...]


@dataclass(frozen=True)
class PytestContainerEntry:
    """Pytest run inside the Glue image (``gtk test``)."""

    pytest_args: tuple[str, ...]


ContainerEntry = SparkContainerEntry | PytestContainerEntry

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


def build_run_argv(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    image: str,
    project_dir: Path,
    container_command: Sequence[str],
    *,
    platform: str | None = None,
    extra_volumes: Sequence[tuple[Path, str]] = (),
    env_forwards: Sequence[str] = (),
) -> list[str]:
    """Build a ``docker run`` argv list for one Glue local container.

    Args:
        image: Glue local Docker image reference.
        project_dir: Job root directory mounted at
            :data:`~aws_glue_toolkit.mounts.WORKSPACE_MOUNT`.
        container_command: Tokens after the image name.
        platform: Docker ``--platform`` value, or ``None`` to omit (native
            multi-arch selection).
        extra_volumes: Extra host→container binds.
        env_forwards: Host env var names to pass through with ``-e``.

    """
    host_dir = project_dir.resolve()
    platform_flags = ("--platform", platform) if platform is not None else ()
    return [
        # Base: ephemeral, interactive Glue local container.
        "docker",
        "run",
        "--rm",
        "-i",
        *platform_flags,
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


def _pip_conf_section_and_option(key: str) -> tuple[str, str] | None:
    """Map a pip configuration key to INI section and option, or skip."""
    normalized = key.replace(":env:.", "global.", 1)
    section, _, option = normalized.partition(".")
    if not option or option in _PIP_CONFIG_SKIP_KEYS:
        return None
    return section, option


def _merged_pip_configuration() -> dict[str, object]:
    """Load host pip settings merged in pip override order."""
    configuration = Configuration(isolated=False)
    try:
        configuration.load()
    except (ConfigurationFileCouldNotBeLoaded, ConfigurationError) as exc:
        raise DockerError(str(exc)) from exc
    return {
        key: value
        for variant in OVERRIDE_ORDER
        for bucket in configuration.get_values_in_config(variant).values()
        for key, value in bucket.items()
    }


def _write_pip_conf_ini(sections: dict[str, dict[str, str]]) -> str:
    """Serialize section map to pip.conf INI text."""
    if not sections:
        return ""
    parser = ConfigParser(interpolation=None)
    parser.read_dict(sections)
    buffer = StringIO()
    parser.write(buffer, space_around_delimiters=True)
    return buffer.getvalue()


def _pip_conf_ini_from_merged(merged: dict[str, object]) -> str:
    """Build pip.conf INI text from merged pip configuration keys."""
    sections: dict[str, dict[str, str]] = {}
    for key, value in merged.items():
        parsed = _pip_conf_section_and_option(key)
        if parsed is None:
            continue
        section, option = parsed
        sections.setdefault(section, {})[option] = str(value)
    return _write_pip_conf_ini(sections)


@contextmanager
def _host_pip_config_bind() -> Iterator[
    tuple[tuple[Path, str], tuple[str, ...]]
]:
    """Yield a pip.conf volume mount and ``PIP_CONFIG_FILE`` env token."""
    ini = _pip_conf_ini_from_merged(_merged_pip_configuration())
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
    platform: str,
    volume_mounts: Sequence[tuple[Path, str]],
) -> CompletedProcess[str]:
    """Run ``python3 -m pip`` inside the Glue container.

    Rewrites absolute host paths in ``pip_args`` that fall under
    ``volume_mounts`` to their container paths. Mounts a snapshot of the
    host's effective pip config at
    :data:`~aws_glue_toolkit.mounts.PIP_CONFIG_MOUNT` and sets
    ``PIP_CONFIG_FILE`` to that path.

    Args:
        image: Glue local Docker image reference.
        project_dir: Job root directory mounted at
            :data:`~aws_glue_toolkit.mounts.WORKSPACE_MOUNT`.
        pip_args: Arguments passed to ``python3 -m pip``.
        platform: Docker ``--platform`` (worker arch for container pip).
        volume_mounts: Extra ``(host_path, container_mount)`` pairs for
            this run.

    Returns:
        Completed subprocess result with captured stdout and stderr.

    Raises:
        DockerError: ``docker`` is not on ``PATH``, could not be started,
            or host pip configuration load failed.

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
            platform=platform,
            extra_volumes=[*resolved_mounts, pip_conf_mount],
            env_forwards=env_forwards,
        )
        return run_container_capture(argv)


def pip_runner(job: GlueJobProject) -> PipRunner:
    """Return a Docker :class:`~aws_glue_toolkit.requirements.PipRunner`.

    Always uses the Glue worker Docker platform from runtime metadata.
    """

    def execute_pip(
        args: Sequence[str],
        volume_mounts: Sequence[tuple[Path, str]],
    ) -> None:
        result = run_pip_in_container(
            job.runtime.docker_image,
            job.project_dir,
            args,
            platform=job.runtime.worker_docker_platform,
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
    """Return a host :class:`~aws_glue_toolkit.requirements.PipRunner`.

    Runs ``python -m pip`` with the current interpreter. Host paths in ``args``
    are used as-is.
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


def _run_glue_container_command(
    job: GlueJobProject,
    *,
    platform: str | None,
    container_command: Sequence[str],
    extra_volumes: Sequence[tuple[Path, str]],
    install_deps: bool,
) -> int:
    """Launch one Glue image container; optionally mount host pip config."""

    def launch(
        volumes: Sequence[tuple[Path, str]],
        env_forwards: Sequence[str],
    ) -> int:
        return run_container(
            build_run_argv(
                job.runtime.docker_image,
                job.project_dir,
                container_command,
                platform=platform,
                extra_volumes=volumes,
                env_forwards=env_forwards,
            ),
        )

    if install_deps:
        with _host_pip_config_bind() as (pip_conf_mount, env_forwards):
            return launch([*extra_volumes, pip_conf_mount], env_forwards)
    return launch(extra_volumes, ())


def _container_command_for_entry(
    job: GlueJobProject,
    entry: ContainerEntry,
    *,
    install_deps: bool,
) -> list[str]:
    """Build container argv tokens for a Spark or pytest entry."""
    if isinstance(entry, SparkContainerEntry):
        spark_builder = (
            build_install_and_spark_submit_argv
            if install_deps
            else build_spark_submit_argv
        )
        return spark_builder(
            job.project_dir,
            job.script,
            job.name,
            entry.job_args,
        )
    pytest_builder = (
        build_install_and_pytest_argv if install_deps else build_pytest_argv
    )
    return pytest_builder(
        job.project_dir,
        job.source_dir,
        job.tests_dir,
        entry.pytest_args,
    )


def run_in_glue_container(
    job: GlueJobProject,
    *,
    platform: str | None,
    entry: ContainerEntry,
    dependency_session: DependencySession | None,
    extra_volumes: Sequence[tuple[Path, str]] = (),
) -> int:
    """Run ``spark-submit`` or ``pytest`` in the Glue Docker image.

    When ``dependency_session`` is not ``None``, pip-installs job dependencies
    in the same ephemeral container and mounts a host pip configuration
    snapshot. ``extra_volumes`` supplies staging binds (for example monorepo
    ``file:`` paths) from the caller's
    :func:`~aws_glue_toolkit.requirements.staged_requirements` context.

    Args:
        job: Resolved job config from
            :func:`~aws_glue_toolkit.job.load_pyproject`.
        platform: Docker ``--platform``, or ``None`` for native multi-arch.
        entry: Spark or pytest command description.
        dependency_session: When set, run the install-deps recipe before the
            main command; when ``None``, skip pip install and pip config mount.
        extra_volumes: Extra host→container binds (pip work, ``file:`` deps).

    Returns:
        Container exit code.

    """
    install_deps = dependency_session is not None
    container_command = _container_command_for_entry(
        job,
        entry,
        install_deps=install_deps,
    )

    def execute(volumes: Sequence[tuple[Path, str]]) -> int:
        return _run_glue_container_command(
            job,
            platform=platform,
            container_command=container_command,
            extra_volumes=volumes,
            install_deps=install_deps,
        )

    if isinstance(entry, SparkContainerEntry):
        wrapper = files("aws_glue_toolkit").joinpath("run_wrapper.py")
        with as_file(wrapper) as wrapper_host:
            return execute(
                [
                    *extra_volumes,
                    (Path(wrapper_host), RUN_WRAPPER_MOUNT),
                ],
            )
    return execute(extra_volumes)
