"""AWS Glue Toolkit — simplify the AWS Glue development lifecycle.

The stable ``gtk`` command-line interface is documented in the package README
(commands, flags, and exit codes).

Modules:

- :mod:`aws_glue_toolkit.workflows` — ``gtk`` build, run, and test
  orchestration
- :mod:`aws_glue_toolkit.artifacts` — Glue job zip artifact formats
- :mod:`aws_glue_toolkit.cli` — ``gtk`` command-line entry (``build``, ``run``,
  ``test``)
- :mod:`aws_glue_toolkit.requirements` — dependency prep, pin policy,
  pip workspaces
- :mod:`aws_glue_toolkit.wheels` — wheel bundling for build
- :mod:`aws_glue_toolkit.container` — Glue container subprocesses and
  :func:`~aws_glue_toolkit.container.host_pip_runner`
- :mod:`aws_glue_toolkit.job` — load job ``pyproject.toml`` and resolve
  Glue runtime into :class:`~aws_glue_toolkit.job.GlueJobProject`
- :mod:`aws_glue_toolkit.mounts` — container mount paths and path mapping
- :mod:`aws_glue_toolkit.runtime` — bundled per-version Glue runtime pins

Core modules hold domain logic; shell modules run Docker, workflows, and the
``gtk`` command-line interface. See ``CONTRIBUTING.md#architecture`` for layer
layout.

Import submodules directly for library APIs (for example
``from aws_glue_toolkit.job import load_pyproject``).

"""

from importlib.metadata import version

__all__ = ["__version__"]

__version__ = version("aws-glue-toolkit")
