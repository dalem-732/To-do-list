import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.auth import SESSION_SECONDS, purge_expired_sessions
from app.models import Task

CLEANUP_INTERVAL_SECONDS = 60 * 60
_last_run: float | None = None


def purge_expired_guest_tasks(db: Session) -> None:
    cutoff = datetime.now(UTC) - timedelta(seconds=SESSION_SECONDS)
    db.execute(delete(Task).where(Task.user_id.is_(None), Task.guest_id.is_not(None), Task.created_at < cutoff))


def run_cleanup(db: Session) -> None:
    global _last_run
    purge_expired_guest_tasks(db)
    purge_expired_sessions(db)
    db.commit()
    _last_run = time.monotonic()


def maybe_run_cleanup(db: Session) -> None:
    if _last_run is not None and time.monotonic() - _last_run < CLEANUP_INTERVAL_SECONDS:
        return
    run_cleanup(db)
