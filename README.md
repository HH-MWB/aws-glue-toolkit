# AWS Glue Toolkit

A streamlined command-line utility designed to simplify the AWS Glue development lifecycle.

> **Disclaimer:** This is an independent, community-maintained project. It is **not** affiliated with, endorsed by, or sponsored by Amazon Web Services (AWS). AWS, AWS Glue, and related marks are trademarks of Amazon.com, Inc. or its affiliates.

## Installation

Requires Python 3.11+. Install [Docker](https://docs.docker.com/get-docker/) for `run`, `test`, and `gtk build --mode container`; the Glue image is pulled on first use. Default `gtk build` uses host pip (`--mode host`).

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

Configure the job in `pyproject.toml`:

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

Then run:

```bash
cd my-glue-job
gtk build .
gtk run .
gtk test .
```

## Configuration

Each job is a directory containing `pyproject.toml`. `gtk` reads the `[project]` and `[tool.aws-glue-toolkit]` fields in the table below. Other keys in the file are skipped. The `source` directory and entry `script` must exist before `gtk` runs.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `project.name` | yes | — | Job name; used in artifact file names |
| `project.version` | no | `0.0.0` | Job version; used in artifact file names |
| `project.dependencies` | no | `[]` | Direct dependencies (PEP 508: PyPI, `file:`, Git) |
| `tool.aws-glue-toolkit.glue_version` | yes | — | Glue release; bundled pins for `5.0` and `5.1` |
| `tool.aws-glue-toolkit.source` | yes | — | Source directory, relative to the job root |
| `tool.aws-glue-toolkit.script` | yes | — | Entry script, relative to `source` |
| `tool.aws-glue-toolkit.tests` | no | `tests` | Test directory, relative to the job root |
| `tool.aws-glue-toolkit.dependencies` | no | `[]` | Extra PEP 508 strings merged after `project.dependencies`; recommended for relative `file:` paths |

## Commands

`[JOB-DIR]` is the job directory (default `.`) on **`build`**, **`run`**, and **`test`**.

| Command | Usage |
| --- | --- |
| build | `gtk build [JOB-DIR] [--mode host\|container]` |
| run | `gtk run [JOB-DIR] [--platform native\|worker] [args...]` |
| test | `gtk test [JOB-DIR] [--platform native\|worker] [pytest args...]` |

Run `gtk COMMAND --help` for flags and behavior (for example `gtk build --help`).

For `build --mode container`, `run`, and `test`, `gtk` snapshots the host [pip configuration](https://pip.pypa.io/en/stable/topics/configuration/) for pip inside the container.

## Build artifacts

Use `gtk build` to verify dependencies against Glue runtime pins. It writes two zips under the job directory:

| File | Glue parameter | Contents |
| --- | --- | --- |
| `{name}-{version}.dependencies.zip` | `--extra-py-files` | `.py` files under `source`, except the entry script |
| `{name}-{version}.gluewheels.zip` | `--additional-python-modules` (Glue 5.0+) | `wheels/requirements.txt` and `*.whl` per [AWS Glue Appendix A](https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html) |

## Exit codes

Documented failure codes follow the Berkeley `sysexits` convention in the 64–78 band:

| Code | Meaning |
| --- | --- |
| 65 | Pip or dependency resolution failure (`build`, `run`, or `test`) |
| 66 | `pyproject.toml` missing or unreadable |
| 69 | Docker not available or could not be started |
| 70 | **`build` only:** zip or other packaging failure on the host |
| 78 | Invalid `pyproject.toml`, job layout (missing source, script, or tests directory), unsupported Glue version, or invalid dependency |

Success is `0`; unhandled errors are `1`. **`build`** uses the codes above. **`run`** and **`test`** return the container command’s exit code when pip install and the main command succeed (pytest exit code `1` means failing tests).

## License

This project is licensed under the MIT License — see the [LICENSE](https://github.com/HH-MWB/aws-glue-toolkit/blob/main/LICENSE) file for details.
