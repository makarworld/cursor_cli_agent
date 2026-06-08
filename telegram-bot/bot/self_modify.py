"""
Самомодификация бота: git-коммиты, откат, перезапуск после правок кода.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SELF_MODIFY_ENABLED = os.getenv("SELF_MODIFY_ENABLED", "true").lower() in ("1", "true", "yes")
SELF_MODIFY_AUTO_FIX = os.getenv("SELF_MODIFY_AUTO_FIX", "true").lower() in ("1", "true", "yes")
SELF_MODIFY_MAX_PER_HOUR = int(os.getenv("SELF_MODIFY_MAX_PER_HOUR", "5"))
SELF_MODIFY_CODEWORD_REQUIRED = os.getenv("SELF_MODIFY_CODEWORD_REQUIRED", "true").lower() in (
    "1",
    "true",
    "yes",
)
# Кодовые слова для разрешения правок кода бота (через запятую, регистр не важен)
SELF_MODIFY_CODEWORDS: tuple[str, ...] = tuple(
    w.strip().lower()
    for w in os.getenv("SELF_MODIFY_CODEWORDS", "бурмалда,бурмалди").split(",")
    if w.strip()
)

BOT_REPO_DIR = Path(os.getenv("BOT_REPO_DIR", "/workspace/cursor_cli_agent"))
BOT_SOURCE_DIR = Path(os.getenv("BOT_SOURCE_DIR", "/app"))
SELF_MODIFY_STATE_FILE = Path(
    os.getenv("SELF_MODIFY_STATE_FILE", "/workspace/.bot/self_modify_state.json")
)

GIT_USER_NAME = os.getenv("GIT_USER_NAME", "cursor-telegram-bot")
GIT_USER_EMAIL = os.getenv("GIT_USER_EMAIL", "bot@cursor-cli-agent.local")

# Файлы бота, которые разрешено менять при самоисправлении
BOT_CODE_GLOBS = ("bot/**/*.py", "default_prompt.txt", "requirements.txt")


def is_enabled() -> bool:
    return SELF_MODIFY_ENABLED


def codeword_required() -> bool:
    return SELF_MODIFY_CODEWORD_REQUIRED


def has_codeword(text: str) -> bool:
    """Проверяет наличие кодового слова в тексте (без учёта регистра)."""
    if not text or not SELF_MODIFY_CODEWORDS:
        return False
    lowered = text.lower()
    return any(word in lowered for word in SELF_MODIFY_CODEWORDS)


def check_codeword(text: str) -> tuple[bool, str]:
    """Проверяет разрешение на самомодификацию по кодовому слову."""
    if not codeword_required():
        return True, "OK"
    if has_codeword(text):
        return True, "OK"
    words = ", ".join(f"«{w}»" for w in SELF_MODIFY_CODEWORDS)
    return False, f"Нужно кодовое слово в сообщении: {words}"


def build_codeword_guard_prompt(user_text: str) -> str:
    """Инструкция для агента: можно или нельзя трогать код бота."""
    words = ", ".join(f"«{w}»" for w in SELF_MODIFY_CODEWORDS)
    if codeword_required() and not has_codeword(user_text):
        return (
            "[ЗАПРЕТ САМОМОДИФИКАЦИИ]\n"
            f"В запросе пользователя НЕТ кодового слова ({words}).\n"
            "ЗАПРЕЩЕНО изменять код Telegram-бота: telegram-bot/, /app/, bot/main.py, "
            "bot/self_modify.py, default_prompt.txt, requirements.txt.\n"
            "Не делай git commit в telegram-bot/. Только отвечай или работай с другими файлами.\n"
            "[/ЗАПРЕТ САМОМОДИФИКАЦИИ]"
        )
    if codeword_required() and has_codeword(user_text):
        return (
            "[САМОМОДИФИКАЦИЯ РАЗРЕШЕНА]\n"
            f"В запросе есть кодовое слово ({words}).\n"
            "Сам реши, просит ли пользователь обновить/исправить/изменить ЭТОТ Telegram-бот "
            "(его код, команды, поведение).\n"
            "• Если ДА — правь telegram-bot/ или /app/, затем добавь в ответ:\n"
            "  SELF_MODIFY_COMMIT::сообщение коммита на русском\n"
            "• Если НЕТ (задача про другой проект, файл, вопрос) — код бота НЕ трогай.\n"
            "Кодовое слово само по себе не означает «обязательно меняй бота» — смотри на смысл запроса.\n"
            "[/САМОМОДИФИКАЦИЯ РАЗРЕШЕНА]"
        )
    return ""


def has_bot_code_changes() -> bool:
    """Есть ли незакоммиченные изменения в telegram-bot/."""
    if not repo_ready():
        return False
    code, out, _ = _run_git(["status", "--porcelain", "--", "telegram-bot/"])
    return code == 0 and bool(out.strip())


def get_bot_code_paths() -> list[Path]:
    """Возвращает список файлов исходников бота."""
    paths: list[Path] = []
    for pattern in BOT_CODE_GLOBS:
        paths.extend(BOT_SOURCE_DIR.glob(pattern))
    return sorted(p for p in paths if p.is_file())


def _run_git(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    repo = cwd or BOT_REPO_DIR
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", GIT_USER_NAME)
    env.setdefault("GIT_COMMITTER_NAME", GIT_USER_NAME)
    env.setdefault("GIT_AUTHOR_EMAIL", GIT_USER_EMAIL)
    env.setdefault("GIT_COMMITTER_EMAIL", GIT_USER_EMAIL)

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, "", str(e)


def git_available() -> bool:
    code, _, _ = _run_git(["--version"])
    return code == 0


def repo_ready() -> bool:
    if not BOT_REPO_DIR.is_dir():
        return False
    code, _, _ = _run_git(["rev-parse", "--git-dir"])
    return code == 0


def git_status_short() -> str:
    code, out, err = _run_git(["status", "--short"])
    if code != 0:
        return f"git status failed: {err or out}"
    return out or "(нет изменений)"


def git_log(limit: int = 10) -> str:
    code, out, err = _run_git(
        ["log", f"-{limit}", "--oneline", "--", "telegram-bot/"]
    )
    if code != 0:
        return f"git log failed: {err or out}"
    return out or "(коммитов нет)"


def git_commit(message: str, paths: list[Path] | None = None) -> tuple[bool, str]:
    """Коммитит изменения в telegram-bot/. Возвращает (успех, сообщение)."""
    if not repo_ready():
        return False, f"Git-репозиторий не найден: {BOT_REPO_DIR}"

    rel_paths: list[str]
    if paths:
        rel_paths = []
        for p in paths:
            try:
                rel = p.resolve().relative_to(BOT_REPO_DIR.resolve())
                rel_paths.append(str(rel).replace("\\", "/"))
            except ValueError:
                try:
                    rel = p.resolve().relative_to(BOT_SOURCE_DIR.resolve())
                    rel_paths.append(f"telegram-bot/{rel}".replace("\\", "/"))
                except ValueError:
                    logger.warning("Путь вне репозитория, пропуск: %s", p)
    else:
        rel_paths = ["telegram-bot/"]

    _run_git(["add", "--", *rel_paths])
    code, _, err = _run_git(["diff", "--cached", "--quiet"])
    if code == 0:
        return False, "Нет изменений для коммита"

    code, out, err = _run_git(["commit", "-m", message])
    if code != 0:
        return False, err or out or "git commit failed"

    _save_state_after_commit(message)
    return True, out or "Коммит создан"


def git_discard_worktree() -> tuple[bool, str]:
    """Сбрасывает незакоммиченные изменения в telegram-bot/."""
    if not repo_ready():
        return False, f"Git-репозиторий не найден: {BOT_REPO_DIR}"
    code, out, err = _run_git(["checkout", "HEAD", "--", "telegram-bot/"])
    if code != 0:
        return False, err or out or "git checkout failed"
    return True, "Незакоммиченные изменения отменены"


def git_rollback(steps: int = 1) -> tuple[bool, str]:
    """Откатывает последние N коммитов (только telegram-bot/)."""
    if not repo_ready():
        return False, f"Git-репозиторий не найден: {BOT_REPO_DIR}"

    if steps < 1:
        return False, "steps должен быть >= 1"

    code, head, err = _run_git(["rev-parse", "HEAD"])
    if code != 0:
        return False, err or "Не удалось получить HEAD"

    target = f"HEAD~{steps}"
    code, _, err = _run_git(["rev-parse", target])
    if code != 0:
        return False, f"Недостаточно коммитов для отката на {steps}"

    code, out, err = _run_git(["checkout", target, "--", "telegram-bot/"])
    if code != 0:
        return False, err or out or "git checkout failed"

    msg = f"bot: rollback {steps} step(s) from {head[:8]}"
    ok, commit_msg = git_commit(msg)
    if not ok:
        return False, commit_msg
    return True, f"Откат выполнен. Было: {head[:8]}. {commit_msg}"


def validate_python_files(paths: list[Path] | None = None) -> tuple[bool, str]:
    """Проверяет синтаксис Python-файлов через py_compile."""
    targets = paths or [p for p in get_bot_code_paths() if p.suffix == ".py"]
    errors: list[str] = []
    for path in targets:
        code, _, err = _run_py_compile(path)
        if code != 0:
            errors.append(f"{path.name}: {err}")
    if errors:
        return False, "\n".join(errors)
    return True, "Синтаксис OK"


def _run_py_compile(path: Path) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, "", str(e)


def _load_state() -> dict:
    try:
        if SELF_MODIFY_STATE_FILE.exists():
            return json.loads(SELF_MODIFY_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Не удалось загрузить state: %s", e)
    return {"fixes": [], "last_commit": None}


def _save_state(data: dict) -> None:
    try:
        SELF_MODIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SELF_MODIFY_STATE_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Не удалось сохранить state: %s", e)


def _save_state_after_commit(message: str) -> None:
    state = _load_state()
    code, head, _ = _run_git(["rev-parse", "HEAD"])
    if code == 0:
        state["last_commit"] = head
    state.setdefault("commits", []).append({"ts": time.time(), "message": message, "hash": head if code == 0 else None})
    state["commits"] = state["commits"][-50:]
    _save_state(state)


def can_auto_fix() -> tuple[bool, str]:
    """Проверяет лимит автофиксов в час."""
    if not SELF_MODIFY_AUTO_FIX:
        return False, "SELF_MODIFY_AUTO_FIX выключен"
    if not is_enabled():
        return False, "SELF_MODIFY_ENABLED выключен"
    if not repo_ready():
        return False, f"Репозиторий не смонтирован: {BOT_REPO_DIR}"

    state = _load_state()
    now = time.time()
    recent = [t for t in state.get("fixes", []) if now - t < 3600]
    if len(recent) >= SELF_MODIFY_MAX_PER_HOUR:
        return False, f"Лимит автофиксов ({SELF_MODIFY_MAX_PER_HOUR}/час)"

    state["fixes"] = recent
    _save_state(state)
    return True, "OK"


def record_auto_fix() -> None:
    state = _load_state()
    state.setdefault("fixes", []).append(time.time())
    _save_state(state)


def build_self_fix_prompt(error_text: str, user_hint: str = "") -> str:
    """Формирует промпт для cursor-agent при самоисправлении."""
    bot_files = "\n".join(f"- {p}" for p in get_bot_code_paths())
    repo_note = (
        f"Репозиторий: {BOT_REPO_DIR}\n"
        f"Исходники бота: {BOT_SOURCE_DIR}\n"
        f"Коммиты делай только в telegram-bot/ (путь от корня репо).\n"
    )
    hint_block = f"\nПодсказка от пользователя:\n{user_hint}\n" if user_hint else ""

    return (
        "ЗАДАЧА: исправь баг в коде Telegram-бота (самомодификация).\n\n"
        f"{repo_note}\n"
        f"Файлы бота:\n{bot_files}\n\n"
        f"ОШИБКА:\n{error_text}\n"
        f"{hint_block}\n"
        "ТРЕБОВАНИЯ:\n"
        "1. Минимальные правки — только то, что нужно для фикса.\n"
        "2. Сохрани стиль и соглашения существующего кода.\n"
        "3. Не ломай безопасность (ALLOWED_USER_IDS, проверки путей).\n"
        "4. После правок напиши краткое описание изменений.\n"
        "5. В конце ответа ОБЯЗАТЕЛЬНО добавь строку:\n"
        "   SELF_MODIFY_COMMIT::сообщение коммита на русском\n"
        "   (одна строка, без переносов — это сообщение для git commit)\n"
    )


def parse_commit_message(agent_response: str) -> str | None:
    """Извлекает сообщение коммита из ответа агента."""
    for line in agent_response.splitlines():
        line = line.strip()
        if line.startswith("SELF_MODIFY_COMMIT::"):
            msg = line[len("SELF_MODIFY_COMMIT::") :].strip()
            if msg:
                return msg[:200]
    return None


async def schedule_restart(delay_seconds: float = 2.0) -> None:
    """Перезапускает процесс бота (Docker restart: unless-stopped подхватит)."""
    await asyncio.sleep(delay_seconds)
    logger.info("Перезапуск бота после самомодификации...")
    os.execv(sys.executable, [sys.executable, "-m", "bot"])
