"""
Планировщик напоминаний: Todoist (source of truth) + локальные TG-пинги −5ч/−1ч.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from peewee import (
    AutoField,
    BigIntegerField,
    BooleanField,
    CharField,
    DateTimeField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)

from .todoist_client import TodoistError, close_task, create_task, get_task, is_configured, list_open_tasks

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() in ("1", "true", "yes")
SCHEDULER_POLL_INTERVAL = int(os.getenv("SCHEDULER_POLL_INTERVAL_SECONDS", "30"))
SCHEDULER_DB_PATH = Path(os.getenv("SCHEDULER_DB_PATH", "/workspace/.bot/scheduler.db"))
TODOIST_SYNC_CHAT_ID = os.getenv("TODOIST_SYNC_CHAT_ID", "").strip()
TG_TITLE_BLOCK_SUBSTR = ("зуб", "таблет")
PING_OFFSETS_HOURS = (5, 1)

_db = SqliteDatabase(None)
_scheduler_lock = asyncio.Lock()
_loop_task: asyncio.Task | None = None
_db_initialized = False


class _BaseModel(Model):
    class Meta:
        database = _db


class ScheduledEvent(_BaseModel):
    """Локальный TG-пинг (offset до starts_at); связан с Todoist-задачей."""

    id = AutoField()
    user_id = BigIntegerField()
    chat_id = BigIntegerField()
    title = CharField(max_length=500)
    body = TextField(default="")
    remind_at = DateTimeField()
    reminded = BooleanField(default=False)
    cancelled = BooleanField(default=False)
    created_at = DateTimeField(default=datetime.utcnow)
    reminded_at = DateTimeField(null=True)
    todoist_task_id = CharField(null=True, index=True, max_length=64)
    starts_at = DateTimeField(null=True)
    offset_hours = IntegerField(null=True)


def is_tg_reminder_blocked(title: str) -> bool:
    t = " ".join(title.casefold().split())
    return any(s in t for s in TG_TITLE_BLOCK_SUBSTR)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _migrate_columns() -> None:
    cols = {row[1] for row in _db.execute_sql("PRAGMA table_info(scheduledevent)").fetchall()}
    needed = {
        "todoist_task_id": "VARCHAR(64)",
        "starts_at": "DATETIME",
        "offset_hours": "INTEGER",
    }
    added = [name for name in needed if name not in cols]
    for name in added:
        _db.execute_sql(f"ALTER TABLE scheduledevent ADD COLUMN {name} {needed[name]}")
    if added:
        logger.info("Планировщик: миграция колонок %s", added)


def init_db() -> None:
    global _db_initialized
    SCHEDULER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db.init(str(SCHEDULER_DB_PATH))
    _db.connect(reuse_if_open=True)
    _db.create_tables([ScheduledEvent], safe=True)
    _migrate_columns()
    _db_initialized = True
    logger.info("Планировщик: БД %s", SCHEDULER_DB_PATH)


def ensure_db() -> None:
    if not _db_initialized:
        init_db()


def close_db() -> None:
    if not _db.is_closed():
        _db.close()


def create_event(
    user_id: int,
    chat_id: int,
    title: str,
    remind_at: datetime,
    body: str = "",
    *,
    todoist_task_id: str | None = None,
    starts_at: datetime | None = None,
    offset_hours: int | None = None,
) -> ScheduledEvent:
    ensure_db()
    title = title.strip()
    if not title:
        raise ValueError("Пустой заголовок напоминания")
    event = ScheduledEvent.create(
        user_id=user_id,
        chat_id=chat_id,
        title=title[:500],
        body=(body or "").strip(),
        remind_at=_normalize_dt(remind_at),
        todoist_task_id=todoist_task_id,
        starts_at=_normalize_dt(starts_at) if starts_at else None,
        offset_hours=offset_hours,
    )
    logger.info(
        "Создано напоминание id=%s user=%s at=%s offset=%s: %s",
        event.id,
        user_id,
        event.remind_at,
        offset_hours,
        title[:80],
    )
    return event


def create_linked_reminder(
    user_id: int,
    chat_id: int,
    title: str,
    starts_at: datetime,
    body: str = "",
) -> list[ScheduledEvent]:
    """Todoist (due=starts_at) + локальные пинги −5ч/−1ч. Blacklist → Todoist only, []."""
    if not is_configured():
        raise TodoistError("TODOIST_API_KEY не задан — связь с Todoist обязательна")

    starts_at = _normalize_dt(starts_at)
    title = title.strip()
    if not title:
        raise ValueError("Пустой заголовок напоминания")

    task_id = str(create_task(title, starts_at, body or "")["id"])
    if is_tg_reminder_blocked(title):
        logger.info("TG-пинги пропущены (blacklist), todoist=%s: %s", task_id, title[:80])
        return []

    now = _utc_now()
    events: list[ScheduledEvent] = []
    for hours in PING_OFFSETS_HOURS:
        remind_at = starts_at - timedelta(hours=hours)
        if remind_at <= now:
            continue
        events.append(
            create_event(
                user_id,
                chat_id,
                title,
                remind_at,
                body,
                todoist_task_id=task_id,
                starts_at=starts_at,
                offset_hours=hours,
            )
        )
    return events


def get_events_by_todoist_id(task_id: str) -> list[ScheduledEvent]:
    ensure_db()
    return list(
        ScheduledEvent.select().where(ScheduledEvent.todoist_task_id == task_id)
    )


def get_due_events(limit: int = 20) -> list[ScheduledEvent]:
    now = _utc_now()
    return list(
        ScheduledEvent.select()
        .where(
            (ScheduledEvent.reminded == False)  # noqa: E712
            & (ScheduledEvent.cancelled == False)  # noqa: E712
            & (ScheduledEvent.remind_at <= now)
        )
        .order_by(ScheduledEvent.remind_at)
        .limit(limit)
    )


def list_events(
    user_id: int,
    *,
    include_past: bool = False,
    limit: int = 30,
) -> list[ScheduledEvent]:
    q = ScheduledEvent.select().where(
        (ScheduledEvent.user_id == user_id) & (ScheduledEvent.cancelled == False)  # noqa: E712
    )
    if not include_past:
        q = q.where(ScheduledEvent.reminded == False)  # noqa: E712
    return list(q.order_by(ScheduledEvent.remind_at).limit(limit))


def cancel_event(event_id: int, user_id: int) -> bool:
    """Отменяет событие (+ siblings по todoist_task_id) и закрывает Todoist."""
    ensure_db()
    active = (
        (ScheduledEvent.reminded == False)  # noqa: E712
        & (ScheduledEvent.cancelled == False)  # noqa: E712
    )
    event = (
        ScheduledEvent.select()
        .where((ScheduledEvent.id == event_id) & (ScheduledEvent.user_id == user_id) & active)
        .first()
    )
    if not event:
        return False

    tid = event.todoist_task_id
    if tid:
        ScheduledEvent.update(cancelled=True).where(
            (ScheduledEvent.todoist_task_id == tid)
            & (ScheduledEvent.user_id == user_id)
            & active
        ).execute()
        try:
            close_task(tid)
        except TodoistError as e:
            logger.warning("Не удалось закрыть Todoist %s: %s", tid, e)
    else:
        ScheduledEvent.update(cancelled=True).where(ScheduledEvent.id == event_id).execute()
    return True


def mark_reminded(event_id: int) -> None:
    ScheduledEvent.update(reminded=True, reminded_at=_utc_now()).where(
        ScheduledEvent.id == event_id
    ).execute()


def format_event_line(event: ScheduledEvent, *, escape_html: Callable[[str], str]) -> str:
    status = "✅" if event.reminded else "⏳"
    at = event.remind_at.strftime("%d.%m.%Y %H:%M UTC")
    off = f" (−{event.offset_hours}ч)" if event.offset_hours else ""
    return f"{status} <code>#{event.id}</code> {at}{off} — {escape_html(event.title)}"


def format_reminders_grouped(events: list[ScheduledEvent], *, escape_html: Callable[[str], str]) -> str:
    """Группировка по todoist_task_id для /reminders."""
    groups: dict[str, list[ScheduledEvent]] = {}
    singles: list[ScheduledEvent] = []
    for e in events:
        if e.todoist_task_id:
            groups.setdefault(e.todoist_task_id, []).append(e)
        else:
            singles.append(e)

    lines: list[str] = []
    for tid, items in groups.items():
        items = sorted(items, key=lambda x: x.remind_at)
        head = items[0]
        start = (head.starts_at or head.remind_at).strftime("%d.%m.%Y %H:%M UTC")
        lines.append(f"📌 {escape_html(head.title)}\nНачало: <code>{start}</code> · Todoist <code>{tid}</code>")
        lines.extend("  " + format_event_line(e, escape_html=escape_html) for e in items)
        lines.append("")
    lines.extend(format_event_line(e, escape_html=escape_html) for e in singles)
    return "\n".join(lines).strip()


def format_dry_reminder(event: ScheduledEvent) -> str:
    start = (event.starts_at or event.remind_at).strftime("%d.%m.%Y %H:%M UTC")
    left = event.offset_hours or "?"
    title = html.escape(event.title)
    body = html.escape(event.body) if event.body else ""
    extra = f"\n{body}" if body else ""
    return (
        f"⏰ <b>Напоминание</b> (за {left} ч)\n"
        f"{title}{extra}\n"
        f"Начало: <code>{start}</code>"
    )


def reconcile_with_todoist() -> int:
    """Гасит локальные pending, если Todoist-задача закрыта/удалена."""
    if not is_configured():
        return 0
    ensure_db()
    active = (
        (ScheduledEvent.reminded == False)  # noqa: E712
        & (ScheduledEvent.cancelled == False)  # noqa: E712
    )
    pending = list(
        ScheduledEvent.select().where(active & ScheduledEvent.todoist_task_id.is_null(False))
    )
    seen: set[str] = set()
    cancelled = 0
    for event in pending:
        tid = event.todoist_task_id
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            if get_task(tid) is not None:
                continue
        except TodoistError as e:
            logger.warning("Reconcile get_task %s: %s", tid, e)
            continue
        n = (
            ScheduledEvent.update(cancelled=True)
            .where((ScheduledEvent.todoist_task_id == tid) & active)
            .execute()
        )
        cancelled += n
        logger.info("Reconcile: Todoist %s закрыт — отменено локальных %s", tid, n)
    return cancelled


def _parse_todoist_due(task: dict) -> datetime | None:
    due = task.get("due") or {}
    if not isinstance(due, dict):
        return None
    raw = due.get("datetime") or due.get("date")
    if not raw or not isinstance(raw, str):
        return None
    try:
        if "T" in raw:
            return _normalize_dt(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except ValueError:
        return None


def import_todoist_tasks(user_id: int, chat_id: int) -> int:
    """Импорт open Todoist tasks с due в будущем → локальные −5/−1."""
    if not is_configured():
        return 0
    ensure_db()
    now = _utc_now()
    created = 0
    for task in list_open_tasks():
        tid = str(task.get("id", ""))
        title = (task.get("content") or "").strip()
        starts = _parse_todoist_due(task)
        if not tid or not title or is_tg_reminder_blocked(title) or not starts or starts <= now:
            continue
        if any(not e.cancelled for e in get_events_by_todoist_id(tid)):
            continue
        for hours in PING_OFFSETS_HOURS:
            remind_at = starts - timedelta(hours=hours)
            if remind_at <= now:
                continue
            create_event(
                user_id,
                chat_id,
                title,
                remind_at,
                task.get("description") or "",
                todoist_task_id=tid,
                starts_at=starts,
                offset_hours=hours,
            )
            created += 1
    return created


async def _process_one_event(
    bot: Bot,
    event: ScheduledEvent,
    deliver: Callable[[Bot, int, str], Awaitable[None]],
) -> None:
    text = format_dry_reminder(event)
    try:
        await deliver(bot, event.chat_id, text)
        mark_reminded(event.id)
        logger.info("Напоминание id=%s отправлено в chat_id=%s", event.id, event.chat_id)
    except Exception as e:
        logger.exception("Не удалось отправить напоминание id=%s: %s", event.id, e)


async def _scheduler_tick(
    bot: Bot,
    deliver: Callable[[Bot, int, str], Awaitable[None]],
) -> None:
    async with _scheduler_lock:
        try:
            reconcile_with_todoist()
            if TODOIST_SYNC_CHAT_ID.isdigit():
                chat_id = int(TODOIST_SYNC_CHAT_ID)
                import_todoist_tasks(chat_id, chat_id)
        except Exception as e:
            logger.exception("Reconcile/import: %s", e)

        due = get_due_events()
        if not due:
            return
        logger.info("Планировщик: найдено %s сработавших напоминаний", len(due))
        for event in due:
            await _process_one_event(bot, event, deliver)


async def scheduler_loop(
    bot: Bot,
    deliver: Callable[[Bot, int, str], Awaitable[None]],
) -> None:
    logger.info(
        "Планировщик запущен (интервал %s сек, БД %s)",
        SCHEDULER_POLL_INTERVAL,
        SCHEDULER_DB_PATH,
    )
    while True:
        try:
            await _scheduler_tick(bot, deliver)
        except Exception as e:
            logger.exception("Ошибка в цикле планировщика: %s", e)
        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)


def start_scheduler(
    bot: Bot,
    deliver: Callable[[Bot, int, str], Awaitable[None]],
) -> asyncio.Task | None:
    """Запускает фоновую задачу планировщика (сухие пинги, без LLM)."""
    global _loop_task
    if not SCHEDULER_ENABLED:
        logger.info("Планировщик отключён (SCHEDULER_ENABLED=false)")
        return None
    init_db()
    if _loop_task is not None and not _loop_task.done():
        return _loop_task
    _loop_task = asyncio.create_task(scheduler_loop(bot, deliver))
    return _loop_task
