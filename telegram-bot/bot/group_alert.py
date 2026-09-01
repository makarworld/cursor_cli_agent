"""Группы-алерты: триггеры, cooldown, план фикса, Build/Skip."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from aiogram import F
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

if TYPE_CHECKING:
    from aiogram import Dispatcher, Router

    from .main import run_cursor_agent_streaming

logger = logging.getLogger(__name__)

GROUP_ALERTS_DIR = Path(os.getenv("GROUP_ALERTS_DIR", "/workspace/.bot/group_alerts"))
COOLDOWN_FILE = Path(os.getenv("GROUP_ALERT_COOLDOWN_FILE", "/workspace/.bot/group_alert_cooldowns.json"))
COOLDOWN_SECONDS = int(os.getenv("ALERT_GROUP_COOLDOWN_SECONDS", "300"))
TRIGGERS = tuple(
    t.strip()
    for t in os.getenv("ALERT_GROUP_TRIGGERS", "error,🔴").split(",")
    if t.strip()
)
ALERT_GROUP_IDS: frozenset[int] = frozenset(
    int(x.strip())
    for x in os.getenv("ALERT_GROUP_CHAT_IDS", "-1004293720334").split(",")
    if x.strip()
)

_CAPTION_RE = re.compile(r"GROUP_ALERT_CAPTION::(.+)", re.DOTALL)
_ERROR_WORD_RE = re.compile(r"\berror\b", re.I)

_pending: dict[str, dict] = {}
_build_lock = asyncio.Lock()
_cooldowns: dict[str, float] = {}
_bot_username: str | None = None
_run_agent: Callable[..., Awaitable[tuple[str, bool, bool]]] | None = None
_is_allowed: Callable[[int], bool] | None = None
_workspace_dir: Path = Path("/workspace")


def configure(
    *,
    bot_username: str | None,
    run_agent: Callable[..., Awaitable[tuple[str, bool, bool]]],
    is_allowed: Callable[[int], bool],
    workspace_dir: Path,
) -> None:
    global _bot_username, _run_agent, _is_allowed, _workspace_dir
    _bot_username = bot_username
    _run_agent = run_agent
    _is_allowed = is_allowed
    _workspace_dir = workspace_dir
    GROUP_ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    _load_cooldowns()


def is_alert_group_chat(chat_id: int) -> bool:
    return chat_id in ALERT_GROUP_IDS


def _load_cooldowns() -> None:
    global _cooldowns
    try:
        if COOLDOWN_FILE.exists():
            _cooldowns = {k: float(v) for k, v in json.loads(COOLDOWN_FILE.read_text()).items()}
    except Exception as e:
        logger.warning("group_alert cooldown load: %s", e)
        _cooldowns = {}


def _save_cooldowns() -> None:
    try:
        COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOLDOWN_FILE.write_text(json.dumps(_cooldowns), encoding="utf-8")
    except Exception as e:
        logger.warning("group_alert cooldown save: %s", e)


def _cooldown_key(chat_id: int, trigger: str) -> str:
    return f"{chat_id}:{trigger}"


def _on_cooldown(chat_id: int, trigger: str) -> bool:
    key = _cooldown_key(chat_id, trigger)
    last = _cooldowns.get(key, 0.0)
    return (time.monotonic() - last) < COOLDOWN_SECONDS


def _mark_triggered(chat_id: int, trigger: str) -> None:
    _cooldowns[_cooldown_key(chat_id, trigger)] = time.monotonic()
    _save_cooldowns()


def match_trigger(text: str, entities: list | None = None) -> str | None:
    """Возвращает ключ триггера: mention | error | red | …"""
    if not text:
        return None
    username = (_bot_username or os.getenv("ALERT_BOT_USERNAME", "nesofdbot")).lower()
    lowered = text.lower()
    if f"@{username}" in lowered:
        return "mention"
    if entities:
        for ent in entities:
            if getattr(ent, "type", None) == "mention":
                frag = text[ent.offset : ent.offset + ent.length].lstrip("@").lower()
                if frag == username:
                    return "mention"
    if "🔴" in text:
        return "red"
    if _ERROR_WORD_RE.search(text):
        return "error"
    for trigger in TRIGGERS:
        if trigger == "error":
            continue
        if trigger == "🔴":
            continue
        if trigger.lower() in lowered:
            return trigger.lower()
    return None


def _parse_caption(response: str) -> str:
    m = _CAPTION_RE.search(response)
    if m:
        return m.group(1).strip()
    lines = [ln for ln in response.strip().splitlines() if not ln.startswith("GROUP_ALERT_")]
    return "\n".join(lines[:8]).strip() or "Инцидент — см. план в документе."


def _plan_markup(plan_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔨 Build", callback_data=f"alert_build:{plan_id}"),
                InlineKeyboardButton(text="Skip", callback_data=f"alert_skip:{plan_id}"),
            ]
        ]
    )


def _save_plan_meta(plan_id: str, data: dict) -> None:
    path = GROUP_ALERTS_DIR / f"{plan_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _pending[plan_id] = data


def _load_plan_meta(plan_id: str) -> dict | None:
    if plan_id in _pending:
        return _pending[plan_id]
    path = GROUP_ALERTS_DIR / f"{plan_id}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    _pending[plan_id] = data
    return data


def _analysis_prompt(source_text: str, plan_path: Path, chat_title: str) -> str:
    return f"""[GROUP ALERT — инцидент в ops-чате]

Сообщение из чата «{chat_title}»:
---
{source_text}
---

Протокол (строго по шагам):
1. Определи какой сервис/компонент затронут (VoiceBot, cursor-cli-agent, deploy, БД, …).
2. Посмотри последние логи через MCP deploychan (список сервисов + tail логов).
3. Составь максимально детальный план фикса (root cause, шаги, проверки, риски).
   Используй подход using-superpowers / writing-plans.
4. Сохрани план в файл (markdown):
   {plan_path}
5. В конце ответа ОДНА строка (HTML для Telegram caption, до 900 символов):
   GROUP_ALERT_CAPTION::что сломалось и почему (кратко, по делу)

Не меняй код без отдельной команды Build. Только диагностика и план.
"""


def _build_prompt(plan_path: Path) -> str:
    return f"""[GROUP ALERT — реализация плана]

Реализуй план из файла:
{plan_path}

Правила:
- ponytail: минимальный diff, YAGNI
- execute-plans: батчами, с промежуточными отчётами
- После завершения — отчёт: что сделано, что проверить, коммиты если были

Если план требует правок кода cursor_cli_agent/telegram-bot — добавь SELF_MODIFY_COMMIT::...
"""


async def _run_incident(message: Message) -> None:
    if _run_agent is None:
        return
    plan_id = uuid.uuid4().hex[:12]
    plan_path = GROUP_ALERTS_DIR / f"plan_{plan_id}.md"
    chat_title = message.chat.title or str(message.chat.id)
    source_text = (message.text or message.caption or "").strip()

    status = await message.reply("🔍 Анализирую инцидент…")
    response, success, cancelled = await _run_agent(
        _analysis_prompt(source_text, plan_path, chat_title),
        _workspace_dir,
        session_key=None,
        status_msg=status,
        user_id=message.from_user.id if message.from_user else None,
    )

    if cancelled:
        await status.edit_text(response, reply_markup=None)
        return
    if not success:
        await status.edit_text(f"⛔ Не удалось проанализировать:\n<pre>{html.escape(response[:2000])}</pre>")
        return

    if not plan_path.is_file():
        plan_path.write_text(response, encoding="utf-8")

    caption = _parse_caption(response)
    meta = {
        "plan_id": plan_id,
        "chat_id": message.chat.id,
        "status_message_id": status.message_id,
        "source_text": source_text[:4000],
        "plan_path": str(plan_path),
        "caption": caption,
        "status": "pending",
        "created_at": time.time(),
    }
    _save_plan_meta(plan_id, meta)

    try:
        await status.delete()
    except Exception:
        pass

    await message.answer_document(
        FSInputFile(plan_path, filename=f"incident_plan_{plan_id}.md"),
        caption=caption,
        reply_markup=_plan_markup(plan_id),
    )


async def _run_build(plan_id: str, bot, chat_id: int) -> None:
    if _run_agent is None:
        return
    meta = _load_plan_meta(plan_id)
    if not meta or meta.get("status") in ("building", "done", "skipped"):
        return

    plan_path = Path(meta["plan_path"])
    if not plan_path.is_file():
        await bot.send_message(chat_id, f"⛔ План {plan_id} не найден")
        return

    meta["status"] = "building"
    _save_plan_meta(plan_id, meta)

    status = await bot.send_message(chat_id, "🔨 Build: реализую план…")
    response, success, cancelled = await _run_agent(
        _build_prompt(plan_path),
        _workspace_dir,
        session_key=None,
        status_msg=status,
        user_id=None,
    )

    if cancelled:
        await status.edit_text("⏹ Build остановлен", reply_markup=None)
        meta["status"] = "pending"
        _save_plan_meta(plan_id, meta)
        return

    meta["status"] = "done"
    meta["build_response"] = response[:8000]
    _save_plan_meta(plan_id, meta)

    report = response if success else f"⛔ Ошибка Build:\n{response}"
    try:
        await status.delete()
    except Exception:
        pass
    await bot.send_message(chat_id, f"✅ <b>Отчёт по реализации</b>\n\n{report[:3500]}")


async def handle_group_alert_message(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    if not is_alert_group_chat(message.chat.id):
        return

    text = message.text or message.caption or ""
    trigger = match_trigger(text, message.entities)
    if not trigger:
        return
    if _on_cooldown(message.chat.id, trigger):
        logger.info("group_alert cooldown chat=%s trigger=%s", message.chat.id, trigger)
        return

    _mark_triggered(message.chat.id, trigger)
    asyncio.create_task(_run_incident(message))


async def handle_alert_build(callback: CallbackQuery) -> None:
    if not callback.data or not callback.from_user:
        await callback.answer()
        return
    if _is_allowed and not _is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа")
        return

    plan_id = callback.data.split(":", 1)[1]
    meta = _load_plan_meta(plan_id)
    if not meta:
        await callback.answer("План не найден")
        return
    if meta.get("status") == "done":
        await callback.answer("Уже выполнено")
        return

    await callback.answer("Запускаю Build…")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    async with _build_lock:
        await _run_build(plan_id, callback.bot, meta["chat_id"])


async def handle_alert_skip(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return
    plan_id = callback.data.split(":", 1)[1]
    meta = _load_plan_meta(plan_id)
    if meta:
        meta["status"] = "skipped"
        _save_plan_meta(plan_id, meta)
    await callback.answer("Пропущено")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


def register_group_alert(dp: Dispatcher) -> None:
    """Регистрирует callback-хендлеры Build/Skip."""
    dp.callback_query.register(handle_alert_build, F.data.startswith("alert_build:"))
    dp.callback_query.register(handle_alert_skip, F.data.startswith("alert_skip:"))
