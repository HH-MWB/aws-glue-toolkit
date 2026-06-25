"""AWS Glue Toolkit — simplify the AWS Glue development lifecycle.

Modules:

- :mod:`aws_glue_toolkit.artifacts` — Glue job zip artifact formats
- :mod:`aws_glue_toolkit.cli` — ``gtk`` CLI (`check`, `build`, `run`, `test`)
- :mod:`aws_glue_toolkit.workflows` — ``gtk`` use-case orchestration wired to
  core and Docker
- :mod:`aws_glue_toolkit.docker` — run jobs, tests, and pip in the AWS Glue
  local Docker image
- :mod:`aws_glue_toolkit.job` — load job ``pyproject.toml`` and resolve
  Glue runtime into :class:`~aws_glue_toolkit.job.GlueJobProject`
- :mod:`aws_glue_toolkit.pip` — ``pip`` resolution and wheel download
- :mod:`aws_glue_toolkit.runtime` — bundled per-version Glue runtime pins

See ``docs/architecture.md`` for the two-layer layout (core and shell).

Import submodules directly for library APIs (for example
``from aws_glue_toolkit.job import load_pyproject``).

"""

from importlib.metadata import version

__all__ = ["__version__"]

__version__ = version("aws-glue-toolkit")
