"""Single source of truth for 'now' — the app stores naive UTC everywhere."""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current time as a naive UTC datetime (tzinfo stripped)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
