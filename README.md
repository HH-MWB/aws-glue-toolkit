# AWS Glue Toolkit

A streamlined CLI utility designed to simplify the AWS Glue development lifecycle.

> **Disclaimer:** This is an independent, community-maintained project. It is **not** affiliated with, endorsed by, or sponsored by Amazon Web Services (AWS). AWS, AWS Glue, and related marks are trademarks of Amazon.com, Inc. or its affiliates.

## Installation

Requires Python 3.11+ and [Docker](https://docs.docker.com/get-docker/) for most `gtk` commands (`check`, default `build`, `run`, `test`; assumed installed, never installed by `gtk`). Installing the package adds the `gtk` command and a compatible `pip` release.

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

See [samples/README.md](samples/README.md) for a worked sample (Glue `5.0`).

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

### Pip configuration

For container pip (`check`, default `build`, and dep install on `run` / `test`), `gtk` snapshots the host’s effective [pip configuration](https://pip.pypa.io/en/stable/topics/configuration/) (files + `PIP_*`, env wins) into a temporary `pip.conf`, mounts it, and sets `PIP_CONFIG_FILE`. Host-local keys (`cache-dir`, `cert`, `target`, and similar) are omitted. With `gtk build --mode fast`, host pip config applies directly.

```bash
# ~/.config/pip/pip.conf  or:
export PIP_EXTRA_INDEX_URL="https://my.company/simple"
gtk check .
```

## Commands

`[JOB-DIR]` is the job directory path, passed as a positional argument or with `--job-dir [JOB-DIR]` (default: `.`). Except for `gtk build --mode fast`, Docker must be available; the Glue image is pulled on first use. `gtk` never installs Docker.

| Command | Usage |
| --- | --- |
| check | `gtk check [JOB-DIR]` |
| build | `gtk build [JOB-DIR] [--mode fast]` |
| run | `gtk run [JOB-DIR] [args...]` |
| test | `gtk test [JOB-DIR] [pytest args...]` |

For `run` / `test`: job dir mounted at `/home/hadoop/workspace`; non-empty `project.dependencies` install into an ephemeral `PYTHONPATH` target (Glue pins as constraints); stdio pass through; exit code is the container command’s (or pip’s if install fails). Same dependency forms as `check` / `build`.

### check

Resolves `project.dependencies` in the Glue image for `glue_version` against bundled pins. Does not write files.

### build

Writes a dependencies zip (host: `.py` under `source`) and a gluewheels zip.

- **Default:** `pip wheel` in the Glue image (`linux/amd64`).
- **`--mode fast`:** host packaging (no Docker). Path/VCS deps via
  `pip wheel --no-deps` (wheel tags must be `any` or the Glue
  `pip_platform`); other packages via `pip download --platform` with
  `--only-binary=:all:` (bundled manylinux + Glue Python version). VCS
  needs network and `git` on the host.

Path deps must be installable packages (`pyproject.toml` or `setup.py`);
loose job modules belong under `source`. Both zips are always written;
gluewheels omits packages already pinned on the image at the same version.

| File | Glue parameter | Contents |
| --- | --- | --- |
| `{name}-{version}.dependencies.zip` | `--extra-py-files` | `.py` files under `source`, except the entry script |
| `{name}-{version}.gluewheels.zip` | `--additional-python-modules` (Glue 5.0+) | `wheels/requirements.txt` and `*.whl` per [AWS Glue Appendix A](https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html) |

#### Dependency forms

| Form | Example | Notes |
| --- | --- | --- |
| PyPI | `pydantic==2.13.4` | Version pins or ranges |
| Path (in job) | `my-lib @ file:./libs/my-lib` | Built into a wheel at build time |
| Path (monorepo) | `shared @ file:../packages/shared` | Default: mounted into the container; fast: host path |
| Git (HTTPS) | `tool @ git+https://github.com/org/tool.git@v1` | Needs network + `git` |

Editable installs (`-e`) are rejected.

### run

`spark-submit` on the entry script. Passes `--JOB_NAME` from `project.name` unless overridden. Extra `--key value` tokens after `[JOB-DIR]` go to `getResolvedOptions` (pass an explicit `[JOB-DIR]` when using `.`). Shuts down the Spark driver so the container returns.

### test

`python3 -m pytest`. `PYTHONPATH` includes `source` (and the install target when deps are present). Default target is the configured `tests` dir; if the first forwarded token starts with `-`, that dir is still passed first (`gtk test . -v`); if it is a path, only those tokens go to pytest.

### Exit codes

| Code | Meaning |
| --- | --- |
| 65 | Dependencies unsatisfiable with Glue runtime pins |
| 66 | `pyproject.toml` missing or unreadable |
| 69 | Docker not available or could not be started |
| 70 | Build pipeline failed |
| 78 | Invalid `pyproject.toml`, job layout, unsupported Glue version, or invalid dependency |

Success is `0`; unhandled errors are `1`. For `run` / `test`, a successful Docker launch returns the container command’s exit code (not limited to the table above).

## Architecture

Two-layer module layout (core and shell) is documented in
[docs/architecture.md](docs/architecture.md).

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and workflow. Participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report security vulnerabilities privately via [SECURITY.md](SECURITY.md).

## License

This project is licensed under the MIT License — see the [LICENSE](https://github.com/HH-MWB/aws-glue-toolkit/blob/main/LICENSE) file for details.
