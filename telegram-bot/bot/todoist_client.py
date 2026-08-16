"""Тонкий клиент Todoist REST API v1 (stdlib urllib)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

API_BASE = "https://api.todoist.com/api/v1"


class TodoistError(Exception):
    """Ошибка Todoist API или конфигурации."""


def is_configured() -> bool:
    return bool(os.getenv("TODOIST_API_KEY", "").strip())


def _token() -> str:
    token = os.getenv("TODOIST_API_KEY", "").strip()
    if not token:
        raise TodoistError("TODOIST_API_KEY не задан")
    return token


def _format_due(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> Any:
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise TodoistError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise TodoistError(f"Сеть: {e.reason}") from e


def create_task(
    content: str,
    due_datetime: datetime,
    description: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": content.strip(),
        "due_datetime": _format_due(due_datetime),
    }
    if description.strip():
        payload["description"] = description.strip()
    result = _request("POST", "/tasks", body=payload)
    if not isinstance(result, dict) or "id" not in result:
        raise TodoistError(f"Неожиданный ответ create_task: {result!r}")
    return result


def close_task(task_id: str) -> None:
    _request("POST", f"/tasks/{task_id}/close")


def get_task(task_id: str) -> dict[str, Any] | None:
    """Активная задача или None если completed/404."""
    try:
        result = _request("GET", f"/tasks/{task_id}")
    except TodoistError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    return result if isinstance(result, dict) else None


def list_open_tasks(*, limit: int = 100) -> list[dict[str, Any]]:
    """Активные задачи (одна страница)."""
    result = _request("GET", "/tasks", params={"limit": str(min(limit, 200))})
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        items = result.get("results") or result.get("items") or []
        return items if isinstance(items, list) else []
    return []
