# AWS Glue Toolkit

A streamlined CLI utility designed to simplify the AWS Glue development lifecycle.

## Installation

Requires Python 3.11+ and [Docker](https://docs.docker.com/get-docker/) (for all `gtk` commands; assumed installed, never installed by `gtk`). Installing the package adds the `gtk` command and a compatible `pip` release.

```bash
pip install aws-glue-toolkit
```

## Example

A Glue job is a directory with a `pyproject.toml` and a source tree. Given this layout:

```
my-glue-job/
  pyproject.toml
  src/
    __main__.py
  tests/
    test_example.py
```

configure the job in `pyproject.toml`:

```toml
[project]
name = "my-glue-job"
version = "0.1.0"
dependencies = ["pandas>=2"]

[tool.aws-glue-toolkit]
glue_version = "5.1"
source = "src"
script = "__main__.py"
tests = "tests"
```

then run:

```bash
cd my-glue-job
gtk check .
gtk build .
gtk run .
gtk test .
```

`check` validates dependencies inside the official AWS Glue local Docker image. `build` writes deployment zips into the job directory; wheels are built or downloaded in the same image. `run` executes the entry script in that image via `spark-submit`, mounting the job directory at `/home/hadoop/workspace`. Tokens after the job directory are forwarded to the job (for example for `getResolvedOptions`). The container exit code is returned as the process exit code.

## Configuration

Each job is a directory containing `pyproject.toml`. Unknown keys are ignored. The `source` directory and entry `script` must exist before `gtk` runs.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `project.name` | yes | — | Job name; used in artifact file names |
| `project.version` | no | `0.0.0` | Job version; used in artifact file names |
| `project.dependencies` | no | `[]` | Direct dependencies as PEP 508 strings (PyPI, `file:` path, or git/VCS) |
| `tool.aws-glue-toolkit.glue_version` | yes | — | Glue release; bundled pins for `5.0` and `5.1` |
| `tool.aws-glue-toolkit.source` | yes | — | Source directory, relative to the job root |
| `tool.aws-glue-toolkit.script` | yes | — | Entry script, relative to `source` |
| `tool.aws-glue-toolkit.tests` | no | `tests` | Test directory, relative to the job root |

## Commands

`[JOB-DIR]` is the job directory path, passed as a positional argument or with `--job-dir [JOB-DIR]` (default: `.`).

| Command | Usage |
| --- | --- |
| check | `gtk check [JOB-DIR]` |
| build | `gtk build [JOB-DIR]` |
| run | `gtk run [JOB-DIR] [args...]` |
| test | `gtk test [JOB-DIR] [pytest args...]` |

### check

Resolves `project.dependencies` inside the official AWS Glue local Docker image for `glue_version`, against the bundled runtime pins. Supports PyPI version pins, `file:` path references (including paths outside the job directory), and git/VCS direct URLs. Does not write files. Docker pulls the image on first use; `gtk` does not install Docker or pull images explicitly.

### build

Writes a dependencies zip and a gluewheels zip to the job directory. The dependencies zip is assembled on the host from local `.py` files under `source`. The gluewheels zip runs `pip wheel` inside the official AWS Glue local Docker image for `glue_version`, building or downloading wheels for every resolved dependency (PyPI, `file:` path, or git/VCS). Path dependencies must be installable packages (`pyproject.toml` or `setup.py`); loose job modules belong under `source`, not in `dependencies`. Docker pulls the image on first use; `gtk` does not install Docker or pull images explicitly.

| File | Glue parameter | Contents |
| --- | --- | --- |
| `{name}-{version}.dependencies.zip` | `--extra-py-files` | `.py` files under the configured source directory, except the entry script |
| `{name}-{version}.gluewheels.zip` | `--additional-python-modules` (Glue 5.0+) | `wheels/requirements.txt` and `*.whl` files per [AWS Glue Appendix A](https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html) |

The gluewheels zip omits packages already pinned on the Glue image at the same version. Both zips are always written, even when empty.

#### Dependency forms

| Form | Example | Notes |
| --- | --- | --- |
| PyPI | `pydantic==2.13.4` | Version pins or ranges |
| Path (in job) | `my-lib @ file:./libs/my-lib` | Built into a wheel at build time |
| Path (monorepo) | `shared @ file:../packages/shared` | Mounted into the container at build time |
| Git (HTTPS) | `tool @ git+https://github.com/org/tool.git@v1` | Requires container network access and `git` in the Glue image |

Editable installs (`-e`) are rejected.

### run

Runs the configured entry script with `spark-submit` inside the official AWS Glue local Docker image for `glue_version`. The job directory is mounted read-write at `/home/hadoop/workspace` with that path as the container working directory. `gtk` passes `--JOB_NAME` from `project.name` unless you supply your own `--JOB_NAME`. Any additional `--key value` tokens after `[JOB-DIR]` are forwarded to `spark-submit` and are available to the job via `getResolvedOptions` (give an explicit `[JOB-DIR]` when passing extra args from the default directory). Docker pulls the image on first use; `gtk` does not install Docker or pull images explicitly. Container stdout and stderr pass through unchanged. When Docker launches successfully, the process exit code is the container/`spark-submit` exit code.

### test

Runs `python3 -m pytest` inside the official AWS Glue local Docker image for `glue_version`. The job directory is mounted read-write at `/home/hadoop/workspace` with that path as the container working directory. `gtk` sets `PYTHONPATH` to the configured `source` directory and, with no extra tokens, runs pytest against the configured `tests` directory (default `tests`). When the first forwarded token is a pytest option (starts with `-`), that directory is still passed before the options (for example `gtk test . -v` runs `tests` with verbose output). When the first forwarded token is a path, only those tokens are passed to pytest. Docker pulls the image on first use; `gtk` does not install Docker or pull images explicitly. Container stdout and stderr pass through unchanged. When Docker launches successfully, the process exit code is pytest's exit code (for example `1` when tests fail).

### Exit codes

On failure, `gtk` uses BSD `sysexits.h` codes:

| Code | Meaning |
| --- | --- |
| 65 | Dependencies unsatisfiable with Glue runtime pins |
| 66 | `pyproject.toml` missing or unreadable |
| 69 | Docker not available or could not be started |
| 70 | Build pipeline failed |
| 78 | Invalid `pyproject.toml`, job layout, unsupported Glue version, or invalid dependency |

Success exits `0`. Unhandled errors exit `1`. For `run` and `test`, when Docker launches successfully, the process exit code is the container command's exit code (not limited to the BSD codes above).

## Architecture

Layered module layout (core, application, shell) is documented in
[docs/architecture.md](docs/architecture.md).

## License

This project is licensed under the MIT License — see the [LICENSE](https://github.com/HH-MWB/aws-glue-toolkit/blob/main/LICENSE) file for details.
