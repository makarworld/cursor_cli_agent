"""
Cursor CLI Telegram Bot
Управление Cursor CLI через Telegram.
Использует cursor-agent в headless режиме с сохранением контекста.
"""

import asyncio
import html
import json
import logging
import os
import re
import shutil
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import BasicAuth
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    ChosenInlineResult,
    ErrorEvent,
    FSInputFile,
    InlineQuery,
    InlineQueryResultArticle,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputTextMessageContent,
    Message,
)
from dotenv import load_dotenv

from .agent_sessions import (
    SESSION_KEY_SCHEDULER,
    clear_chat_id,
    ensure_chat_id,
    has_agent_session,
    load_agent_sessions,
    user_session_key,
)
from .batch_middleware import MessageBatchMiddleware, setup_message_batch
from .scheduler import (
    SCHEDULER_ENABLED,
    cancel_event,
    create_event,
    format_event_line,
    list_events,
    start_scheduler,
)
from .self_modify import (
    SELF_MODIFY_AUTO_FIX,
    SELF_MODIFY_CODEWORDS,
    build_codeword_guard_prompt,
    build_self_fix_prompt,
    can_auto_fix,
    check_codeword,
    codeword_required,
    get_bot_code_paths,
    has_bot_code_changes,
    has_codeword,
    git_commit,
    git_discard_worktree,
    git_log,
    git_push,
    git_rollback,
    git_status_short,
    pop_restart_notification,
    is_enabled as self_modify_enabled,
    parse_commit_message,
    record_auto_fix,
    repo_ready,
    schedule_restart,
    validate_python_files,
)

load_dotenv()
load_dotenv(
    Path(os.getenv("WORKSPACE_DIR", "/workspace")) / "cursor_cli_agent" / ".env",
    override=True,
)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
TELEGRAM_PROXY_TYPE = os.getenv("TELEGRAM_PROXY_TYPE", "socks5").strip()
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/workspace"))
FILES_DIR = WORKSPACE_DIR / "files"
CURSOR_CLI_PATH = os.getenv("CURSOR_CLI_PATH", "cursor-agent")
CURSOR_MODEL = os.getenv("CURSOR_MODEL", "auto")
CURSOR_API_KEY = os.getenv("CURSOR_API_KEY")
MEM0_API_KEY = os.getenv("MEM0_API_KEY", "").strip()
CURSOR_TIMEOUT = int(os.getenv("CURSOR_TIMEOUT_SECONDS", "300"))
MAX_RESPONSE_LENGTH = 4000  # Лимит Telegram
USER_PROMPTS_FILE = Path(os.getenv("USER_PROMPTS_FILE", "/workspace/.bot/user_prompts.json"))
ERROR_REPORTS_DIR = Path(os.getenv("ERROR_REPORTS_DIR", "/workspace/.bot/errors"))
DEFAULT_PROMPT_FILE = Path(__file__).resolve().parent.parent / "default_prompt.txt"

dp = Dispatcher()

_bot_username: str | None = None
_pending_inline: dict[str, tuple[str, int]] = {}
_self_fix_lock = asyncio.Lock()
_RESTART_NOTIFY_TEXT = (
    '✅ <b>Бот перезапущен</b> и снова на связи! '
    '<tg-emoji emoji-id="5377809374016192785">🐱</tg-emoji>'
)


def _ensure_mcp_config() -> None:
    """Копирует mcp.json из workspace в ~/.cursor для cursor-agent."""
    src = WORKSPACE_DIR / ".cursor" / "mcp.json"
    if not src.is_file():
        return
    dst_dir = Path.home() / ".cursor"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "mcp.json"
    if not dst.exists() or src.read_bytes() != dst.read_bytes():
        shutil.copy2(src, dst)
        logger.info("MCP config synced: %s -> %s", src, dst)


def _format_commit_push_notes(committed: bool, commit_msg: str, commit_result: str) -> str:
    """Форматирует строки о коммите и push для Telegram."""
    if not committed:
        return html.escape(commit_result)
    lines = [f"📦 Коммит: <code>{html.escape(commit_msg)}</code>"]
    pushed, push_result = git_push()
    icon = "📤" if pushed else "⚠️"
    lines.append(f"{icon} Push: {html.escape(push_result)}")
    return "\n".join(lines)


async def _notify_after_restart(bot: Bot) -> None:
    """Отправляет уведомление пользователю после перезапуска бота."""
    pending = pop_restart_notification()
    if not pending:
        return
    chat_id = pending.get("chat_id")
    text = pending.get("text")
    if not chat_id or not text:
        return
    try:
        await bot.send_message(int(chat_id), str(text))
        logger.info("Отправлено уведомление после перезапуска в chat_id=%s", chat_id)
    except Exception as e:
        logger.warning("Не удалось отправить уведомление после перезапуска: %s", e)


def _parse_telegram_proxy(value: str) -> str | tuple[str, BasicAuth]:
    """
    Парсит TELEGRAM_PROXY:
    - socks5://user:pass@host:port (полный URL)
    - host:port:user:pass (короткий формат)
    - host:port (без авторизации)
    """
    if "://" in value:
        return value

    parts = value.split(":")
    if len(parts) == 4:
        host, port, login, password = parts
        return (f"{TELEGRAM_PROXY_TYPE}://{host}:{port}", BasicAuth(login=login, password=password))
    if len(parts) == 2:
        host, port = parts
        return f"{TELEGRAM_PROXY_TYPE}://{host}:{port}"

    return f"{TELEGRAM_PROXY_TYPE}://{value}"


def _create_bot_session() -> AiohttpSession | None:
    """Создаёт aiohttp-сессию с прокси, если задан TELEGRAM_PROXY."""
    if not TELEGRAM_PROXY:
        return None

    proxy = _parse_telegram_proxy(TELEGRAM_PROXY)
    logger.info("Telegram API через прокси (%s)", TELEGRAM_PROXY_TYPE)
    return AiohttpSession(proxy=proxy)


# Текущая директория пользователя: user_id -> Path
_user_cwd: dict[int, Path] = {}
# Пользовательские промпты: user_id -> str
_user_prompts: dict[int, str] = {}


def _get_unique_file_path(directory: Path, filename: str) -> Path:
    """Возвращает уникальный путь для файла (добавляет _1, _2, ... при коллизии)."""
    path = directory / filename
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        path = directory / f"{stem}_{i}{suffix}"
        if not path.exists():
            return path
        i += 1


def _get_user_cwd(user_id: int) -> Path:
    """Текущая рабочая директория пользователя."""
    return _user_cwd.get(user_id, WORKSPACE_DIR)


def _set_user_cwd(user_id: int, path: Path) -> None:
    """Установить рабочую директорию пользователя."""
    _user_cwd[user_id] = path


def _resolve_path(user_id: int, path_str: str) -> Path | None:
    """
    Разрешает путь относительно текущей директории пользователя.
    Возвращает None если путь выходит за пределы WORKSPACE_DIR.
    """
    base = _get_user_cwd(user_id)
    path = (base / path_str).resolve()
    try:
        path.relative_to(WORKSPACE_DIR)
    except ValueError:
        return None
    return path


def _parse_schedule_reminders(
    text: str,
    user_id: int,
    chat_id: int,
) -> tuple[str, list[str]]:
    """
    Извлекает schedule_reminder::ISO_DATETIME::заголовок::контекст из текста агента.
    Создаёт записи в БД. Возвращает (текст без директив, список подтверждений).
    """
    if not SCHEDULER_ENABLED:
        return text, []

    confirmations: list[str] = []
    remaining = text
    pattern = re.compile(r"schedule_reminder::([^\n]+)")

    for m in pattern.finditer(text):
        full = m.group(0)
        rest = m.group(1)
        parts = rest.split("::", 2)
        if len(parts) < 2:
            remaining = remaining.replace(full, "")
            continue
        dt_str, title = parts[0].strip(), parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        try:
            remind_at = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            event = create_event(user_id, chat_id, title, remind_at, body)
            at_fmt = event.remind_at.strftime("%d.%m.%Y %H:%M UTC")
            confirmations.append(f"⏰ Напоминание #{event.id} на {at_fmt}: {title}")
        except Exception as e:
            logger.warning("Не удалось создать напоминание: %s — %s", rest[:80], e)
            confirmations.append(f"⛔ Не удалось запланировать: {title or dt_str} ({e})")
        remaining = remaining.replace(full, "")

    remaining = re.sub(r"\n{3,}", "\n\n", remaining.strip())
    return remaining, confirmations


def _parse_remind_args(args: str) -> tuple[datetime, str] | None:
    """
    Парсит аргументы /remind:
    +30m текст | +2h текст | +1d текст
    2025-06-09 10:00 текст | 2025-06-09T10:00 текст
    09.06.2025 10:00 текст
    """
    text = args.strip()
    if not text:
        return None

    rel = re.match(r"^\+(\d+)([mhdMHD])\s+(.+)$", text, re.DOTALL)
    if rel:
        amount, unit, title = int(rel.group(1)), rel.group(2).lower(), rel.group(3).strip()
        if not title:
            return None
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}
        if unit not in delta:
            return None
        return datetime.now(timezone.utc).replace(tzinfo=None) + delta[unit], title

    iso = re.match(
        r"^(\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2})?)\s+(.+)$",
        text,
        re.DOTALL,
    )
    if iso:
        dt_str, title = iso.group(1).replace(" ", "T"), iso.group(2).strip()
        if not title:
            return None
        try:
            return datetime.fromisoformat(dt_str), title
        except ValueError:
            return None

    dmy = re.match(r"^(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2}(?::\d{2})?)\s+(.+)$", text, re.DOTALL)
    if dmy:
        date_part, time_part, title = dmy.group(1), dmy.group(2), dmy.group(3).strip()
        if not title:
            return None
        try:
            fmt = "%d.%m.%Y %H:%M:%S" if time_part.count(":") == 2 else "%d.%m.%Y %H:%M"
            return datetime.strptime(f"{date_part} {time_part}", fmt), title
        except ValueError:
            return None

    return None


def _parse_send_document(text: str) -> tuple[str, list[tuple[Path, str, str]]]:
    """
    Извлекает send_document::path::name::caption из текста.
    Возвращает (текст без этих строк, список (path, name, caption)).
    """
    docs: list[tuple[Path, str, str]] = []
    pattern = re.compile(r"send_document::([^\n]+)")
    remaining = text

    for m in pattern.finditer(text):
        full = m.group(0)
        rest = m.group(1)
        parts = rest.split("::", 2)
        if len(parts) >= 2:
            path_str, name = parts[0], parts[1]
            caption = parts[2] if len(parts) > 2 else ""
            try:
                docs.append((Path(path_str.strip()), name.strip(), caption.strip()))
            except Exception:
                pass
        remaining = remaining.replace(full, "")

    remaining = re.sub(r"\n{3,}", "\n\n", remaining.strip())
    return remaining, docs


_INLINE_URL_PREFIXES = ("http://", "https://", "tg://")


def _parse_button_pair(raw: str) -> tuple[str, str] | None:
    """Парсит пару url::текст для inline-кнопки."""
    parts = raw.split("::", 1)
    if len(parts) != 2:
        return None
    url, label = parts[0].strip(), parts[1].strip()
    if not url or not label:
        return None
    if not url.startswith(_INLINE_URL_PREFIXES):
        return None
    return url, label


def _parse_inline_buttons(text: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """
    Извлекает директивы inline-кнопок из текста.
    inline_button::url::текст — одна кнопка в отдельном ряду
    inline_button_row::url::текст;;url::текст — несколько кнопок в одном ряду
    Возвращает (текст без директив, список рядов кнопок).
    """
    rows: list[list[tuple[str, str]]] = []
    remaining = text

    for m in re.finditer(r"inline_button_row::([^\n]+)", text):
        row: list[tuple[str, str]] = []
        for part in m.group(1).split(";;"):
            pair = _parse_button_pair(part.strip())
            if pair:
                row.append(pair)
        if row:
            rows.append(row)
        remaining = remaining.replace(m.group(0), "")

    for m in re.finditer(r"inline_button::([^\n]+)", remaining):
        pair = _parse_button_pair(m.group(1))
        if pair:
            rows.append([pair])
        remaining = remaining.replace(m.group(0), "")

    remaining = re.sub(r"\n{3,}", "\n\n", remaining.strip())
    return remaining, rows


def _build_inline_markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    """Собирает InlineKeyboardMarkup из рядов (url, текст)."""
    if not rows:
        return None
    keyboard = [
        [InlineKeyboardButton(text=label, url=url) for url, label in row]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


MSG_SPLIT_SEP = ";;;"
_TELEGRAM_HTML_TAGS = frozenset(
    {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-emoji", "span", "em", "strong"}
)
_VOID_HTML_TAGS = frozenset({"br", "hr", "img"})


def _html_tag_name(raw: str) -> str | None:
    """Имя тега из содержимого <...> или None."""
    raw = raw.strip()
    if not raw or raw.startswith("!"):
        return None
    if raw.startswith("/"):
        raw = raw[1:].strip()
    name = raw.split(None, 1)[0].lower().rstrip("/")
    if name in _VOID_HTML_TAGS or raw.endswith("/"):
        return None
    return name or None


def _balance_html_tags(text: str) -> str:
    """Закрывает незакрытые Telegram HTML-теги в фрагменте."""
    open_stack: list[str] = []
    parts: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "<":
            parts.append(text[i])
            i += 1
            continue
        end = text.find(">", i)
        if end == -1:
            parts.append(text[i:])
            break
        tag_chunk = text[i : end + 1]
        parts.append(tag_chunk)
        inner = text[i + 1 : end]
        if inner.startswith("/"):
            name = _html_tag_name(inner)
            if name and open_stack and open_stack[-1] == name:
                open_stack.pop()
        else:
            name = _html_tag_name(inner)
            if name and (name in _TELEGRAM_HTML_TAGS or name.startswith("tg-")):
                open_stack.append(name)
        i = end + 1
    for name in reversed(open_stack):
        parts.append(f"</{name}>")
    return "".join(parts)


def _strip_html_tags(text: str) -> str:
    """Убирает HTML-теги, оставляет текст."""
    return re.sub(r"<[^>]*>", "", text)


def _get_open_html_tags(text: str) -> list[str]:
    """Возвращает стек незакрытых HTML-тегов в фрагменте."""
    open_stack: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "<":
            i += 1
            continue
        end = text.find(">", i)
        if end == -1:
            break
        inner = text[i + 1 : end]
        if inner.startswith("/"):
            name = _html_tag_name(inner)
            if name and open_stack and open_stack[-1] == name:
                open_stack.pop()
        else:
            name = _html_tag_name(inner)
            if name and (name in _TELEGRAM_HTML_TAGS or name.startswith("tg-")):
                open_stack.append(name)
        i = end + 1
    return open_stack


def _prepend_open_tags(text: str, tags: list[str]) -> str:
    if not tags:
        return text
    return "".join(f"<{t}>" for t in tags) + text


def _split_long_text(text: str, max_len: int = MAX_RESPONSE_LENGTH) -> list[str]:
    """
    Разбивает длинный текст на части ≤ max_len.
    Не режет внутри HTML-тегов; переносит открытые теги в следующую часть.
    """
    text = text.strip()
    if not text:
        return ["(пустой ответ)"]
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    carry_tags: list[str] = []

    while remaining:
        prefix = _prepend_open_tags("", carry_tags)
        effective_max = max_len - len(prefix)

        if len(remaining) <= effective_max:
            chunks.append(_balance_html_tags((prefix + remaining).strip()))
            break

        cut = effective_max
        nl = remaining.rfind("\n", 0, cut)
        if nl > cut // 2:
            cut = nl + 1

        lt = remaining.rfind("<", 0, cut)
        gt = remaining.rfind(">", 0, cut)
        if lt > gt:
            cut = lt

        # Учитываем закрывающие теги, которые добавит _balance_html_tags
        while cut > 0:
            piece = remaining[:cut]
            full_chunk = prefix + piece
            balanced = _balance_html_tags(full_chunk.strip())
            if len(balanced) <= max_len:
                carry_tags = _get_open_html_tags(balanced)
                chunks.append(balanced)
                remaining = remaining[cut:].lstrip("\n")
                break
            cut -= max(1, len(balanced) - max_len)
        else:
            # Крайний случай: один символ
            piece = remaining[:1]
            balanced = _balance_html_tags((prefix + piece).strip())
            carry_tags = _get_open_html_tags(balanced)
            chunks.append(balanced)
            remaining = remaining[1:].lstrip("\n")

    return chunks or ["(пустой ответ)"]


def _split_response_messages(text: str) -> list[str]:
    """
    Разбивает текст по ;;; на отдельные сообщения.
    Не режет внутри HTML-тегов — иначе Telegram падает на незакрытом <code> и т.п.
    """
    if MSG_SPLIT_SEP not in text:
        stripped = text.strip()
        return [stripped] if stripped else ["(пустой ответ)"]

    parts: list[str] = []
    current: list[str] = []
    open_tags: list[str] = []
    i = 0
    length = len(text)

    while i < length:
        if text.startswith(MSG_SPLIT_SEP, i) and not open_tags:
            chunk = "".join(current).strip()
            if chunk:
                parts.append(_balance_html_tags(chunk))
            current = []
            i += len(MSG_SPLIT_SEP)
            continue

        if text[i] == "<":
            end = text.find(">", i)
            if end == -1:
                current.append(text[i])
                i += 1
                continue
            tag_chunk = text[i : end + 1]
            current.append(tag_chunk)
            inner = text[i + 1 : end]
            if inner.startswith("/"):
                name = _html_tag_name(inner)
                if name and open_tags and open_tags[-1] == name:
                    open_tags.pop()
            else:
                name = _html_tag_name(inner)
                if name and (name in _TELEGRAM_HTML_TAGS or name.startswith("tg-")):
                    open_tags.append(name)
            i = end + 1
            continue

        current.append(text[i])
        i += 1

    chunk = "".join(current).strip()
    if chunk:
        parts.append(_balance_html_tags(chunk))

    return parts if parts else ["(пустой ответ)"]


def _sanitize_prompt_for_cli(text: str) -> str:
    """
    cursor-agent ошибочно парсит строки с '---' как CLI-флаги.
    Убираем опасные префиксы перед передачей в --print.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("---"):
            line = line.replace("---", "###", 1)
        lines.append(line)
    return "\n".join(lines)


async def _send_one_message(
    target: Message,
    text: str,
    message: Message,
    bot: Bot,
    edit: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """
    Отправляет одно сообщение (edit или answer). При ошибке — логирует и отправляет отчёт.
    Возвращает True при успехе. Длинный текст должен быть разбит вызывающим кодом.
    """
    text = _balance_html_tags(text or "(пустой ответ)")

    async def _do_send(body: str, *, use_html: bool = True) -> None:
        kwargs: dict = {}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if not use_html:
            kwargs["parse_mode"] = None
        if edit:
            await target.edit_text(body, **kwargs)
        else:
            await message.answer(body, **kwargs)

    try:
        await _do_send(text)
        return True
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "parse entities" in err_msg or "can't parse" in err_msg:
            fixed = _balance_html_tags(text)
            if fixed != text:
                try:
                    await _do_send(fixed)
                    return True
                except TelegramBadRequest:
                    pass
            try:
                await _do_send(_strip_html_tags(text), use_html=False)
                return True
            except TelegramBadRequest:
                pass

        err_name = type(e).__name__
        logger.error("Ошибка отправки %s, текст: %s", err_name, text[:500])
        ERROR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        update_id = getattr(message, "message_id", 0)
        report_path = ERROR_REPORTS_DIR / f"error_{ts}_{update_id}.txt"
        content = (
            f"Ошибка: {err_name}\n{err_msg}\n\n--- Текст ---\n{text}\n\n--- Traceback ---\n{traceback.format_exc()}"
        )
        report_path.write_text(content, encoding="utf-8")
        try:
            if edit:
                await target.edit_text(f"⛔ Ошибка отправки: {err_name}")
            await bot.send_document(
                chat_id=message.chat.id,
                document=FSInputFile(report_path, filename=report_path.name),
                caption="Отчёт об ошибке",
            )
        except Exception as send_err:
            logger.exception("Не удалось отправить отчёт: %s", send_err)
        return False
    except Exception as e:
        logger.exception("Ошибка при отправке: %s", e)
        try:
            await message.answer(f"⛔ Ошибка: {type(e).__name__}")
        except Exception:
            pass
        return False


async def _deliver_agent_text(bot: Bot, chat_id: int, response: str) -> None:
    """Отправляет текст агента в чат (напоминания, проактивные уведомления)."""
    remaining_text, send_docs = _parse_send_document(response)
    for path, name, caption in send_docs:
        try:
            p = Path(path)
            if p.is_file():
                await bot.send_document(
                    chat_id=chat_id,
                    document=FSInputFile(p, filename=name),
                    caption=caption or None,
                )
        except Exception as e:
            logger.warning("Не удалось отправить файл %s: %s", path, e)

    parts = _split_response_messages(remaining_text)
    send_count = 0
    for part in parts:
        clean_part, button_rows = _parse_inline_buttons(part)
        reply_markup = _build_inline_markup(button_rows)
        chunks = _split_long_text(clean_part)
        for j, chunk in enumerate(chunks):
            markup = reply_markup if j == 0 else None
            text = _balance_html_tags(chunk or "(пустой ответ)")
            try:
                await bot.send_message(chat_id, text, reply_markup=markup)
            except TelegramBadRequest:
                try:
                    await bot.send_message(chat_id, _strip_html_tags(text), parse_mode=None)
                except Exception as e:
                    logger.error("Не удалось отправить проактивное сообщение: %s", e)
            send_count += 1
            if send_count > 1:
                await asyncio.sleep(0.3)


async def _send_response(
    status_msg: Message,
    response: str,
    message: Message,
    bot: Bot,
) -> None:
    """
    Отправляет ответ в Telegram. Поддерживает split по ;;; — несколько сообщений.
    При ошибке (ENTITY_TEXT_INVALID и др.):
    логирует, пишет в файл, отправляет файл пользователю и короткое сообщение.
    """
    parts = _split_response_messages(response)
    is_first_send = True
    for part in parts:
        clean_part, button_rows = _parse_inline_buttons(part)
        reply_markup = _build_inline_markup(button_rows)
        chunks = _split_long_text(clean_part)
        for j, chunk in enumerate(chunks):
            markup = reply_markup if j == 0 else None
            await _send_one_message(
                status_msg,
                chunk,
                message,
                bot,
                edit=is_first_send,
                reply_markup=markup,
            )
            if not is_first_send:
                await asyncio.sleep(0.3)
            is_first_send = False


def _load_user_prompts() -> None:
    """Загрузить пользовательские промпты из файла."""
    global _user_prompts
    try:
        if USER_PROMPTS_FILE.exists():
            data = json.loads(USER_PROMPTS_FILE.read_text(encoding="utf-8"))
            _user_prompts = {int(k): v for k, v in data.items() if isinstance(v, str)}
    except Exception as e:
        logger.warning("Не удалось загрузить user_prompts: %s", e)


def _save_user_prompts() -> None:
    """Сохранить пользовательские промпты в файл."""
    try:
        USER_PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_PROMPTS_FILE.write_text(
            json.dumps({str(k): v for k, v in _user_prompts.items()}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Не удалось сохранить user_prompts: %s", e)


def _get_default_prompt() -> str:
    """Загрузить глобальный промпт из файла или env."""
    env_prompt = os.getenv("DEFAULT_PROMPT", "").strip()
    if env_prompt:
        return env_prompt
    try:
        if DEFAULT_PROMPT_FILE.exists():
            return DEFAULT_PROMPT_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("Не удалось загрузить default_prompt: %s", e)
    return ""


def _set_default_prompt(text: str) -> None:
    """Сохранить глобальный промпт в файл."""
    try:
        DEFAULT_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_PROMPT_FILE.write_text(text.strip(), encoding="utf-8")
    except Exception as e:
        logger.warning("Не удалось сохранить default_prompt: %s", e)
        raise


def _get_user_prompt(user_id: int) -> str:
    """Получить пользовательский промпт (пустая строка если нет)."""
    return _user_prompts.get(user_id, "")


def _set_user_prompt(user_id: int, text: str) -> None:
    """Установить пользовательский промпт."""
    if text.strip():
        _user_prompts[user_id] = text.strip()
    elif user_id in _user_prompts:
        del _user_prompts[user_id]
    _save_user_prompts()


def _parse_stream_status(line: str) -> str | None:
    """Извлекает короткий статус из строки stream-json для отображения в Telegram."""
    try:
        data = json.loads(line)
        t = data.get("type")
        if t == "tool_call" and data.get("subtype") == "started":
            tc = data.get("tool_call", {})
            if "shellToolCall" in tc:
                cmd = tc["shellToolCall"].get("args", {}).get("command", "")[:40]
                return f"💻 Выполняю: {cmd}..." if len(cmd) >= 40 else f"💻 Выполняю: {cmd}"
            if "readToolCall" in tc:
                path = tc["readToolCall"].get("args", {}).get("path", "файл")
                return f"📖 Читаю: {path}"
            if "editToolCall" in tc:
                path = tc["editToolCall"].get("args", {}).get("path", "файл")
                return f"✏️ Редактирую: {path}"
            if "writeToolCall" in tc:
                path = tc["writeToolCall"].get("args", {}).get("path", "файл")
                return f"📝 Пишу: {path}"
            if "grepToolCall" in tc:
                return "🔍 Поиск по файлам..."
            if "lsToolCall" in tc:
                return "📂 Просмотр директории..."
            if "globToolCall" in tc:
                return "🔍 Поиск файлов..."
            return "🔧 Работаю..."
        if t == "assistant":
            return "💭 Пишу ответ..."
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


async def run_cursor_agent_streaming(
    prompt: str,
    cwd: Path,
    *,
    session_key: str | None = None,
    status_msg: Message | None = None,
) -> tuple[str, bool]:
    """
    Запускает cursor-agent со stream-json, обновляет status_msg по ходу выполнения.
    session_key — ключ сессии (user:123, scheduler); None — разовый запуск без resume.
    Возвращает (ответ, успех).
    """
    if not CURSOR_API_KEY:
        return (
            "❌ CURSOR_API_KEY не настроен. Добавьте в .env ключ с https://cursor.com/dashboard?tab=background-agents",
            False,
        )

    env = os.environ.copy()
    env["CURSOR_API_KEY"] = CURSOR_API_KEY

    chat_id: str | None = None
    if session_key:
        chat_id = await ensure_chat_id(session_key, cwd, env)

    cmd = [CURSOR_CLI_PATH, "--model", CURSOR_MODEL, "--force", "--output-format", "stream-json"]
    if chat_id:
        cmd.extend(["--resume", chat_id])
    cmd.extend(["--print", _sanitize_prompt_for_cli(prompt)])

    last_status = '<tg-emoji emoji-id="5210764626857313664">🤖</tg-emoji> Инициализация...'
    last_edit_time = [0.0]  # mutable для доступа из вложенной функции
    STATUS_DEBOUNCE = 2.0  # секунд между обновлениями Telegram
    assistant_parts: list[str] = []

    async def _run() -> tuple[str, bool]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        assert proc.stdout
        buffer = ""
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                status = _parse_stream_status(line)
                if status:
                    last_status = status
                    if status_msg is not None:
                        now = time.monotonic()
                        if now - last_edit_time[0] >= STATUS_DEBOUNCE:
                            try:
                                await status_msg.edit_text(f"⏳ {last_status}", parse_mode=None)
                                last_edit_time[0] = now
                            except Exception:
                                pass
                try:
                    data = json.loads(line)
                    if data.get("type") == "assistant":
                        content = data.get("message", {}).get("content", [])
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                assistant_parts.append(c.get("text", ""))
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

        stderr = await proc.stderr.read() if proc.stderr else b""
        await proc.wait()

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500]
            return f"❌ Ошибка Cursor CLI:\n```\n{err}\n```", False

        output = "".join(assistant_parts).strip() or "(пустой ответ)"
        return output, True

    try:
        return await asyncio.wait_for(_run(), timeout=CURSOR_TIMEOUT)
    except asyncio.TimeoutError:
        return f"⏱ Превышено время ожидания ({CURSOR_TIMEOUT} сек)", False
    except FileNotFoundError:
        return (
            f"❌ Cursor CLI не найден. Проверьте CURSOR_CLI_PATH (сейчас: {CURSOR_CLI_PATH})",
            False,
        )
    except Exception as e:
        logger.exception("Ошибка при вызове cursor-agent")
        return f"❌ Ошибка: {str(e)}", False


async def _finalize_bot_changes(
    response: str,
    user_text: str,
    status_msg: Message,
    message: Message,
) -> bool:
    """
    Если агент изменил код бота — коммит и перезапуск.
    Возвращает True, если запланирован перезапуск.
    """
    if not self_modify_enabled() or not repo_ready():
        return False
    if codeword_required() and not has_codeword(user_text):
        return False
    if not has_bot_code_changes():
        return False

    py_ok, py_err = validate_python_files()
    if not py_ok:
        git_discard_worktree()
        await message.answer(
            f"⛔ Синтаксическая ошибка в коде бота, изменения отменены:\n<pre>{html.escape(py_err[:2000])}</pre>"
        )
        return False

    commit_msg = parse_commit_message(response) or (
        f"bot: {user_text[:80].replace(chr(10), ' ')}"
    )
    committed, commit_result = git_commit(commit_msg, get_bot_code_paths())

    note = _format_commit_push_notes(committed, commit_msg, commit_result)
    await message.answer(f"✅ <b>Код бота обновлён</b>\n{note}\n🔄 Перезапуск через 3 сек...")
    asyncio.create_task(
        schedule_restart(3.0, chat_id=message.chat.id, notify_text=_RESTART_NOTIFY_TEXT)
    )
    return True


async def _run_self_fix(
    error_text: str,
    user_id: int,
    bot: Bot,
    chat_id: int,
    user_hint: str = "",
    auto: bool = False,
    skip_codeword_check: bool = False,
) -> None:
    """Запускает cursor-agent для исправления кода бота, коммитит и перезапускается."""
    if not self_modify_enabled():
        await bot.send_message(chat_id, "⛔ Самомодификация отключена (SELF_MODIFY_ENABLED=false).")
        return

    if not skip_codeword_check:
        check_text = f"{user_hint}\n{error_text}".strip()
        ok, reason = check_codeword(check_text)
        if not ok:
            if auto:
                logger.warning("Автофикс пропущен: %s", reason)
                return
            await bot.send_message(chat_id, f"⛔ {html.escape(reason)}")
            return

    if not repo_ready():
        await bot.send_message(
            chat_id,
            "⛔ Репозиторий не смонтирован. Добавьте volume `.:/workspace/cursor_cli_agent` в docker-compose.",
        )
        return

    if auto:
        ok, reason = can_auto_fix()
        if not ok:
            logger.warning("Автофикс пропущен: %s", reason)
            return

    async with _self_fix_lock:
        status = await bot.send_message(
            chat_id,
            "🔧 <b>Самоисправление</b>\nАнализирую ошибку и правлю код бота...",
        )

        prompt = build_self_fix_prompt(error_text, user_hint)
        cwd = Path(os.getenv("BOT_REPO_DIR", "/workspace/cursor_cli_agent"))
        if not cwd.is_dir():
            cwd = WORKSPACE_DIR

        response, success = await run_cursor_agent_streaming(
            prompt,
            cwd,
            session_key=None,
            status_msg=status,
        )

        if not success:
            await status.edit_text(f"⛔ Агент не смог исправить:\n<pre>{html.escape(response[:3000])}</pre>")
            return

        py_ok, py_err = validate_python_files()
        if not py_ok:
            git_discard_worktree()
            await status.edit_text(
                f"⛔ Синтаксическая ошибка после правок, изменения отменены:\n<pre>{html.escape(py_err[:2000])}</pre>"
            )
            return

        commit_msg = parse_commit_message(response) or (
            f"bot: auto-fix — {error_text[:80].replace(chr(10), ' ')}"
        )
        committed, commit_result = git_commit(commit_msg, get_bot_code_paths())

        if auto:
            record_auto_fix()

        summary = response
        for marker in ("SELF_MODIFY_COMMIT::",):
            if marker in summary:
                summary = summary.split(marker)[0].strip()

        parts = [
            "✅ <b>Код бота обновлён</b>",
            f"<pre>{html.escape(summary[:2500])}</pre>",
        ]
        parts.append(_format_commit_push_notes(committed, commit_msg, commit_result))
        parts.append("🔄 Перезапуск через 3 сек...")

        await status.edit_text("\n\n".join(parts))
        asyncio.create_task(
            schedule_restart(3.0, chat_id=chat_id, notify_text=_RESTART_NOTIFY_TEXT)
        )


def is_allowed(user_id: int) -> bool:
    """Проверка доступа пользователя."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def _guest_session_key(user_id: int) -> str:
    return f"guest:{user_id}"


_GUEST_RESET_RE = re.compile(r"(?:^|\s)/?(?:new|reset|сброс)(?:\s|$)", re.I)


def _display_name(entity) -> str:
    """Имя User или Chat (first_name / title / username)."""
    if not entity:
        return "неизвестно"
    parts: list[str] = []
    if getattr(entity, "first_name", None):
        parts.append(entity.first_name)
    if getattr(entity, "last_name", None):
        parts.append(entity.last_name)
    name = " ".join(parts).strip()
    username = getattr(entity, "username", None)
    if username:
        name = f"{name} (@{username})" if name else f"@{username}"
    if name:
        return name
    title = getattr(entity, "title", None)
    if title:
        return title
    return f"id:{entity.id}"


def _format_guest_chat_context(message: Message) -> str:
    """Контекст чата для Guest Mode: кто спросил и с кем переписка."""
    lines: list[str] = []
    asker = message.from_user
    if asker:
        lines.append(f"Спросил: {_display_name(asker)}")

    chat = message.chat
    if chat:
        if chat.type == "private" and asker and chat.id != asker.id:
            lines.append(f"Собеседник в переписке: {_display_name(chat)}")
        elif chat.type in ("group", "supergroup"):
            lines.append(f"Групповой чат: {_display_name(chat)}")
        elif chat.type == "private":
            lines.append("Чат: личная переписка")

    reply = message.reply_to_message
    if reply and reply.from_user:
        lines.append(f"Ответ на сообщение от: {_display_name(reply.from_user)}")

    return "\n".join(lines)


def _is_guest_reset_query(text: str) -> bool:
    """Сброс guest-контекста: бурмалда, /new бурмалда или reset/сброс с кодовым словом."""
    if not has_codeword(text):
        return False
    if _GUEST_RESET_RE.search(text):
        return True
    remainder = text.lower().strip()
    for word in SELF_MODIFY_CODEWORDS:
        remainder = remainder.replace(word, "")
    remainder = re.sub(r"[\s/.,!?]+", "", remainder)
    return remainder == ""


def _strip_bot_mention(text: str) -> str:
    """Убирает @username бота из текста."""
    if not text:
        return ""
    if _bot_username:
        text = re.sub(rf"@{re.escape(_bot_username)}\b", "", text, flags=re.I)
    return re.sub(r"@\w+\s*", "", text, count=1).strip()


def _clean_external_response(text: str) -> str:
    """Убирает служебные директивы из ответа для guest/inline."""
    cleaned = text
    for marker in ("SELF_MODIFY_COMMIT::", "schedule_reminder::"):
        if marker in cleaned:
            cleaned = cleaned.split(marker)[0]
    cleaned, _ = _parse_send_document(cleaned)
    cleaned, _ = _parse_inline_buttons(cleaned)
    cleaned = re.sub(r"inline_button(?:_row)?::[^\n]+", "", cleaned)
    return cleaned.strip() or "(пустой ответ)"


def _truncate_telegram(text: str, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _build_external_prompt(
    user_id: int,
    query: str,
    *,
    source: str,
    chat_context: str = "",
) -> tuple[str, Path]:
    """Промпт для guest/inline — ответ для чужого чата, без самомодификации."""
    session_key = _guest_session_key(user_id)
    parts: list[str] = []
    default_prompt = _get_default_prompt()
    if not has_agent_session(session_key) and default_prompt:
        parts.append(default_prompt)
    user_prompt = _get_user_prompt(user_id)
    if user_prompt:
        parts.append(f"[Информация от пользователя]\n{user_prompt}\n[/Информация от пользователя]")
    if chat_context:
        parts.append(f"[Контекст чата]\n{chat_context}\n[/Контекст чата]")
    parts.append(
        f"[{source}]\n"
        "Пользователь задал вопрос в переписке с другим человеком. "
        "Ответь кратко, понятно и по делу — его увидят оба собеседника. "
        "Учитывай имена из контекста чата, если они указаны. "
        "Не меняй код бота и не используй служебные директивы.\n"
        f"Вопрос: {query}"
    )
    return "\n\n".join(parts), WORKSPACE_DIR


async def _run_external_agent(
    user_id: int,
    query: str,
    *,
    source: str,
    chat_context: str = "",
) -> tuple[str, bool]:
    prompt, agent_cwd = _build_external_prompt(
        user_id, query, source=source, chat_context=chat_context
    )
    response, success = await run_cursor_agent_streaming(
        prompt,
        agent_cwd,
        session_key=_guest_session_key(user_id),
        status_msg=None,
    )
    return _truncate_telegram(_clean_external_response(response)), success


def _make_article_result(result_id: str, title: str, text: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=title[:64],
        description=title[:128] if len(title) > 64 else None,
        input_message_content=InputTextMessageContent(
            message_text=_balance_html_tags(_truncate_telegram(text)),
            parse_mode=ParseMode.HTML,
        ),
    )


def _format_user_info(user) -> str:
    """Форматирует информацию о пользователе для отправки."""
    parts = [
        f"🆔 <b>ID:</b> <code>{user.id}</code>",
        f"👤 <b>Имя:</b> {html.escape(user.first_name or '')}",
    ]
    if user.last_name:
        parts.append(f"👤 <b>Фамилия:</b> {html.escape(user.last_name)}")
    if user.username:
        parts.append(f"📛 <b>Username:</b> @{html.escape(user.username)}")
    if user.language_code:
        parts.append(f"🌐 <b>Язык:</b> {html.escape(user.language_code)}")
    return "\n".join(parts)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Команда /start."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    user_info = _format_user_info(message.from_user)
    await message.answer(
        "👋 <b>Cursor CLI Bot</b>\n\n"
        f"{user_info}\n\n"
        "Отправь сообщение — я передам его Cursor Agent и пришлю ответ.\n"
        "Контекст сохраняется между сообщениями.\n\n"
        "<b>В любом чате (в т.ч. с друзьями):</b>\n"
        "• Упомяни @бота в сообщении (Guest Mode)\n"
        "• Или набери @бота в поле ввода и выбери результат (Inline Mode)\n"
        "• @бот бурмалда — сброс контекста Guest Mode (или /new бурмалда)\n\n"
        "<b>Команды:</b>\n"
        "/start — это сообщение\n"
        "/new — сбросить контекст, начать новый диалог\n"
        "/status — проверка подключения\n"
        "/help — справка\n"
        "/set_prompt — задать свой промпт для агента\n"
        "/myprompt — показать свой промпт\n"
        "/clear_prompt — очистить свой промпт\n"
        "/get_global_prompt — показать глобальный промпт\n"
        "/set_global_prompt — задать глобальный промпт\n"
        "/self_fix [описание] — попросить бота исправить свой код\n"
        "/bot_git_status — статус git (изменения бота)\n"
        "/bot_git_log — последние коммиты бота\n"
        "/bot_rollback — откатить последний коммит бота\n"
        "/remind &lt;когда&gt; &lt;текст&gt; — напоминание напрямую\n"
        "/reminders — список запланированных напоминаний\n"
        "/cancel_reminder &lt;id&gt; — отменить напоминание",
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Команда /status."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    has_key = "✅" if CURSOR_API_KEY else "❌"
    has_mem0 = "✅" if MEM0_API_KEY else "❌"
    workspace_exists = "✅" if WORKSPACE_DIR.exists() else "❌"
    self_mod = "✅" if self_modify_enabled() else "❌"
    git_ok = "✅" if repo_ready() else "❌"

    await message.answer(
        f"📊 <b>Статус</b>\n\n"
        f"CURSOR_API_KEY: {has_key}\n"
        f"MEM0_API_KEY: {has_mem0}\n"
        f"Рабочая директория: {workspace_exists} (<code>{WORKSPACE_DIR}</code>)\n"
        f"Cursor CLI: <code>{CURSOR_CLI_PATH}</code>\n"
        f"Модель: <code>{CURSOR_MODEL}</code>\n"
        f"Самомодификация: {self_mod}\n"
        f"Git-репозиторий: {git_ok}\n"
        f"Автофикс ошибок: {'✅' if SELF_MODIFY_AUTO_FIX else '❌'}\n"
        f"Кодовое слово: {'✅ обязательно' if codeword_required() else '❌ выкл'} "
        f"({', '.join(SELF_MODIFY_CODEWORDS)})\n"
        f"Планировщик: {'✅' if SCHEDULER_ENABLED else '❌'}",
    )


@dp.message(Command("new", "reset"))
async def cmd_new(message: Message) -> None:
    """Команда /new — сброс контекста."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    clear_chat_id(user_session_key(message.from_user.id))
    await message.answer(
        "🔄 Контекст сброшен. Следующее сообщение начнёт новый диалог.",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Команда /help."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    await message.answer(
        "📖 *Справка*\n\n"
        "Просто напиши задачу на естественном языке, например:\n"
        '• "Найди баги в main.py"\n'
        '• "Добавь обработку ошибок в api"\n'
        '• "Объясни что делает функция parse"\n\n'
        "<b>В переписке с кем угодно:</b>\n"
        "• @бот что такое дуги на брекитах — Guest Mode (ответ прямо в чат)\n"
        "• @бот бурмалда — сброс контекста Guest Mode (или /new бурмалда)\n"
        "• @бот в поле ввода → выбрать результат — Inline Mode\n"
        "Нужно включить Guest Mode и Inline Mode в @BotFather.\n\n"
        "Системные команды: /cd, /pwd, /ls, /mkdir, /cat, /rm\n\n"
        "Обновление бота — напиши «бурмалда» в сообщении, бот сам поймёт нужна ли правка кода.\n"
        "/self_fix [описание] — принудительное самоисправление (без кодового слова)\n"
        "/bot_git_status — изменения в git\n"
        "/bot_git_log — история коммитов\n"
        "/bot_rollback [N] — откат N коммитов\n\n"
        "Напоминания: «напомни мне завтра в 10 про ...» или команда /remind.\n"
        "Примеры: /remind +30m выпить воды | /remind 2025-06-09 10:00 деплой\n"
        "/reminders — список, /cancel_reminder &lt;id&gt; — отмена",
        parse_mode=ParseMode.MARKDOWN,
    )


@dp.message(Command("self_fix"))
async def cmd_self_fix(message: Message, command: CommandObject) -> None:
    """Команда /self_fix — ручной запуск самоисправления."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    hint = (command.args or "").strip()
    error_text = hint or "Пользователь запросил улучшение/исправление кода бота."
    asyncio.create_task(
        _run_self_fix(
            error_text,
            message.from_user.id,
            message.bot,
            message.chat.id,
            user_hint=hint,
            auto=False,
            skip_codeword_check=True,
        )
    )


@dp.message(Command("bot_git_status"))
async def cmd_bot_git_status(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(f"<pre>{html.escape(git_status_short())}</pre>")


@dp.message(Command("bot_git_log"))
async def cmd_bot_git_log(message: Message, command: CommandObject) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    limit = 10
    if command.args and command.args.strip().isdigit():
        limit = min(int(command.args.strip()), 30)
    await message.answer(f"<pre>{html.escape(git_log(limit))}</pre>")


@dp.message(Command("remind"))
async def cmd_remind(message: Message, command: CommandObject) -> None:
    """Команда /remind — создать напоминание без агента."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    if not SCHEDULER_ENABLED:
        await message.answer("⛔ Планировщик отключён.")
        return

    args = (command.args or "").strip()
    parsed = _parse_remind_args(args)
    if not parsed:
        await message.answer(
            "Использование:\n"
            "<code>/remind +30m текст</code>\n"
            "<code>/remind +2h текст</code>\n"
            "<code>/remind 2025-06-09 10:00 текст</code>\n"
            "<code>/remind 09.06.2025 10:00 текст</code>\n\n"
            "Время — UTC. Или просто напиши «напомни мне ...»"
        )
        return

    remind_at, title = parsed
    try:
        event = create_event(message.from_user.id, message.chat.id, title, remind_at)
        at_fmt = event.remind_at.strftime("%d.%m.%Y %H:%M UTC")
        await message.answer(
            f"⏰ Напоминание <code>#{event.id}</code> на {at_fmt}:\n{html.escape(title)}"
        )
    except Exception as e:
        await message.answer(f"⛔ Не удалось создать: {html.escape(str(e))}")


@dp.message(Command("reminders"))
async def cmd_reminders(message: Message) -> None:
    """Список запланированных напоминаний."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    if not SCHEDULER_ENABLED:
        await message.answer("⛔ Планировщик отключён.")
        return

    events = list_events(message.from_user.id)
    if not events:
        await message.answer(
            "📭 Нет активных напоминаний.\n\n"
            "Напиши, например: «напомни мне завтра в 10:00 проверить деплой»"
        )
        return

    lines = [format_event_line(e, escape_html=html.escape) for e in events]
    await message.answer(
        "⏰ <b>Запланированные напоминания</b>\n\n" + "\n".join(lines)
    )


@dp.message(Command("cancel_reminder"))
async def cmd_cancel_reminder(message: Message, command: CommandObject) -> None:
    """Отмена напоминания по ID."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    if not SCHEDULER_ENABLED:
        await message.answer("⛔ Планировщик отключён.")
        return

    args = (command.args or "").strip()
    if not args or not args.split()[0].isdigit():
        await message.answer("Использование: /cancel_reminder &lt;id&gt;\nСписок: /reminders")
        return

    event_id = int(args.split()[0])
    if cancel_event(event_id, message.from_user.id):
        await message.answer(f"🗑 Напоминание <code>#{event_id}</code> отменено.")
    else:
        await message.answer(
            f"⛔ Напоминание <code>#{event_id}</code> не найдено или уже сработало."
        )


@dp.message(Command("bot_rollback"))
async def cmd_bot_rollback(message: Message, command: CommandObject) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    args = (command.args or "").strip()
    steps = 1
    tokens = args.split()
    for token in tokens:
        if token.isdigit():
            steps = min(int(token), 5)
            break

    ok, result = git_rollback(steps)
    if ok:
        push_note = ""
        pushed, push_result = git_push()
        icon = "📤" if pushed else "⚠️"
        push_note = f"\n{icon} Push: {html.escape(push_result)}"
        await message.answer(
            f"✅ {html.escape(result)}{push_note}\n🔄 Перезапуск через 3 сек..."
        )
        asyncio.create_task(
            schedule_restart(3.0, chat_id=message.chat.id, notify_text=_RESTART_NOTIFY_TEXT)
        )
    else:
        await message.answer(f"⛔ {html.escape(result)}")


@dp.message(Command("set_prompt"))
async def cmd_set_prompt(message: Message, command: CommandObject) -> None:
    """Команда /set_prompt <текст> — задать свой промпт для агента."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    if not command.args or not command.args.strip():
        await message.answer(
            "Использование: /set_prompt &lt;текст&gt;\n\n"
            "Этот промпт будет добавляться к каждому твоему запросу (о себе, предпочтениях, контексте)."
        )
        return

    _set_user_prompt(message.from_user.id, command.args.strip())
    preview = command.args.strip()[:200] + ("..." if len(command.args.strip()) > 200 else "")
    await message.answer(f"✅ Промпт сохранён:\n\n<pre>{html.escape(preview)}</pre>")


@dp.message(Command("myprompt"))
async def cmd_myprompt(message: Message) -> None:
    """Команда /myprompt — показать свой промпт."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    prompt = _get_user_prompt(message.from_user.id)
    if not prompt:
        await message.answer("У тебя нет сохранённого промпта. Используй /set_prompt &lt;текст&gt;")
        return

    preview = prompt[:1500] + ("..." if len(prompt) > 1500 else "")
    await message.answer(f"📝 Твой промпт:\n\n<pre>{html.escape(preview)}</pre>")


@dp.message(Command("clear_prompt"))
async def cmd_clear_prompt(message: Message) -> None:
    """Команда /clear_prompt — очистить свой промпт."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    _set_user_prompt(message.from_user.id, "")
    await message.answer("🗑 Промпт очищен.")


@dp.message(Command("get_global_prompt", "global_prompt"))
async def cmd_get_global_prompt(message: Message) -> None:
    """Команда /get_global_prompt — показать глобальный промпт."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    prompt = _get_default_prompt()
    if not prompt:
        await message.answer("Глобальный промпт пуст. Используй /set_global_prompt &lt;текст&gt;")
        return

    preview = prompt[:3500] + ("..." if len(prompt) > 3500 else "")
    await message.answer(f"📋 <b>Глобальный промпт:</b>\n\n<pre>{html.escape(preview)}</pre>")


@dp.message(Command("set_global_prompt"))
async def cmd_set_global_prompt(message: Message, command: CommandObject) -> None:
    """Команда /set_global_prompt <текст> — задать глобальный промпт для агента."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    if not command.args or not command.args.strip():
        await message.answer("Использование: /set_global_prompt &lt;текст&gt;")
        return

    try:
        _set_default_prompt(command.args.strip())
        preview = command.args.strip()[:200] + ("..." if len(command.args.strip()) > 200 else "")
        await message.answer(f"✅ Глобальный промпт сохранён:\n\n<pre>{html.escape(preview)}</pre>")
    except Exception as e:
        await message.answer(f"⛔ Ошибка сохранения: {e}")


@dp.message(Command("cd"))
async def cmd_cd(message: Message, command: CommandObject) -> None:
    """Команда /cd <путь> — сменить директорию."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    if not command.args or not command.args.strip():
        await message.answer(
            f"📂 Текущая: <code>{_get_user_cwd(message.from_user.id)}</code>\n\nИспользование: /cd &lt;путь&gt;"
        )
        return

    path = _resolve_path(message.from_user.id, command.args.strip())
    if path is None:
        await message.answer("⛔ Путь вне рабочей директории.")
        return
    if not path.is_dir():
        await message.answer(f"⛔ Не директория: <code>{path}</code>")
        return

    _set_user_cwd(message.from_user.id, path)
    await message.answer(f"📂 <code>{path}</code>")


@dp.message(Command("pwd"))
async def cmd_pwd(message: Message) -> None:
    """Команда /pwd — показать текущую директорию."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    cwd = _get_user_cwd(message.from_user.id)
    await message.answer(f"📂 <code>{cwd}</code>")


@dp.message(Command("ls"))
async def cmd_ls(message: Message, command: CommandObject) -> None:
    """Команда /ls [путь] — список файлов."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    path = _get_user_cwd(message.from_user.id)
    if command.args and command.args.strip():
        p = _resolve_path(message.from_user.id, command.args.strip())
        if p is None:
            await message.answer("⛔ Путь вне рабочей директории.")
            return
        path = p

    if not path.is_dir():
        await message.answer(f"⛔ Не директория: <code>{path}</code>")
        return

    try:
        entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        lines = []
        for e in entries:
            icon = "📁" if e.is_dir() else "📄"
            lines.append(f"{icon} <code>{html.escape(e.name)}</code>")
        text = "\n".join(lines[:50]) if lines else "(пусто)"
        if len(lines) > 50:
            text += f"\n\n... и ещё {len(lines) - 50}"
        await message.answer(f"📂 <code>{path}</code>\n\n{text}")
    except OSError as e:
        await message.answer(f"⛔ Ошибка: {e}")


@dp.message(Command("mkdir"))
async def cmd_mkdir(message: Message, command: CommandObject) -> None:
    """Команда /mkdir <путь> — создать директорию."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    if not command.args or not command.args.strip():
        await message.answer("Использование: /mkdir &lt;путь&gt;")
        return

    path = _resolve_path(message.from_user.id, command.args.strip())
    if path is None:
        await message.answer("⛔ Путь вне рабочей директории.")
        return

    try:
        path.mkdir(parents=True, exist_ok=True)
        await message.answer(f"📁 Создано: <code>{path}</code>")
    except OSError as e:
        await message.answer(f"⛔ Ошибка: {e}")


@dp.message(Command("cat"))
async def cmd_cat(message: Message, command: CommandObject) -> None:
    """Команда /cat <файл> — показать содержимое файла."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    if not command.args or not command.args.strip():
        await message.answer("Использование: /cat &lt;файл&gt;")
        return

    path = _resolve_path(message.from_user.id, command.args.strip())
    if path is None:
        await message.answer("⛔ Путь вне рабочей директории.")
        return
    if not path.is_file():
        await message.answer(f"⛔ Не файл: <code>{path}</code>")
        return

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        escaped = html.escape(content)
        pre_max = MAX_RESPONSE_LENGTH - len("<pre></pre>")
        chunks = _split_long_text(escaped, max_len=pre_max)
        for i, chunk in enumerate(chunks):
            await message.answer(f"<pre>{chunk}</pre>")
            if i < len(chunks) - 1:
                await asyncio.sleep(0.3)
    except OSError as e:
        await message.answer(f"⛔ Ошибка: {e}")


@dp.message(Command("rm"))
async def cmd_rm(message: Message, command: CommandObject) -> None:
    """Команда /rm <путь> — удалить файл или пустую директорию."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    if not command.args or not command.args.strip():
        await message.answer("Использование: /rm &lt;файл или директория&gt;")
        return

    path = _resolve_path(message.from_user.id, command.args.strip())
    if path is None:
        await message.answer("⛔ Путь вне рабочей директории.")
        return
    if path == WORKSPACE_DIR:
        await message.answer("⛔ Нельзя удалить корень workspace.")
        return

    try:
        if path.is_file():
            path.unlink()
            await message.answer(f"🗑 Удалён файл: <code>{path}</code>")
        elif path.is_dir():
            if any(path.iterdir()):
                await message.answer("⛔ Директория не пуста. Удалите содержимое сначала.")
            else:
                path.rmdir()
                await message.answer(f"🗑 Удалена директория: <code>{path}</code>")
        else:
            await message.answer(f"⛔ Не найден: <code>{path}</code>")
    except OSError as e:
        await message.answer(f"⛔ Ошибка: {e}")


def _build_agent_prompt(user_id: int, user_text: str, prompt_body: str) -> tuple[str, Path]:
    """Собирает полный промпт и рабочую директорию для cursor-agent."""
    session_key = user_session_key(user_id)
    parts: list[str] = []
    default_prompt = _get_default_prompt()
    if not has_agent_session(session_key) and default_prompt:
        parts.append(default_prompt)
    user_prompt = _get_user_prompt(user_id)
    if user_prompt:
        parts.append(f"[Информация от пользователя]\n{user_prompt}\n[/Информация от пользователя]")
    codeword_guard = build_codeword_guard_prompt(user_text)
    if codeword_guard:
        parts.append(codeword_guard)
    parts.append(prompt_body)
    prompt = "\n\n".join(parts)

    agent_cwd = _get_user_cwd(user_id)
    if self_modify_enabled() and codeword_required() and has_codeword(user_text):
        repo = Path(os.getenv("BOT_REPO_DIR", "/workspace/cursor_cli_agent"))
        if repo.is_dir():
            agent_cwd = repo
    return prompt, agent_cwd


async def _run_agent_for_user(
    message: Message,
    prompt_body: str,
    user_text: str,
) -> None:
    """Запускает cursor-agent и отправляет ответ пользователю."""
    user_id = message.from_user.id
    prompt, agent_cwd = _build_agent_prompt(user_id, user_text, prompt_body)

    status_msg = await message.answer(
        '<tg-emoji emoji-id="5210764626857313664">🤖</tg-emoji> Инициализация...'
    )

    response, success = await run_cursor_agent_streaming(
        prompt,
        agent_cwd,
        session_key=user_session_key(user_id),
        status_msg=status_msg,
    )

    remaining_text, schedule_notes = _parse_schedule_reminders(
        response, user_id, message.chat.id
    )
    remaining_text, send_docs = _parse_send_document(remaining_text)
    bot = message.bot
    for path, name, caption in send_docs:
        resolved = _resolve_path(user_id, str(path))
        if resolved and resolved.is_file():
            try:
                await message.answer_document(
                    FSInputFile(resolved, filename=name),
                    caption=caption or None,
                )
            except Exception as e:
                logger.warning("Не удалось отправить файл %s: %s", path, e)
                remaining_text = f"⛔ Не удалось отправить файл: {path}\n\n{remaining_text}"

    final_text = remaining_text or "(пустой ответ)"
    if schedule_notes:
        notes_block = "\n".join(schedule_notes)
        final_text = f"{final_text}\n\n{notes_block}" if final_text != "(пустой ответ)" else notes_block
    await _send_response(status_msg, final_text, message, bot)

    if success:
        await _finalize_bot_changes(response, user_text, status_msg, message)


async def _run_headless_agent(prompt: str, user_id: int) -> tuple[str, bool]:
    """Запускает cursor-agent без Telegram-статуса (для планировщика)."""
    parts: list[str] = []
    default_prompt = _get_default_prompt()
    if not has_agent_session(SESSION_KEY_SCHEDULER) and default_prompt:
        parts.append(default_prompt)
    user_prompt = _get_user_prompt(user_id)
    if user_prompt:
        parts.append(f"[Информация от пользователя]\n{user_prompt}\n[/Информация от пользователя]")
    parts.append(prompt)
    full_prompt = "\n\n".join(parts)
    return await run_cursor_agent_streaming(
        full_prompt,
        WORKSPACE_DIR,
        session_key=SESSION_KEY_SCHEDULER,
        status_msg=None,
    )


async def _save_telegram_photo(message: Message) -> tuple[Path, str] | None:
    """Сохраняет фото в files/. Возвращает (абсолютный путь, files/имя) или None."""
    photo = message.photo[-1]
    filename = f"photo_{photo.file_unique_id}.jpg"
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    dest = _get_unique_file_path(FILES_DIR, filename)
    try:
        await message.bot.download(photo, destination=dest)
        return dest, f"files/{dest.name}"
    except Exception as e:
        logger.exception("Ошибка сохранения фото: %s", e)
        await message.answer(f"⛔ Не удалось сохранить фото: {e}")
        return None


def _build_photo_prompt(rel_path: str, abs_path: Path, caption: str) -> str:
    """Формирует промпт для агента по отправленному фото."""
    lines = [
        "[Пользователь отправил фото]",
        f"Изображение: @{abs_path}",
        f"Файл в workspace: {rel_path}",
    ]
    if caption:
        lines.append(f"Запрос пользователя: {caption}")
    else:
        lines.append("Запрос: опиши изображение и ответь пользователю.")
    lines.append("Используй прикреплённое изображение (@...) как визуальный контекст.")
    return "\n".join(lines)


@dp.message(F.document)
async def handle_document(message: Message) -> None:
    """Сохранение документа в files/."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    doc = message.document
    filename = doc.file_name or f"document_{doc.file_unique_id}"
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    dest = _get_unique_file_path(FILES_DIR, filename)

    try:
        await message.bot.download(doc, destination=dest)
        await message.answer(f"📥 Файл сохранён: <code>files/{dest.name}</code>")
    except Exception as e:
        logger.exception("Ошибка сохранения файла: %s", e)
        await message.answer(f"⛔ Не удалось сохранить файл: {e}")


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    """Фолбэк: если батчинг выключен или не сработал."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    saved = await _save_telegram_photo(message)
    if not saved:
        return

    dest, rel_path = saved
    caption = (message.caption or "").strip()
    user_text = caption if caption else f"[фото] {rel_path}"
    prompt_body = _build_photo_prompt(rel_path, dest, caption)
    await _run_agent_for_user(message, prompt_body, user_text)


@dp.message(F.video)
async def handle_video(message: Message) -> None:
    """Сохранение видео в files/."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    video = message.video
    filename = video.file_name or f"video_{video.file_unique_id}.mp4"
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    dest = _get_unique_file_path(FILES_DIR, filename)

    try:
        await message.bot.download(video, destination=dest)
        await message.answer(f"📥 Видео сохранено: <code>files/{dest.name}</code>")
    except Exception as e:
        logger.exception("Ошибка сохранения видео: %s", e)
        await message.answer(f"⛔ Не удалось сохранить видео: {e}")


@dp.guest_message(F.text)
async def handle_guest_message(message: Message) -> None:
    """Guest Mode: @бот вопрос в любом чате (даже без добавления бота)."""
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    guest_query_id = message.guest_query_id
    if not guest_query_id:
        return

    query = _strip_bot_mention(message.text or "")
    if not query:
        await message.bot.answer_guest_query(
            guest_query_id,
            _make_article_result("empty", "Пустой запрос", "Напиши вопрос после @бота"),
        )
        return

    if _is_guest_reset_query(query):
        clear_chat_id(_guest_session_key(message.from_user.id))
        try:
            await message.bot.answer_guest_query(
                guest_query_id,
                _make_article_result(
                    "reset",
                    "Контекст сброшен",
                    "🔄 Guest Mode: контекст сброшен. Следующий вопрос — новый диалог.",
                ),
            )
        except TelegramBadRequest as e:
            logger.warning("answer_guest_query reset failed: %s", e)
        return

    chat_context = _format_guest_chat_context(message)
    answer, success = await _run_external_agent(
        message.from_user.id, query, source="Guest Mode", chat_context=chat_context
    )
    if not success:
        answer = f"⛔ {answer}"

    try:
        await message.bot.answer_guest_query(
            guest_query_id,
            _make_article_result("answer", query[:64], answer),
        )
    except TelegramBadRequest as e:
        logger.warning("answer_guest_query failed: %s", e)


@dp.inline_query()
async def handle_inline_query(inline_query: InlineQuery) -> None:
    """Inline Mode: @бот вопрос в поле ввода любого чата."""
    if not inline_query.from_user or not is_allowed(inline_query.from_user.id):
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    query = (inline_query.query or "").strip()
    if not query:
        await inline_query.answer(
            [
                _make_article_result(
                    "hint",
                    "Задай вопрос",
                    "Напиши вопрос после @бота, например: что такое дуги на брекитах",
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    result_id = uuid.uuid4().hex
    _pending_inline[result_id] = (query, inline_query.from_user.id)
    await inline_query.answer(
        [
            _make_article_result(
                result_id,
                query[:64],
                f"⏳ Думаю над: {html.escape(query[:200])}",
            )
        ],
        cache_time=0,
        is_personal=True,
    )


@dp.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult) -> None:
    """Догенерация ответа после выбора inline-результата."""
    if not chosen.from_user or not is_allowed(chosen.from_user.id):
        return
    pending = _pending_inline.pop(chosen.result_id, None)
    if not pending:
        return
    query, user_id = pending
    inline_message_id = chosen.inline_message_id

    if inline_message_id:
        try:
            await chosen.bot.edit_message_text(
                "⏳ Генерирую ответ...",
                inline_message_id=inline_message_id,
            )
        except TelegramBadRequest:
            pass

    answer, success = await _run_external_agent(user_id, query, source="Inline Mode")
    if not success:
        answer = f"⛔ {answer}"
    answer = _balance_html_tags(answer)

    if inline_message_id:
        try:
            await chosen.bot.edit_message_text(
                answer,
                inline_message_id=inline_message_id,
                parse_mode=ParseMode.HTML,
            )
            return
        except TelegramBadRequest:
            answer = _strip_html_tags(answer)

    await _deliver_agent_text(chosen.bot, chosen.from_user.id, answer)


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    """Фолбэк: если батчинг выключен или не сработал."""
    if not message.text:
        return

    if message.text.strip().startswith("/"):
        return

    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    prompt_body = message.html_text.strip()
    if not prompt_body:
        return

    user_text = message.text.strip()
    await _run_agent_for_user(message, prompt_body, user_text)


@dp.errors()
async def global_error_handler(event: ErrorEvent) -> None:
    """Ловит необработанные ошибки и при необходимости запускает автофикс."""
    logger.exception("Необработанная ошибка: %s", event.exception)

    update = event.update
    if not update or not update.message:
        return

    msg = update.message
    if not is_allowed(msg.from_user.id):
        return

    err_text = "".join(traceback.format_exception(type(event.exception), event.exception, event.exception.__traceback__))
    ERROR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_path = ERROR_REPORTS_DIR / f"crash_{ts}.txt"
    report_path.write_text(err_text, encoding="utf-8")

    auto_fix_will_run = (
        self_modify_enabled()
        and SELF_MODIFY_AUTO_FIX
        and check_codeword(err_text)[0]
    )
    try:
        crash_msg = (
            f"💥 <b>Критическая ошибка</b>\n<code>{html.escape(type(event.exception).__name__)}</code>"
        )
        if auto_fix_will_run:
            crash_msg += "\nЗапускаю самоисправление..."
        elif self_modify_enabled() and SELF_MODIFY_AUTO_FIX and codeword_required():
            words = ", ".join(f"«{w}»" for w in SELF_MODIFY_CODEWORDS)
            crash_msg += f"\nАвтофикс пропущен. Используй /self_fix или сообщение с кодовым словом ({words})."
        await msg.answer(crash_msg)
    except Exception:
        pass

    if auto_fix_will_run:
        asyncio.create_task(
            _run_self_fix(
                err_text[-4000:],
                msg.from_user.id,
                event.bot,
                msg.chat.id,
                auto=True,
            )
        )


async def main() -> None:
    """Запуск бота."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN обязателен. Задайте в .env")

    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS пуст — доступ для всех (не рекомендуется)")

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_mcp_config()
    load_agent_sessions()
    _load_user_prompts()

    session = _create_bot_session()
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )

    global _bot_username
    me = await bot.get_me()
    _bot_username = me.username

    # Меню команд (подсказки при вводе /)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Приветствие и твой ID"),
            BotCommand(command="help", description="Справка по боту"),
            BotCommand(command="new", description="Сбросить контекст чата"),
            BotCommand(command="status", description="Проверка подключения"),
            BotCommand(command="set_prompt", description="Задать свой промпт"),
            BotCommand(command="myprompt", description="Показать свой промпт"),
            BotCommand(command="clear_prompt", description="Очистить промпт"),
            BotCommand(command="get_global_prompt", description="Показать глобальный промпт"),
            BotCommand(command="set_global_prompt", description="Задать глобальный промпт"),
            BotCommand(command="cd", description="Сменить директорию"),
            BotCommand(command="pwd", description="Текущая директория"),
            BotCommand(command="ls", description="Список файлов"),
            BotCommand(command="mkdir", description="Создать директорию"),
            BotCommand(command="cat", description="Показать файл"),
            BotCommand(command="rm", description="Удалить файл/папку"),
            BotCommand(command="self_fix", description="Исправить код бота"),
            BotCommand(command="bot_git_status", description="Git-статус бота"),
            BotCommand(command="bot_git_log", description="Коммиты бота"),
            BotCommand(command="bot_rollback", description="Откат коммита бота"),
            BotCommand(command="remind", description="Создать напоминание"),
            BotCommand(command="reminders", description="Список напоминаний"),
            BotCommand(command="cancel_reminder", description="Отменить напоминание"),
        ]
    )

    if self_modify_enabled():
        logger.info(
            "Самомодификация: вкл, repo=%s, auto_fix=%s",
            "OK" if repo_ready() else "НЕТ",
            SELF_MODIFY_AUTO_FIX,
        )

    setup_message_batch(_run_agent_for_user)
    dp.message.middleware(MessageBatchMiddleware())

    await _notify_after_restart(bot)
    start_scheduler(bot, _run_headless_agent, _deliver_agent_text)
    logger.info("Бот запущен (@%s)", _bot_username or "?")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        if session is not None:
            await session.close()


if __name__ == "__main__":
    asyncio.run(main())
