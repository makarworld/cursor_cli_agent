"""Миграция старых локальных напоминаний под Todoist + offsets −5ч/−1ч."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

# Allow `python -m bot.migrate_reminders_to_todoist` from /app
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.scheduler import (  # noqa: E402
    PING_OFFSETS_HOURS,
    ScheduledEvent,
    _normalize_dt,
    _utc_now,
    create_event,
    init_db,
    is_tg_reminder_blocked,
)
from bot.todoist_client import TodoistError, create_task, is_configured  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("migrate_reminders")


def migrate(*, dry_run: bool) -> int:
    if not is_configured():
        raise SystemExit("TODOIST_API_KEY не задан")
    init_db()
    now = _utc_now()
    pending = list(
        ScheduledEvent.select().where(
            (ScheduledEvent.reminded == False)  # noqa: E712
            & (ScheduledEvent.cancelled == False)  # noqa: E712
            & (ScheduledEvent.todoist_task_id.is_null(True))
        )
    )
    changed = 0
    for event in pending:
        title = event.title
        if is_tg_reminder_blocked(title):
            logger.info("blacklist cancel #%s %s", event.id, title)
            if not dry_run:
                ScheduledEvent.update(cancelled=True).where(ScheduledEvent.id == event.id).execute()
            changed += 1
            continue

        starts_at = _normalize_dt(event.starts_at or event.remind_at)
        logger.info("migrate #%s → Todoist due=%s %s", event.id, starts_at, title)
        if dry_run:
            changed += 1
            continue

        try:
            tid = str(create_task(title, starts_at, event.body or "")["id"])
        except TodoistError as e:
            logger.error("Todoist fail #%s: %s", event.id, e)
            continue

        ScheduledEvent.update(cancelled=True).where(ScheduledEvent.id == event.id).execute()
        for hours in PING_OFFSETS_HOURS:
            remind_at = starts_at - timedelta(hours=hours)
            if remind_at <= now:
                continue
            create_event(
                event.user_id,
                event.chat_id,
                title,
                remind_at,
                event.body or "",
                todoist_task_id=tid,
                starts_at=starts_at,
                offset_hours=hours,
            )
        changed += 1
    return changed


def main() -> None:
    p = argparse.ArgumentParser(description="Migrate local reminders to Todoist dual offsets")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    n = migrate(dry_run=args.dry_run)
    logger.info("Готово: %s записей%s", n, " (dry-run)" if args.dry_run else "")


if __name__ == "__main__":
    main()
