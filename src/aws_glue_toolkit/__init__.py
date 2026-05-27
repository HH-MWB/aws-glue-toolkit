"""AWS Glue Toolkit — develop and package Python dependencies for Glue jobs.

Modules:

- :mod:`aws_glue_toolkit.pyproject` — read and validate job ``pyproject.toml``;
  returns :class:`~aws_glue_toolkit.pyproject.GlueJobProject`
- :mod:`aws_glue_toolkit.runtime` — bundled per-version Glue runtime pins
- :mod:`aws_glue_toolkit.dependencies` — resolve dependencies with ``uv pip``
- :mod:`aws_glue_toolkit.uv` — ``uv pip`` subprocess helper
- :mod:`aws_glue_toolkit.wheels` — build ``.gluewheels.zip`` artifacts
- :mod:`aws_glue_toolkit.cli` — ``gtk`` CLI

"""

from importlib.metadata import version

__version__ = version("aws-glue-toolkit")
