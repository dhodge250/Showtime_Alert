"""Structured in-app logging helpers."""
import json
import logging

logger = logging.getLogger(__name__)


def write_log(category: str, message: str, level: str = "INFO",
              user_id: int | None = None, details: dict | None = None) -> None:
    """Write a structured log entry to the database.

    Silently swallows DB errors so a logging failure never breaks the caller.
    """
    from app import db
    from app.models import LogEntry

    entry = LogEntry(
        level=level,
        category=category,
        message=message,
        user_id=user_id,
        details=json.dumps(details) if details else None,
    )
    try:
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.warning("write_log failed: %s", exc)
