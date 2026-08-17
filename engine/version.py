"""Version identity for GitReins.

Single source of truth is the installed package metadata (importlib.metadata).
When the package is not installed (e.g. a bare source checkout), fall back to
the version declared in pyproject.toml, then to a hardcoded dev placeholder.
"""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

try:
    __version__ = metadata.version("gitreins")
except metadata.PackageNotFoundError:
    try:
        _pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with _pyproject.open("rb") as _f:
            __version__ = tomllib.load(_f)["project"]["version"]
    except (KeyError, OSError, tomllib.TOMLDecodeError):
        __version__ = "0.0.0.dev"
