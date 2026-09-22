"""Уведомления в ADMIN_CHAT_ID с полным текстом ошибки."""

from __future__ import annotations

import html
import logging
import os
import time
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)

ERROR_REPORTS_DIR = Path(os.getenv("ERROR_REPORTS_DIR", "/workspace/.bot/errors"))


def admin_chat_id() -> int | None:
    """ADMIN_CHAT_ID, иначе первый id из ALLOWED_USER_IDS."""
    raw = os.getenv("ADMIN_CHAT_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    for part in os.getenv("ALLOWED_USER_IDS", "").split(","):
        part = part.strip()
        if part.isdigit():
            return int(part)
    return None


async def notify_admin_error(
    bot: Bot,
    title: str,
    error_text: str,
    *,
    source: str = "",
    extra: str = "",
) -> None:
    """Шлёт в админ-чат краткую сводку + документ с полным текстом ошибки."""
    chat_id = admin_chat_id()
    if not chat_id or not error_text.strip():
        return

    body = error_text.strip()
    lines = [f"🚨 <b>{html.escape(title)}</b>"]
    if source:
        lines.append(f"📍 {html.escape(source)}")
    if extra:
        lines.append(html.escape(extra[:500]))
    preview = body[:400]
    if len(body) > 400:
        preview += "…"
    lines.append(f"\n<pre>{html.escape(preview)}</pre>")

    ERROR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_path = ERROR_REPORTS_DIR / f"admin_{ts}.txt"
    report_path.write_text(body, encoding="utf-8")

    try:
        await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(report_path, filename=report_path.name),
            caption="\n".join(lines),
        )
    except Exception as e:
        logger.warning("notify_admin_error failed: %s", e)
        try:
            await bot.send_message(chat_id, "\n".join(lines))
        except Exception:
            pass
