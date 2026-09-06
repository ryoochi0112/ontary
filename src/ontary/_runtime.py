"""Private defaults for runtime-injectable clock and ID seams."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def default_clock() -> datetime:
    """Return the current UTC time used by an unconfigured runtime."""
    return datetime.now(timezone.utc)


def default_id_factory() -> str:
    """Mint the UUID-shaped IDs used by an unconfigured runtime."""
    return str(uuid.uuid4())
