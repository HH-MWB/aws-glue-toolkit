# AWS Glue Toolkit

A streamlined CLI utility designed to simplify the AWS Glue development lifecycle.

## Installation

Requires Python 3.11+. Installing the package adds the `gtk` command and a compatible `pip` release.

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
```

then run:

```bash
cd my-glue-job
gtk check .
gtk build .
```

`check` validates dependencies. `build` writes deployment zips into the job directory.

## Configuration

Each job is a directory containing `pyproject.toml`. Unknown keys are ignored. The `source` directory and entry `script` must exist before `gtk` runs.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `project.name` | yes | — | Job name; used in artifact file names |
| `project.version` | no | `0.0.0` | Job version; used in artifact file names |
| `project.dependencies` | no | `[]` | Direct dependencies as PEP 508 strings |
| `tool.aws-glue-toolkit.glue_version` | yes | — | Glue release; bundled pins for `5.0` and `5.1` |
| `tool.aws-glue-toolkit.source` | yes | — | Source directory, relative to the job root |
| `tool.aws-glue-toolkit.script` | yes | — | Entry script, relative to `source` |

## Commands

`[JOB-DIR]` is the job directory path, passed as a positional argument or with `--job-dir [JOB-DIR]` (default: `.`).

| Command | Usage |
| --- | --- |
| check | `gtk check [JOB-DIR]` |
| build | `gtk build [JOB-DIR]` |

### check

Resolves `project.dependencies` against the bundled runtime pins for `glue_version`. Uses the Glue worker Python version and platform tag from the bundled metadata. Does not write files.

### build

Writes a dependencies zip and a gluewheels zip to the job directory:

| File | Glue parameter | Contents |
| --- | --- | --- |
| `{name}-{version}.dependencies.zip` | `--extra-py-files` | `.py` files under the configured source directory, except the entry script |
| `{name}-{version}.gluewheels.zip` | `--additional-python-modules` (Glue 5.0+) | `wheels/requirements.txt` and `*.whl` files per [AWS Glue Appendix A](https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html) |

The gluewheels zip skips packages already pinned on the Glue image. Both zips are always written, even when empty.

### Exit codes

On failure, `gtk` uses BSD `sysexits.h` codes:

| Code | Meaning |
| --- | --- |
| 65 | Dependencies unsatisfiable with Glue runtime pins |
| 66 | `pyproject.toml` missing or unreadable |
| 70 | Build pipeline failed |
| 78 | Invalid `pyproject.toml`, job layout, or unsupported Glue version |

Success exits `0`. Unhandled errors exit `1`.

## License

This project is licensed under the MIT License — see the [LICENSE](https://github.com/HH-MWB/aws-glue-toolkit/blob/main/LICENSE) file for details.
