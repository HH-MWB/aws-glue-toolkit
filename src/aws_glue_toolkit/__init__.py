"""AWS Glue Toolkit — CLI utilities for the Glue job development lifecycle.

Package modules:

- :mod:`aws_glue_toolkit.pyproject` — ``pyproject.toml`` schema and loading
- :mod:`aws_glue_toolkit.runtime` — bundled Glue version metadata
- :mod:`aws_glue_toolkit.dependencies` — dependency resolution
- :mod:`aws_glue_toolkit.wheels` — ``.gluewheels.zip`` artifact build
- :mod:`aws_glue_toolkit.cli` — ``gtk`` command-line entry point

"""

from importlib.metadata import version

__version__ = version("aws-glue-toolkit")
