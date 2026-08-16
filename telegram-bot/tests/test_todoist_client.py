"""Unit-тесты тонкого клиента Todoist."""

from __future__ import annotations

from datetime import datetime


def test_is_configured_false_without_key(monkeypatch):
    monkeypatch.delenv("TODOIST_API_KEY", raising=False)
    from bot.todoist_client import is_configured

    assert is_configured() is False


def test_is_configured_true_with_key(monkeypatch):
    monkeypatch.setenv("TODOIST_API_KEY", "tok")
    from bot.todoist_client import is_configured

    assert is_configured() is True


def test_create_task_posts_due_datetime(monkeypatch):
    monkeypatch.setenv("TODOIST_API_KEY", "tok")
    from bot import todoist_client as tc

    captured = {}

    def fake_request(method, path, *, body=None, params=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "T1", "content": body["content"]}

    monkeypatch.setattr(tc, "_request", fake_request)
    due = datetime(2026, 7, 25, 12, 0, 0)
    result = tc.create_task("Встреча", due, "детали")
    assert result["id"] == "T1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/tasks"
    assert captured["body"]["due_datetime"] == "2026-07-25T12:00:00Z"
    assert captured["body"]["description"] == "детали"


def test_get_task_404_returns_none(monkeypatch):
    monkeypatch.setenv("TODOIST_API_KEY", "tok")
    from bot import todoist_client as tc
    from bot.todoist_client import TodoistError

    def fake_request(*a, **k):
        raise TodoistError("HTTP 404: not found")

    monkeypatch.setattr(tc, "_request", fake_request)
    assert tc.get_task("gone") is None
