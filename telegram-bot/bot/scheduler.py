"""
Планировщик напоминаний: SQLite + Peewee, фоновая проверка событий.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from peewee import (
    AutoField,
    BigIntegerField,
    BooleanField,
    CharField,
    DateTimeField,
    Model,
    SqliteDatabase,
    TextField,
)

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() in ("1", "true", "yes")
SCHEDULER_POLL_INTERVAL = int(os.getenv("SCHEDULER_POLL_INTERVAL_SECONDS", "30"))
SCHEDULER_DB_PATH = Path(os.getenv("SCHEDULER_DB_PATH", "/workspace/.bot/scheduler.db"))

_db = SqliteDatabase(None)
_scheduler_lock = asyncio.Lock()
_loop_task: asyncio.Task | None = None


class _BaseModel(Model):
    class Meta:
        database = _db


class ScheduledEvent(_BaseModel):
    """Запланированное напоминание."""

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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_dt(value: datetime) -> datetime:
    """Приводит datetime к naive UTC для хранения в SQLite."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def init_db() -> None:
    """Инициализирует SQLite и создаёт таблицы."""
    SCHEDULER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db.init(str(SCHEDULER_DB_PATH))
    _db.connect(reuse_if_open=True)
    _db.create_tables([ScheduledEvent], safe=True)
    logger.info("Планировщик: БД %s", SCHEDULER_DB_PATH)


def close_db() -> None:
    if not _db.is_closed():
        _db.close()


def create_event(
    user_id: int,
    chat_id: int,
    title: str,
    remind_at: datetime,
    body: str = "",
) -> ScheduledEvent:
    """Создаёт новое напоминание."""
    title = title.strip()
    if not title:
        raise ValueError("Пустой заголовок напоминания")
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


def get_due_events(limit: int = 20) -> list[ScheduledEvent]:
    """Возвращает события, время которых наступило и ещё не отправлены."""
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
    """Список напоминаний пользователя."""
    q = ScheduledEvent.select().where(
        (ScheduledEvent.user_id == user_id) & (ScheduledEvent.cancelled == False)  # noqa: E712
    )
    if not include_past:
        q = q.where(ScheduledEvent.reminded == False)  # noqa: E712
    return list(q.order_by(ScheduledEvent.remind_at).limit(limit))


def cancel_event(event_id: int, user_id: int) -> bool:
    """Отменяет напоминание. Возвращает True если найдено и отменено."""
    updated = (
        ScheduledEvent.update(cancelled=True)
        .where(
            (ScheduledEvent.id == event_id)
            & (ScheduledEvent.user_id == user_id)
            & (ScheduledEvent.reminded == False)  # noqa: E712
            & (ScheduledEvent.cancelled == False)  # noqa: E712
        )
        .execute()
    )
    return updated > 0


def mark_reminded(event_id: int) -> None:
    ScheduledEvent.update(reminded=True, reminded_at=_utc_now()).where(
        ScheduledEvent.id == event_id
    ).execute()


def format_event_line(event: ScheduledEvent, *, escape_html: Callable[[str], str] | None = None) -> str:
    """Краткая строка для списка напоминаний."""
    esc = escape_html or (lambda s: s)
    status = "✅" if event.reminded else "⏳"
    at = event.remind_at.strftime("%d.%m.%Y %H:%M UTC")
    return f"{status} <code>#{event.id}</code> {at} — {esc(event.title)}"


def build_reminder_prompt(event: ScheduledEvent) -> str:
    """Промпт для агента: сгенерировать текст уведомления."""
    created = event.created_at.strftime("%d.%m.%Y %H:%M UTC") if event.created_at else "?"
    remind = event.remind_at.strftime("%d.%m.%Y %H:%M UTC")
    body_block = f"\nДополнительный контекст: {event.body}" if event.body else ""
    return (
        "[АВТОМАТИЧЕСКОЕ НАПОМИНАНИЕ]\n"
        "Сработало запланированное событие. Напиши пользователю уведомление в Telegram.\n"
        "Используй ТОЛЬКО Telegram HTML-разметку (b, i, code, a, tg-emoji и т.д.).\n"
        "НЕ меняй код бота. НЕ добавляй SELF_MODIFY_COMMIT.\n"
        "НЕ добавляй schedule_reminder — это уже сработавшее напоминание.\n"
        "Будь дружелюбным, по делу, можно с шуткой. Custom emoji из .emojies — по желанию.\n\n"
        f"Событие: {event.title}{body_block}\n"
        f"Запланировано на: {remind}\n"
        f"Создано: {created}\n"
        f"ID напоминания: {event.id}"
    )


async def _process_one_event(
    bot: Bot,
    event: ScheduledEvent,
    run_agent: Callable[[str], Awaitable[tuple[str, bool]]],
    deliver: Callable[[int, str], Awaitable[None]],
) -> None:
    """Обрабатывает одно сработавшее напоминание."""
    prompt = build_reminder_prompt(event)
    response, success = await run_agent(prompt)
    if not success:
        logger.warning("Агент не смог сгенерировать напоминание id=%s: %s", event.id, response[:200])
        return

    try:
        await deliver(event.chat_id, response)
        mark_reminded(event.id)
        logger.info("Напоминание id=%s отправлено в chat_id=%s", event.id, event.chat_id)
    except Exception as e:
        logger.exception("Не удалось отправить напоминание id=%s: %s", event.id, e)


async def _scheduler_tick(
    bot: Bot,
    run_agent: Callable[[str], Awaitable[tuple[str, bool]]],
    deliver: Callable[[int, str], Awaitable[None]],
) -> None:
    """Одна итерация проверки БД."""
    async with _scheduler_lock:
        due = get_due_events()
        if not due:
            return
        logger.info("Планировщик: найдено %s сработавших напоминаний", len(due))
        for event in due:
            await _process_one_event(bot, event, run_agent, deliver)


async def scheduler_loop(
    bot: Bot,
    run_agent: Callable[[str], Awaitable[tuple[str, bool]]],
    deliver: Callable[[int, str], Awaitable[None]],
) -> None:
    """Фоновый цикл проверки напоминаний."""
    logger.info(
        "Планировщик запущен (интервал %s сек, БД %s)",
        SCHEDULER_POLL_INTERVAL,
        SCHEDULER_DB_PATH,
    )
    while True:
        try:
            await _scheduler_tick(bot, run_agent, deliver)
        except Exception as e:
            logger.exception("Ошибка в цикле планировщика: %s", e)
        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)


def start_scheduler(
    bot: Bot,
    run_agent: Callable[[str], Awaitable[tuple[str, bool]]],
    deliver: Callable[[int, str], Awaitable[None]],
) -> asyncio.Task | None:
    """Запускает фоновую задачу планировщика."""
    global _loop_task
    if not SCHEDULER_ENABLED:
        logger.info("Планировщик отключён (SCHEDULER_ENABLED=false)")
        return None
    init_db()
    if _loop_task is not None and not _loop_task.done():
        return _loop_task
    _loop_task = asyncio.create_task(scheduler_loop(bot, run_agent, deliver))
    return _loop_task
