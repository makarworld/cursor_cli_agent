"""Тесты schema / dual-write / dry fire."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def sched_db(tmp_path, monkeypatch):
    db_path = tmp_path / "sched.db"
    monkeypatch.setenv("SCHEDULER_DB_PATH", str(db_path))
    monkeypatch.setenv("TODOIST_API_KEY", "test-token")
    import bot.scheduler as sch

    sch._db_initialized = False
    if not sch._db.is_closed():
        sch._db.close()
    sch.init_db()
    yield sch
    if not sch._db.is_closed():
        sch._db.close()
    sch._db_initialized = False


def test_blacklist_teeth():
    from bot.scheduler import is_tg_reminder_blocked

    assert is_tg_reminder_blocked("Почистить зубы")
    assert is_tg_reminder_blocked("принять таблетки вечером")
    assert not is_tg_reminder_blocked("Деплой релиза")


def test_schema_fields(sched_db):
    sch = sched_db
    starts = datetime.utcnow() + timedelta(hours=10)
    ev = sch.create_event(
        1,
        1,
        "Встреча",
        starts - timedelta(hours=5),
        todoist_task_id="T1",
        starts_at=starts,
        offset_hours=5,
    )
    loaded = sch.ScheduledEvent.get_by_id(ev.id)
    assert loaded.todoist_task_id == "T1"
    assert loaded.offset_hours == 5
    assert loaded.starts_at is not None


def test_creates_two_offsets(sched_db, monkeypatch):
    sch = sched_db

    def fake_create(content, due, description=""):
        return {"id": "T1", "content": content}

    monkeypatch.setattr(sch, "create_task", fake_create)
    starts = sch._utc_now() + timedelta(hours=10)
    events = sch.create_linked_reminder(1, 1, "Встреча", starts, "")
    assert len(events) == 2
    assert {e.offset_hours for e in events} == {5, 1}
    assert all(e.todoist_task_id == "T1" for e in events)


def test_blacklist_skips_local(sched_db, monkeypatch):
    sch = sched_db
    called = {"n": 0}

    def fake_create(content, due, description=""):
        called["n"] += 1
        return {"id": "T2", "content": content}

    monkeypatch.setattr(sch, "create_task", fake_create)
    starts = sch._utc_now() + timedelta(hours=10)
    events = sch.create_linked_reminder(1, 1, "Почистить зубы", starts, "")
    assert events == []
    assert called["n"] == 1


def test_fire_does_not_call_agent(sched_db):
    sch = sched_db
    starts = sch._utc_now() + timedelta(hours=2)
    ev = sch.create_event(
        1,
        42,
        "Деплой",
        sch._utc_now() - timedelta(seconds=1),
        todoist_task_id="T9",
        starts_at=starts,
        offset_hours=1,
    )
    delivered = []

    async def deliver(bot, chat_id, text):
        delivered.append((chat_id, text))

    asyncio.run(sch._process_one_event(None, ev, deliver))
    assert len(delivered) == 1
    assert delivered[0][0] == 42
    assert "Напоминание" in delivered[0][1]
    assert "за 1 ч" in delivered[0][1]
    assert sch.ScheduledEvent.get_by_id(ev.id).reminded is True


def test_cancel_siblings(sched_db, monkeypatch):
    sch = sched_db
    closed = []

    def fake_close(tid):
        closed.append(tid)

    monkeypatch.setattr(sch, "close_task", fake_close)
    starts = sch._utc_now() + timedelta(hours=10)
    a = sch.create_event(
        1, 1, "X", starts - timedelta(hours=5), todoist_task_id="TX", starts_at=starts, offset_hours=5
    )
    b = sch.create_event(
        1, 1, "X", starts - timedelta(hours=1), todoist_task_id="TX", starts_at=starts, offset_hours=1
    )
    assert sch.cancel_event(a.id, 1) is True
    assert sch.ScheduledEvent.get_by_id(a.id).cancelled is True
    assert sch.ScheduledEvent.get_by_id(b.id).cancelled is True
    assert closed == ["TX"]
