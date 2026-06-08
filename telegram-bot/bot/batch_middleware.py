"""
Middleware: объединяет сообщения пользователя, пришедшие за короткий интервал,
в одно обращение к агенту (длинный текст, медиагруппы).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)

MESSAGE_BATCH_ENABLED = os.getenv("MESSAGE_BATCH_ENABLED", "true").lower() in ("1", "true", "yes")
MESSAGE_BATCH_WINDOW = float(os.getenv("MESSAGE_BATCH_WINDOW_SECONDS", "2.5"))

ProcessBatchFn = Callable[[Message, str, str], Awaitable[None]]
_process_batch: ProcessBatchFn | None = None


@dataclass
class BatchItem:
    kind: str  # "text" | "photo"
    message: Message
    prompt_body: str = ""
    user_text: str = ""
    abs_path: Path | None = None
    rel_path: str = ""
    caption: str = ""


@dataclass
class _UserBatch:
    items: list[BatchItem] = field(default_factory=list)
    task: asyncio.Task | None = None


class MessageBatchManager:
    """Буфер сообщений пользователя с debounce."""

    def __init__(self, window: float) -> None:
        self.window = window
        self._batches: dict[int, _UserBatch] = {}
        self._lock = asyncio.Lock()

    async def add(self, message: Message) -> bool:
        """
        Добавляет сообщение в буфер.
        Возвращает True — хендлер вызывать не нужно (сообщение буферизовано).
        """
        item = await self._extract_item(message)
        if item is None:
            return False

        user_id = message.from_user.id
        async with self._lock:
            batch = self._batches.get(user_id)
            if batch is None:
                batch = _UserBatch()
                self._batches[user_id] = batch

            batch.items.append(item)
            if batch.task is not None and not batch.task.done():
                batch.task.cancel()
            batch.task = asyncio.create_task(self._flush_later(user_id))

        logger.debug(
            "Батч user=%s: +1 (%s), всего %s",
            user_id,
            item.kind,
            len(batch.items),
        )
        return True

    async def _flush_later(self, user_id: int) -> None:
        try:
            await asyncio.sleep(self.window)
            await self.flush(user_id)
        except asyncio.CancelledError:
            pass

    async def flush(self, user_id: int) -> None:
        async with self._lock:
            batch = self._batches.pop(user_id, None)
        if not batch or not batch.items:
            return

        anchor = batch.items[0].message
        prompt_body, user_text = _merge_items(batch.items)
        count = len(batch.items)
        logger.info(
            "Батч user=%s: объединено %s сообщений → один запрос агенту",
            user_id,
            count,
        )

        if _process_batch is None:
            logger.error("process_batch не настроен, батч user=%s потерян", user_id)
            return

        try:
            await _process_batch(anchor, prompt_body, user_text)
        except Exception:
            logger.exception("Ошибка обработки батча user=%s", user_id)

    async def _extract_item(self, message: Message) -> BatchItem | None:
        if message.text and not message.text.strip().startswith("/"):
            body = (message.html_text or message.text or "").strip()
            plain = message.text.strip()
            if not body:
                return None
            return BatchItem(kind="text", message=message, prompt_body=body, user_text=plain)

        if message.photo:
            from .main import _build_photo_prompt, _save_telegram_photo

            saved = await _save_telegram_photo(message)
            if not saved:
                return None
            dest, rel_path = saved
            caption = (message.caption or "").strip()
            user_text = caption if caption else f"[фото] {rel_path}"
            prompt_body = _build_photo_prompt(rel_path, dest, caption)
            return BatchItem(
                kind="photo",
                message=message,
                prompt_body=prompt_body,
                user_text=user_text,
                abs_path=dest,
                rel_path=rel_path,
                caption=caption,
            )

        return None


def _merge_items(items: list[BatchItem]) -> tuple[str, str]:
    """Собирает единый prompt_body и user_text из частей батча."""
    text_bodies: list[str] = []
    text_users: list[str] = []
    photos: list[BatchItem] = []
    caption = ""

    for item in items:
        if item.kind == "text":
            text_bodies.append(item.prompt_body)
            text_users.append(item.user_text)
        elif item.kind == "photo":
            photos.append(item)
            if item.caption:
                caption = item.caption

    parts: list[str] = []

    if photos:
        if len(photos) == 1:
            parts.append(photos[0].prompt_body)
        else:
            lines = [
                "[Пользователь отправил несколько фото (медиагруппа)]",
            ]
            for i, p in enumerate(photos, 1):
                lines.append(f"Изображение {i}: @{p.abs_path}")
                lines.append(f"Файл в workspace: {p.rel_path}")
            if caption:
                lines.append(f"Запрос пользователя: {caption}")
            else:
                lines.append("Запрос: опиши изображения и ответь пользователю.")
            lines.append("Используй прикреплённые изображения (@...) как визуальный контекст.")
            parts.append("\n".join(lines))

    if text_bodies:
        if len(text_bodies) == 1:
            parts.append(text_bodies[0])
        else:
            parts.append(
                "[Сообщение пользователя (несколько частей подряд)]\n"
                + "\n\n".join(text_bodies)
            )

    prompt_body = "\n\n".join(parts)
    user_parts = list(text_users)
    if caption and caption not in user_parts:
        user_parts.insert(0, caption)
    if not user_parts and photos:
        user_parts.append(caption or f"[{len(photos)} фото]")
    user_text = "\n\n".join(user_parts)
    return prompt_body, user_text


batch_manager = MessageBatchManager(MESSAGE_BATCH_WINDOW)


def setup_message_batch(processor: ProcessBatchFn) -> None:
    """Регистрирует функцию обработки объединённого батча."""
    global _process_batch
    _process_batch = processor


def _is_agent_message(message: Message) -> bool:
    if message.text:
        return not message.text.strip().startswith("/")
    return bool(message.photo)


class MessageBatchMiddleware(BaseMiddleware):
    """Перехватывает текст/фото и буферизует перед вызовом хендлера."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable],
        event: TelegramObject,
        data: dict,
    ):
        if not MESSAGE_BATCH_ENABLED or not isinstance(event, Message):
            return await handler(event, data)

        if not _is_agent_message(event):
            return await handler(event, data)

        from .main import is_allowed

        if not is_allowed(event.from_user.id):
            return await handler(event, data)

        buffered = await batch_manager.add(event)
        if buffered:
            return None
        return await handler(event, data)
