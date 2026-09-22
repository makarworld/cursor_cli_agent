"""
Планировщик напоминаний: локальная SQLite + фоновый poll каждые N сек.
Без Todoist. Source of truth = scheduler.db.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

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

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() in ("1", "true", "yes")
SCHEDULER_POLL_INTERVAL = int(os.getenv("SCHEDULER_POLL_INTERVAL_SECONDS", "30"))
_DEFAULT_DB = "/workspace/.bot/scheduler.db"
TG_TITLE_BLOCK_SUBSTR = ("зуб", "таблет")

_db = SqliteDatabase(None)
_scheduler_lock = asyncio.Lock()
_loop_task: asyncio.Task | None = None
_db_initialized = False

# In-memory состояние фонового таска
_runtime: dict[str, Any] = {
    "status": "idle",  # idle | checking | delivering
    "last_tick_at": None,
    "last_error": None,
    "due_count": 0,
}


def _db_path() -> Path:
    return Path(os.getenv("SCHEDULER_DB_PATH", _DEFAULT_DB))


class _BaseModel(Model):
    class Meta:
        database = _db


class ScheduledEvent(_BaseModel):
    """Локальное напоминание: одно уведомление в remind_at."""

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
    # Deprecated (остаются в БД для старых записей, в логике не используются)
    todoist_task_id = CharField(null=True, index=True, max_length=64)
    starts_at = DateTimeField(null=True)
    offset_hours = IntegerField(null=True)


def is_tg_reminder_blocked(title: str) -> bool:
    t = " ".join(title.casefold().split())
    return any(s in t for s in TG_TITLE_BLOCK_SUBSTR)


def get_scheduler_status() -> dict[str, Any]:
    """Снимок in-memory состояния фонового цикла."""
    return dict(_runtime)


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
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _db.init(str(path))
    _db.connect(reuse_if_open=True)
    _db.create_tables([ScheduledEvent], safe=True)
    _migrate_columns()
    _db_initialized = True
    logger.info("Планировщик: БД %s", path)


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
    **_legacy: Any,
) -> ScheduledEvent:
    """Создаёт одно локальное напоминание на remind_at."""
    ensure_db()
    title = title.strip()
    if not title:
        raise ValueError("Пустой заголовок напоминания")
    if is_tg_reminder_blocked(title):
        raise ValueError("Напоминания про зубы/таблетки запрещены")
    event = ScheduledEvent.create(
        user_id=user_id,
        chat_id=chat_id,
        title=title[:500],
        body=(body or "").strip(),
        remind_at=_normalize_dt(remind_at),
    )
    logger.info(
        "Создано напоминание id=%s user=%s at=%s: %s",
        event.id,
        user_id,
        event.remind_at,
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
    """
    Совместимый API: одна запись с remind_at=starts_at (время уведомления).
    Раньше создавал Todoist + пинги −5ч/−1ч — больше нет.
    """
    event = create_event(user_id, chat_id, title, starts_at, body)
    return [event]


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
    """Отменяет одно локальное напоминание по id."""
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
    ScheduledEvent.update(cancelled=True).where(ScheduledEvent.id == event_id).execute()
    return True


def mark_reminded(event_id: int) -> None:
    ScheduledEvent.update(reminded=True, reminded_at=_utc_now()).where(
        ScheduledEvent.id == event_id
    ).execute()


def format_event_line(event: ScheduledEvent, *, escape_html: Callable[[str], str]) -> str:
    status = "OK" if event.reminded else "ожидает"
    at = event.remind_at.strftime("%d.%m.%Y %H:%M UTC")
    return f"{status} <code>#{event.id}</code> {at} — {escape_html(event.title)}"


def format_reminders_grouped(
    events: list[ScheduledEvent], *, escape_html: Callable[[str], str]
) -> str:
    """Плоский список (без группировки Todoist)."""
    return "\n".join(format_event_line(e, escape_html=escape_html) for e in events)


def format_dry_reminder(event: ScheduledEvent) -> str:
    at = event.remind_at.strftime("%d.%m.%Y %H:%M UTC")
    title = html.escape(event.title)
    body = html.escape(event.body) if event.body else ""
    extra = f"\n{body}" if body else ""
    return f"<b>Напоминание</b>\n{title}{extra}\nВремя: <code>{at}</code>"


async def _notify_scheduler_error(bot: Bot, title: str, error_text: str) -> None:
    """Ошибки бэк-таска → первый админ; сам notify не роняет цикл."""
    try:
        from .admin_notify import notify_admin_error

        await notify_admin_error(
            bot,
            title,
            error_text,
            source="scheduler",
        )
    except Exception as e:
        logger.warning("notify_admin_error из планировщика упал: %s", e)


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
        err = traceback.format_exc()
        logger.exception("Не удалось отправить напоминание id=%s: %s", event.id, e)
        # не помечаем reminded — retry на следующем тике
        await _notify_scheduler_error(
            bot,
            f"Доставка напоминания #{event.id} не удалась",
            err,
        )


async def _scheduler_tick(
    bot: Bot,
    deliver: Callable[[Bot, int, str], Awaitable[None]],
) -> None:
    async with _scheduler_lock:
        _runtime["status"] = "checking"
        _runtime["last_tick_at"] = time.time()
        due = get_due_events()
        _runtime["due_count"] = len(due)
        if not due:
            _runtime["status"] = "idle"
            return
        logger.info("Планировщик: найдено %s сработавших напоминаний", len(due))
        _runtime["status"] = "delivering"
        for event in due:
            await _process_one_event(bot, event, deliver)
        _runtime["status"] = "idle"


async def scheduler_loop(
    bot: Bot,
    deliver: Callable[[Bot, int, str], Awaitable[None]],
) -> None:
    logger.info(
        "Планировщик запущен (интервал %s сек, БД %s)",
        SCHEDULER_POLL_INTERVAL,
        _db_path(),
    )
    while True:
        try:
            await _scheduler_tick(bot, deliver)
            _runtime["last_error"] = None
        except Exception as e:
            err = traceback.format_exc()
            _runtime["last_error"] = str(e)
            _runtime["status"] = "idle"
            logger.exception("Ошибка в цикле планировщика: %s", e)
            await _notify_scheduler_error(bot, "Ошибка цикла планировщика", err)
        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)


def start_scheduler(
    bot: Bot,
    deliver: Callable[[Bot, int, str], Awaitable[None]],
) -> asyncio.Task | None:
    """Запускает фоновую задачу планировщика при старте бота."""
    global _loop_task
    if not SCHEDULER_ENABLED:
        logger.info("Планировщик отключён (SCHEDULER_ENABLED=false)")
        return None
    init_db()
    if _loop_task is not None and not _loop_task.done():
        return _loop_task
    _runtime["status"] = "idle"
    _loop_task = asyncio.create_task(scheduler_loop(bot, deliver))
    return _loop_task
