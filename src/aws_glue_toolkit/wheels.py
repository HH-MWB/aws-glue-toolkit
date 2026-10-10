"""Wheel bundling and Glue image pin omit for ``gtk build``.

Uses :mod:`aws_glue_toolkit.requirements` for prepared specs and pip
workspaces.
Dry-run install reporting is internal to host cross-platform bundling.

Public API: :func:`bundle_wheels`.
"""

from __future__ import annotations

from json import loads
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

from aws_glue_toolkit.requirements import (
    GlueImagePinPolicy,
    PipError,
    PipRunner,
    PreparedRequirements,
    _pip_workspace,
    _volume_mounts_for,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from aws_glue_toolkit.runtime import GlueRuntimeMetadata

__all__ = ["bundle_wheels"]


def _dry_run_install_report(
    work: Path,
    prepared: PreparedRequirements,
    *,
    runner: PipRunner,
) -> dict[str, str]:
    """Run ``pip install --dry-run --report``; return name → version."""
    report_path = Path(work) / "report.json"
    runner(
        [
            "install",
            "--requirement",
            str(work / "requirements.in"),
            "--constraint",
            str(work / "constraints.txt"),
            "--dry-run",
            "--ignore-installed",
            "--quiet",
            "--report",
            str(report_path),
        ],
        _volume_mounts_for(work, prepared),
    )
    data = loads(report_path.read_text(encoding="utf-8"))
    return {
        item["metadata"]["name"]: item["metadata"]["version"]
        for item in data["install"]
    }


def _assert_wheels_portable(dest: Path, pip_platform: str) -> None:
    """Raise :exc:`PipError` if any wheel in ``dest`` is not portable."""
    for wheel_path in sorted(dest.glob("*.whl")):
        _, _, _, tags = parse_wheel_filename(wheel_path.name)
        platforms = frozenset(tag.platform for tag in tags)
        if "any" in platforms or pip_platform in platforms:
            continue
        msg = (
            f"wheel {wheel_path.name} has platform tag(s) "
            f"{', '.join(sorted(platforms))}; "
            f"expected 'any' or {pip_platform!r} for --mode host"
        )
        raise PipError(msg)


def _wheel_direct_url_deps(
    work: Path,
    dest: Path,
    specs: Sequence[str],
    *,
    runner: PipRunner,
    mounts: Sequence[tuple[Path, str]],
) -> None:
    """Wheel every path/VCS dep in ``specs`` into ``dest`` (no-op if none)."""
    url_specs = tuple(
        spec for spec in specs if Requirement(spec).url is not None
    )
    if not url_specs:
        return
    direct_in = work / "direct.in"
    direct_in.write_text("\n".join(url_specs), encoding="utf-8")
    runner(
        [
            "wheel",
            "--no-deps",
            "--requirement",
            str(direct_in),
            "--constraint",
            str(work / "constraints.txt"),
            "--wheel-dir",
            str(dest),
            "--no-cache-dir",
        ],
        mounts,
    )


def _pins_missing_from_dest(
    report: Mapping[str, str],
    dest: Path,
) -> list[str]:
    """Return report pins that have no matching wheel in ``dest``."""
    already = frozenset(
        canonicalize_name(parse_wheel_filename(path.name)[0])
        for path in dest.glob("*.whl")
    )
    return [
        f"{name}=={version}"
        for name, version in sorted(report.items())
        if canonicalize_name(name) not in already
    ]


def _wheel_pin_from_sdist(
    work: Path,
    dest: Path,
    pin_in: Path,
    *,
    runner: PipRunner,
) -> None:
    """Download one pin without platform tags and wheel any sdist."""
    runner(
        [
            "download",
            "--no-deps",
            "--requirement",
            str(pin_in),
            "--constraint",
            str(work / "constraints.txt"),
            "--dest",
            str(dest),
            "--no-cache-dir",
        ],
        (),
    )
    for sdist in sorted(dest.glob("*.tar.gz")):
        runner(
            [
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(dest),
                "--no-cache-dir",
                str(sdist),
            ],
            (),
        )
        sdist.unlink()


def _ensure_pin_wheel(
    work: Path,
    dest: Path,
    pin: str,
    runtime: GlueRuntimeMetadata,
    *,
    runner: PipRunner,
) -> None:
    """Ensure one pin has a wheel in ``dest`` (binary, else sdist→wheel)."""
    pin_in = work / "pin.in"
    pin_in.write_text(pin + "\n", encoding="utf-8")
    try:
        runner(
            [
                "download",
                "--no-deps",
                "--requirement",
                str(pin_in),
                "--constraint",
                str(work / "constraints.txt"),
                "--dest",
                str(dest),
                "--platform",
                runtime.pip_platform,
                "--python-version",
                "".join(runtime.core_engines.python.split(".")),
                "--only-binary=:all:",
                "--no-cache-dir",
            ],
            (),
        )
    except PipError as err:
        detail = f"{err.stderr or ''}{err.stdout or ''}{err}".lower()
        if "no matching distribution found" not in detail:
            raise
        _wheel_pin_from_sdist(work, dest, pin_in, runner=runner)


def _bundle_cross_platform(
    prepared: PreparedRequirements,
    policy: GlueImagePinPolicy,
    dest: Path,
    *,
    runner: PipRunner,
) -> None:
    """Wheel path/VCS deps, then ensure a portable wheel per resolved pin."""
    runtime = policy.runtime
    with _pip_workspace(
        prepared.rewritten_specs,
        policy.constraint_pins,
    ) as work:
        mounts = _volume_mounts_for(
            work,
            prepared,
            staging_parent=dest.parent,
        )
        _wheel_direct_url_deps(
            work,
            dest,
            prepared.rewritten_specs,
            runner=runner,
            mounts=mounts,
        )
        report = _dry_run_install_report(work, prepared, runner=runner)
        for pin in _pins_missing_from_dest(report, dest):
            _ensure_pin_wheel(
                work,
                dest,
                pin,
                runtime,
                runner=runner,
            )
        _assert_wheels_portable(dest, runtime.pip_platform)


def _bundle_in_container(
    prepared: PreparedRequirements,
    policy: GlueImagePinPolicy,
    dest: Path,
    *,
    runner: PipRunner,
) -> None:
    """Build all wheels with ``pip wheel`` in the Glue container."""
    with _pip_workspace(
        prepared.rewritten_specs,
        policy.constraint_pins,
    ) as work:
        runner(
            [
                "wheel",
                "--requirement",
                str(work / "requirements.in"),
                "--constraint",
                str(work / "constraints.txt"),
                "--wheel-dir",
                str(dest),
                "--no-cache-dir",
            ],
            _volume_mounts_for(
                work,
                prepared,
                staging_parent=dest.parent,
            ),
        )


def _omit_pinned_wheels(
    dest: Path,
    pins: Mapping[str, str],
) -> dict[str, str]:
    """Delete Glue-pinned wheels in ``dest``; return kept name→version."""
    kept: dict[str, str] = {}
    for wheel_path in dest.glob("*.whl"):
        name, version, _, _ = parse_wheel_filename(wheel_path.name)
        if pins.get(name) == str(version):
            wheel_path.unlink()
        else:
            kept[name] = str(version)
    return kept


def bundle_wheels(
    prepared: PreparedRequirements,
    policy: GlueImagePinPolicy,
    dest: Path,
    *,
    runner: PipRunner,
    cross_platform: bool = False,
) -> dict[str, str]:
    """Build or download wheels for requirements and omit Glue image pins.

    Args:
        prepared: Dependency specs rewritten for container or host pip.
        policy: Image pin policy (constraints, omit pins, and runtime for
            host ``pip download --platform``).
        dest: Existing wheel output directory from
            :func:`~aws_glue_toolkit.artifacts.stage_gluewheels_zip`.
        runner: Callable that runs pip (container or host).
        cross_platform: When true, path/VCS via ``pip wheel --no-deps``,
            then per pin ``pip download --platform`` or sdist→wheel.
            When false, ``pip wheel`` only.

    Returns:
        Package name → version for wheels kept in ``dest`` (image pins
        removed).

    Raises:
        PipError: ``pip`` failed, or a wheel is not portable for Glue.

    """
    if cross_platform:
        _bundle_cross_platform(prepared, policy, dest, runner=runner)
    else:
        _bundle_in_container(prepared, policy, dest, runner=runner)
    return _omit_pinned_wheels(dest, policy.omit_pins)
