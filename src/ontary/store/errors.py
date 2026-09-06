"""Compatibility module for the removed store-specific exception classes.

Store failures now use the kind classes from :mod:`ontary.errors` directly,
with an explicit stable ``code=`` at every raise site.  The module remains as
an importable package resource for tools that discover store modules, but it
intentionally exports no exception aliases.
"""

from __future__ import annotations

__all__: list[str] = []
