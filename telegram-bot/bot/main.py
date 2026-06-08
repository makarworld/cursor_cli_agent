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
import time
import traceback
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
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

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
    git_rollback,
    git_status_short,
    is_enabled as self_modify_enabled,
    parse_commit_message,
    record_auto_fix,
    repo_ready,
    schedule_restart,
    validate_python_files,
)

load_dotenv()

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
CURSOR_TIMEOUT = int(os.getenv("CURSOR_TIMEOUT_SECONDS", "300"))
MAX_RESPONSE_LENGTH = 4000  # Лимит Telegram
SESSIONS_FILE = Path(os.getenv("SESSIONS_FILE", "/workspace/.bot/sessions.json"))
USER_PROMPTS_FILE = Path(os.getenv("USER_PROMPTS_FILE", "/workspace/.bot/user_prompts.json"))
ERROR_REPORTS_DIR = Path(os.getenv("ERROR_REPORTS_DIR", "/workspace/.bot/errors"))
DEFAULT_PROMPT_FILE = Path(__file__).resolve().parent.parent / "default_prompt.txt"

dp = Dispatcher()

_self_fix_lock = asyncio.Lock()


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


# Хранилище сессий: user_id -> session_active
_user_sessions: dict[int, bool] = {}
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


def _split_response_messages(text: str) -> list[str]:
    """Разбивает текст по ;;; на отдельные сообщения."""
    parts = [p.strip() for p in text.split(MSG_SPLIT_SEP) if p.strip()]
    return parts if parts else ["(пустой ответ)"]


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
    Возвращает True при успехе.
    """
    if len(text) > MAX_RESPONSE_LENGTH:
        text = text[:MAX_RESPONSE_LENGTH] + "\n\n... (обрезано)"
    try:
        if edit:
            await target.edit_text(
                text or "(пустой ответ)",
                reply_markup=reply_markup,
            )
        else:
            await message.answer(
                text or "(пустой ответ)",
                reply_markup=reply_markup,
            )
        return True
    except TelegramBadRequest as e:
        err_name = type(e).__name__
        err_msg = str(e)
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
    for i, part in enumerate(parts):
        is_first = i == 0
        clean_part, button_rows = _parse_inline_buttons(part)
        reply_markup = _build_inline_markup(button_rows)
        await _send_one_message(
            status_msg,
            clean_part,
            message,
            bot,
            edit=is_first,
            reply_markup=reply_markup,
        )
        if not is_first:
            # Небольшая задержка между сообщениями (антифлуд)
            await asyncio.sleep(0.3)


def _load_sessions() -> None:
    """Загрузить сессии из файла."""
    global _user_sessions
    try:
        if SESSIONS_FILE.exists():
            data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            _user_sessions = {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.warning("Не удалось загрузить сессии: %s", e)


def _save_sessions() -> None:
    """Сохранить сессии в файл."""
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(
            json.dumps({str(k): v for k, v in _user_sessions.items()}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Не удалось сохранить сессии: %s", e)


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


def _get_session_active(user_id: int) -> bool:
    """Проверить, есть ли активная сессия у пользователя."""
    return _user_sessions.get(user_id, False)


def _set_session_active(user_id: int, active: bool) -> None:
    """Установить флаг активной сессии."""
    _user_sessions[user_id] = active
    _save_sessions()


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
    continue_session: bool,
    status_msg: Message,
) -> tuple[str, bool]:
    """
    Запускает cursor-agent со stream-json, обновляет status_msg по ходу выполнения.
    Возвращает (ответ, успех).
    """
    if not CURSOR_API_KEY:
        return (
            "❌ CURSOR_API_KEY не настроен. Добавьте в .env ключ с https://cursor.com/dashboard?tab=background-agents",
            False,
        )

    env = os.environ.copy()
    env["CURSOR_API_KEY"] = CURSOR_API_KEY

    cmd = [CURSOR_CLI_PATH, "--model", CURSOR_MODEL, "--force", "--output-format", "stream-json"]
    if continue_session:
        cmd.append("--continue")
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
        if len(output) > MAX_RESPONSE_LENGTH:
            output = output[:MAX_RESPONSE_LENGTH] + "\n\n... (обрезано)"
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

    note = f"📦 Коммит: <code>{html.escape(commit_msg)}</code>" if committed else html.escape(commit_result)
    await message.answer(f"✅ <b>Код бота обновлён</b>\n{note}\n🔄 Перезапуск через 3 сек...")
    asyncio.create_task(schedule_restart(3.0))
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
            continue_session=False,
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
        if committed:
            parts.append(f"📦 Коммит: <code>{html.escape(commit_msg)}</code>")
        else:
            parts.append(f"ℹ️ {html.escape(commit_result)}")
        parts.append("🔄 Перезапуск через 3 сек...")

        await status.edit_text("\n\n".join(parts))
        asyncio.create_task(schedule_restart(3.0))


def is_allowed(user_id: int) -> bool:
    """Проверка доступа пользователя."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


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
        "/bot_rollback — откатить последний коммит бота",
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Команда /status."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    has_key = "✅" if CURSOR_API_KEY else "❌"
    workspace_exists = "✅" if WORKSPACE_DIR.exists() else "❌"
    self_mod = "✅" if self_modify_enabled() else "❌"
    git_ok = "✅" if repo_ready() else "❌"

    await message.answer(
        f"📊 <b>Статус</b>\n\n"
        f"CURSOR_API_KEY: {has_key}\n"
        f"Рабочая директория: {workspace_exists} (<code>{WORKSPACE_DIR}</code>)\n"
        f"Cursor CLI: <code>{CURSOR_CLI_PATH}</code>\n"
        f"Модель: <code>{CURSOR_MODEL}</code>\n"
        f"Самомодификация: {self_mod}\n"
        f"Git-репозиторий: {git_ok}\n"
        f"Автофикс ошибок: {'✅' if SELF_MODIFY_AUTO_FIX else '❌'}\n"
        f"Кодовое слово: {'✅ обязательно' if codeword_required() else '❌ выкл'} "
        f"({', '.join(SELF_MODIFY_CODEWORDS)})",
    )


@dp.message(Command("new", "reset"))
async def cmd_new(message: Message) -> None:
    """Команда /new — сброс контекста."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    _set_session_active(message.from_user.id, False)
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
        "Системные команды: /cd, /pwd, /ls, /mkdir, /cat, /rm\n\n"
        "Обновление бота — напиши «бурмалда» в сообщении, бот сам поймёт нужна ли правка кода.\n"
        "/self_fix [описание] — принудительное самоисправление (без кодового слова)\n"
        "/bot_git_status — изменения в git\n"
        "/bot_git_log — история коммитов\n"
        "/bot_rollback [N] — откат N коммитов",
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
        await message.answer(f"✅ {html.escape(result)}\n🔄 Перезапуск через 3 сек...")
        asyncio.create_task(schedule_restart(3.0))
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
        if len(content) > 3500:
            content = content[:3500] + "\n\n... (обрезано)"
        content = html.escape(content)
        await message.answer(f"<pre>{content}</pre>")
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
    """Сохранение фото в files/ (берём фото максимального размера)."""
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    photo = message.photo[-1]  # наибольшее разрешение
    ext = "jpg"  # Telegram отдаёт фото в JPEG
    filename = f"photo_{photo.file_unique_id}.{ext}"
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    dest = _get_unique_file_path(FILES_DIR, filename)

    try:
        await message.bot.download(photo, destination=dest)
        await message.answer(f"📥 Фото сохранено: <code>files/{dest.name}</code>")
    except Exception as e:
        logger.exception("Ошибка сохранения фото: %s", e)
        await message.answer(f"⛔ Не удалось сохранить фото: {e}")


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


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    """Обработка текстовых сообщений (не команд)."""
    if not message.text:
        return

    # Пропускаем команды — их обрабатывают другие хендлеры
    if message.text.strip().startswith("/"):
        return

    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    prompt = message.html_text.strip()
    if not prompt:
        return

    user_id = message.from_user.id
    continue_session = _get_session_active(user_id)
    user_text = message.text.strip()

    # При новом чате — единоразово добавляем глобальный промпт
    parts: list[str] = []
    default_prompt = _get_default_prompt()
    if not continue_session and default_prompt:
        parts.append(default_prompt)
    # Пользовательский промпт добавляется всегда, если задан
    user_prompt = _get_user_prompt(user_id)
    if user_prompt:
        parts.append(f"[Информация от пользователя]\n{user_prompt}\n[/Информация от пользователя]")
    codeword_guard = build_codeword_guard_prompt(user_text)
    if codeword_guard:
        parts.append(codeword_guard)
    parts.append(prompt)
    prompt = "\n\n".join(parts)

    # Для самомодификации агенту удобнее корень репозитория
    agent_cwd = _get_user_cwd(user_id)
    if self_modify_enabled() and codeword_required() and has_codeword(user_text):
        repo = Path(os.getenv("BOT_REPO_DIR", "/workspace/cursor_cli_agent"))
        if repo.is_dir():
            agent_cwd = repo

    status_msg = await message.answer('<tg-emoji emoji-id="5210764626857313664">🤖</tg-emoji> Инициализация...')

    response, success = await run_cursor_agent_streaming(
        prompt,
        agent_cwd,
        continue_session,
        status_msg,
    )

    if success:
        _set_session_active(user_id, True)

    remaining_text, send_docs = _parse_send_document(response)
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

    await _send_response(status_msg, remaining_text or "(пустой ответ)", message, bot)

    if success:
        await _finalize_bot_changes(response, user_text, status_msg, message)


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
    _load_sessions()
    _load_user_prompts()

    session = _create_bot_session()
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )

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
        ]
    )

    if self_modify_enabled():
        logger.info(
            "Самомодификация: вкл, repo=%s, auto_fix=%s",
            "OK" if repo_ready() else "НЕТ",
            SELF_MODIFY_AUTO_FIX,
        )
    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        if session is not None:
            await session.close()


if __name__ == "__main__":
    asyncio.run(main())
