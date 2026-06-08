"""
Сессии cursor-agent: отдельный chat id на пользователя и на планировщик.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

AGENT_SESSIONS_FILE = Path(
    os.getenv("AGENT_SESSIONS_FILE", "/workspace/.bot/agent_sessions.json")
)
CURSOR_CLI_PATH = os.getenv("CURSOR_CLI_PATH", "cursor-agent")

SESSION_KEY_SCHEDULER = "scheduler"
_USER_PREFIX = "user:"

_chat_ids: dict[str, str] = {}
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_uuid_re = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)


def user_session_key(user_id: int) -> str:
    return f"{_USER_PREFIX}{user_id}"


def get_chat_id(session_key: str) -> str | None:
    return _chat_ids.get(session_key)


def has_agent_session(session_key: str) -> bool:
    return bool(get_chat_id(session_key))


def set_chat_id(session_key: str, chat_id: str) -> None:
    _chat_ids[session_key] = chat_id
    _save()


def clear_chat_id(session_key: str) -> None:
    if session_key in _chat_ids:
        del _chat_ids[session_key]
        _save()


def load_agent_sessions() -> None:
    global _chat_ids
    try:
        if AGENT_SESSIONS_FILE.exists():
            data = json.loads(AGENT_SESSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _chat_ids = {str(k): str(v) for k, v in data.items() if v}
    except Exception as e:
        logger.warning("Не удалось загрузить agent_sessions: %s", e)


def _save() -> None:
    try:
        AGENT_SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        AGENT_SESSIONS_FILE.write_text(
            json.dumps(_chat_ids, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Не удалось сохранить agent_sessions: %s", e)


def _parse_chat_id(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    match = _uuid_re.search(text)
    if match:
        return match.group(0)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        return lines[-1]
    return None


async def _run_cli(
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "timeout"
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def create_agent_chat(cwd: Path, env: dict[str, str]) -> str | None:
    """Создаёт новый чат cursor-agent и возвращает его id."""
    code, stdout, stderr = await _run_cli(
        [CURSOR_CLI_PATH, "create-chat"],
        cwd,
        env,
    )
    if code != 0:
        logger.warning("create-chat failed (%s): %s", code, stderr[:300])
        return None
    chat_id = _parse_chat_id(stdout)
    if chat_id:
        logger.info("Создан chat id: %s", chat_id)
    else:
        logger.warning("create-chat: не удалось распарсить id из: %s", stdout[:200])
    return chat_id


async def ensure_chat_id(session_key: str, cwd: Path, env: dict[str, str]) -> str | None:
    """Возвращает chat id для ключа, создавая новый при необходимости."""
    existing = get_chat_id(session_key)
    if existing:
        return existing

    async with _locks[session_key]:
        existing = get_chat_id(session_key)
        if existing:
            return existing
        chat_id = await create_agent_chat(cwd, env)
        if chat_id:
            set_chat_id(session_key, chat_id)
        return chat_id
