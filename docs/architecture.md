# Architecture

AWS Glue Toolkit is a **flat** Python package (no subpackages) organized in
**two layers**: **core** (domain) and **shell** (I/O and orchestration).

## Core

Domain facts, rules, and transforms. No CLI, Docker subprocesses, or terminal I/O.

| Module | Responsibility |
| --- | --- |
| `paths.py` | Container mount constants; host→container path mapping |
| `runtime.py` | Bundled Glue version JSON → `GlueRuntimeMetadata` |
| `job.py` | `pyproject.toml` validation → `GlueJobProject` |
| `dependencies.py` | PEP 508 prep; pip dry-run resolve, `pip wheel`, image-pin filtering; staged install for run/test; `PipRunner` injection |
| `artifacts.py` | Zip layout for `.dependencies.zip` and gluewheels tree staging |

Core modules must not import `docker`, `cli`, or `app`. Allowed sibling
imports: `job` → `runtime`, `dependencies` → `runtime`, `dependencies` → `paths`.

## Shell

Process boundaries and use-case orchestration:

| Role | Module | Responsibility |
| --- | --- | --- |
| **Infra** | `docker.py` | `docker run` argv builders, `run_job`, `run_tests`, `run_pip_in_container`, `pip_runner` |
| **Infra** | `run_wrapper.py` | `spark-submit` entry for `gtk run`; clean container exit after the job |
| **Application** | `app.py` | `check`, `build`, `run`, `test`; wires `dependencies` to Docker |
| **Presentation** | `cli.py` | `gtk` entry point, `GtkCommandError`, exit codes, Rich panels |

`app` raises domain exceptions (`PipError`, `DockerError`, `ValueError`,
`OSError`, `RequirementPreparationError`) for `cli` to translate.

### FC/IS and DDD

| Lens | `app.py` |
| --- | --- |
| Functional core / imperative shell | Shell — orchestrates I/O |
| Domain-driven design | Application services — coordinates domain + infrastructure |

Both readings are valid: `app` must not contain domain rules (those live in
core) or presentation (Rich, exit codes).

## Module graph

```mermaid
flowchart TB
  subgraph core [Core]
    paths[paths.py]
    runtime[runtime.py]
    job[job.py]
    dependencies[dependencies.py]
    artifacts[artifacts.py]
  end

  subgraph shell [Shell]
    docker[docker.py]
    runWrapper[run_wrapper.py]
    app[app.py]
    cli[cli.py]
  end

  cli --> app
  cli --> docker
  cli --> job
  app --> job
  app --> dependencies
  app --> artifacts
  app --> docker
  job --> runtime
  dependencies --> runtime
  dependencies --> paths
  docker --> paths
  docker --> dependencies
  docker --> job
  docker --> runWrapper
```

- **Core** must not import `docker`, `cli`, or `app`.
- **`dependencies`** must not import **`docker`** — use an injected `PipRunner` instead.
- **Only `app`** imports both **`dependencies`** and **`docker`** (for orchestration).
- **`run_wrapper.py`** is mounted into the Glue container by `docker.run_job` as the `spark-submit` entry script.

## Composition

| Concern | Owner |
| --- | --- |
| Container mount paths | `paths.py` (`WORKSPACE_MOUNT`, `PIP_WORK_MOUNT`, `RUN_WRAPPER_MOUNT`, etc.) |
| Generic Docker I/O | `docker.run_pip_in_container`, `run_container`, `run_job`, `run_tests` |
| Clean exit after job script | `run_wrapper.py` mounted by `docker.run_job` |
| Host pip index env → container | `docker.run_pip_in_container` forwards `PIP_INDEX_URL` and `PIP_EXTRA_INDEX_URL` when set on the host |
| pip-in-Docker adapter (exit codes → `PipError`) | `docker.pip_runner` via `dependencies.pip_error_from_returncode` |
| Prepare `file:` deps for container pip | `dependencies.prepare_requirements` |
| Resolve (dry-run) | `dependencies.resolve_packages` |
| Wheel bundling + omit image pins | `dependencies.bundle_wheels` |
| Gluewheels zip assembly | `app.build` + `artifacts` staging helpers |

## Command flows

### `gtk check`

`cli` → `app.check` → `dependencies.prepare_requirements` → `dependencies.resolve_packages` → `docker.run_pip_in_container`

### `gtk build`

1. `artifacts.build_dependencies_zip` (host)
2. `app.build` → `dependencies.prepare_requirements`
3. `artifacts.stage_gluewheels_zip` → `dependencies.bundle_wheels` → `artifacts.write_gluewheels_tree` → zip

`bundle_wheels` runs `pip wheel` in Docker, then deletes wheels whose
`name==version` matches bundled Glue image pins.

### `gtk run` / `gtk test`

`cli` → `app.run` / `app.test` → `dependencies.prepare_requirements` (when
deps are present) → `dependencies.staged_requirements` →
`docker.run_job` / `docker.run_tests` (pip install ``--target`` then
spark-submit / pytest in one ephemeral container).

For `gtk run`, `docker.run_job` mounts `run_wrapper.py` as the
`spark-submit` entry so the container returns after the job script
finishes.

## Git and VCS dependencies

Git direct URLs are passed through to pip inside the Glue image. See
[README.md](../README.md#dependency-forms) for supported forms and limits.

## Adding code

1. Fact, rule, or transform → core (`paths`, `runtime`, `job`, `dependencies`, `artifacts`)
2. Multi-step user workflow → `app.py`
3. Subprocess or Docker argv → `docker.py`
4. In-container `spark-submit` entry for `gtk run` → `run_wrapper.py`
5. Terminal, Cyclopts, or exit codes → `cli.py`

If a change needs both pip and Docker, wire it in **`app`**, not in
**`dependencies`** or **`docker`**.

## Future extension: host pip for `check` / `build`

Not implemented (YAGNI). When confirmed, add execution policy in **`app`**
only: provide a host `PipRunner` or `docker.pip_runner(job)` for the Docker
path. Core `dependencies` logic stays unchanged.

## Public API

Library callers typically import:

- `from aws_glue_toolkit.job import load_pyproject, GlueJobProject`
- `from aws_glue_toolkit.runtime import load_runtime`
- `from aws_glue_toolkit.dependencies import prepare_requirements`
- `from aws_glue_toolkit.artifacts import build_dependencies_zip`
- `from aws_glue_toolkit.app import check, build, run, test`

The `gtk` CLI is registered at `aws_glue_toolkit.cli:app`.
