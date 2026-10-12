import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from canvas_sync.config import Config
from canvas_sync.reminders import (MacNotifier, ReminderWorker, center, service_status,
                                   set_settings, start_service, stop_service)
from canvas_sync.store import Store
from canvas_sync.tasks import Tasks, iso


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 12, tzinfo=timezone.utc)
    def __call__(self):
        return self.value
    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class FakeNotifier:
    def __init__(self):
        self.calls = []
        self.error = ""
    def send(self, title, body):
        self.calls.append((title, body))
        return {"channel": "test", "error": self.error}


@pytest.fixture
def reminder_system(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    clock = Clock()
    store.save_course("account", {"id": "1", "name": "课程", "term": "秋季", "history": False})
    tasks = Tasks(store, clock)
    notifier = FakeNotifier()
    worker = ReminderWorker(store, notifier, clock)
    due = iso(clock() + timedelta(hours=48))
    key = tasks.canvas("account", "1", {"id": 9, "name": "数据作业", "due_at": due, "description": "说明"})
    return store, clock, tasks, notifier, worker, key


def test_nodes_once_and_restart_does_not_duplicate(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    assert worker.tick("account") == 0
    clock.advance(hours=24)
    assert worker.tick("account") == 1 and len(notifier.calls) == 1
    worker.tick("account")
    restarted = ReminderWorker(Store(db.path), notifier, clock)
    restarted.recover()
    assert restarted.tick("account") == 0 and len(notifier.calls) == 1
    clock.advance(hours=22)
    assert restarted.tick("account") == 1 and len(notifier.calls) == 2
    assert "课程" in notifier.calls[0][1] and "数据作业" in notifier.calls[0][1] and "剩余" in notifier.calls[0][1]
    assert "2026-10-14" in notifier.calls[0][1]
    clock.advance(hours=3)
    assert restarted.tick("account") == 0
    assert tasks.get("account", key)["status"] == "未开始"


@pytest.mark.parametrize("action", ["complete", "task_archive", "course_archive"])
def test_complete_or_archive_cancels_pending(reminder_system, action):
    db, clock, tasks, notifier, worker, key = reminder_system
    worker.tick("account")
    if action == "course_archive":
        db.archive_course("account", "1")
    else:
        tasks.edit("account", key, "已完成" if action == "complete" else "未开始", "", archived=action == "task_archive")
    clock.advance(hours=47)
    assert worker.tick("account") == 0 and not notifier.calls


def test_due_change_reschedules_future_and_keeps_original_history(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    worker.tick("account")
    clock.advance(hours=24)
    worker.tick("account")
    due = iso(clock() + timedelta(days=5))
    tasks.canvas("account", "1", {"id": 9, "name": "改名", "due_at": due, "description": "新要求"})
    worker.tick("account")
    assert tasks.history("account", key)[0]["after_due"] == due
    clock.advance(hours=22)
    assert worker.tick("account") == 0 and len(notifier.calls) == 1
    clock.advance(hours=74)  # 新日期前24小时
    assert worker.tick("account") == 1 and len(notifier.calls) == 2
    assert "改名" in notifier.calls[-1][1]


def test_sleep_gap_merges_many_nodes_once(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    for aid in (10, 11):
        tasks.canvas("account", "1", {"id": aid, "name": f"作业{aid}", "due_at": iso(clock() + timedelta(hours=48)), "description": "说明"})
    worker.tick("account")
    clock.advance(hours=50)
    assert worker.tick("account") == 6
    assert len(notifier.calls) == 1 and "合并 3 个任务" in notifier.calls[0][0]
    assert "已逾期" in notifier.calls[0][1]
    assert len(center(db, "account")) == 6
    worker.tick("account")
    assert len(notifier.calls) == 1


def test_first_import_past_nodes_do_not_bombard(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    tasks.canvas("account", "1", {"id": 12, "name": "旧作业", "due_at": iso(clock() - timedelta(days=200)), "description": "旧内容"})
    worker.tick("account")
    assert not notifier.calls
    assert any(e["status"] == "expired" for e in center(db, "account", True))


def test_no_due_unscheduled_local_plan_scheduled(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    no_due = tasks.add("account", "无日期")
    planned = tasks.add("account", "复习", planned_due=iso(clock() + timedelta(hours=48)))
    worker.tick("account")
    events = center(db, "account", True)
    assert all(e["task"] != no_due for e in events)
    assert any(e["task"] == planned for e in events)


def test_notification_failure_persistent_and_not_retried(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    notifier.error = "权限拒绝"
    worker.tick("account")
    clock.advance(hours=24)
    worker.tick("account")
    assert center(db, "account")[0]["status"] == "failed"
    worker.tick("account")
    assert len(notifier.calls) == 1 and center(db, "account")[0]["error"] == "权限拒绝"


def test_title_update_without_due_change_refreshes_pending_message(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    worker.tick("account")
    original = tasks.get("account", key)
    tasks.canvas("account", "1", {"id": 9, "name": "新标题", "description": "新要求", "due_at": original["canvas_due_raw"]})
    clock.advance(hours=24)
    worker.tick("account")
    assert "新标题" in notifier.calls[0][1]
    assert tasks.get("account", key)["due_version"] == original["due_version"]


def test_crash_after_claim_is_unknown_not_automatically_replayed(reminder_system, monkeypatch):
    db, clock, tasks, notifier, worker, key = reminder_system
    worker.tick("account")
    clock.advance(hours=24)
    class Crash(BaseException):
        pass
    def crash(*args):
        raise Crash()
    monkeypatch.setattr(worker, "_finish", crash)
    with pytest.raises(Crash):
        worker.tick("account")
    assert center(db, "account")[0]["status"] == "sending"
    restarted = ReminderWorker(db, notifier, clock)
    restarted.recover()
    assert center(db, "account")[0]["status"] == "unknown"
    restarted.tick("account")
    assert len(notifier.calls) == 1


def test_concurrent_ticks_claim_only_once(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    worker.tick("account")
    clock.advance(hours=24)
    entered, release = Event(), Event()
    class Blocking(FakeNotifier):
        def send(self, title, body):
            self.calls.append((title, body))
            entered.set()
            assert release.wait(5)
            return {"channel": "test", "error": ""}
    notifier = Blocking()
    first = ReminderWorker(db, notifier, clock)
    second = ReminderWorker(db, notifier, clock)
    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(first.tick, "account")
        assert entered.wait(5)
        assert second.tick("account") == 0
        release.set()
        assert running.result() == 1
    assert len(notifier.calls) == 1


def test_disable_and_reenable_only_future_nodes(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    worker.tick("account")
    set_settings(db, "account", False, [1440, 120], clock)
    clock.advance(hours=25)
    worker.tick("account")
    set_settings(db, "account", True, [1440, 120], clock)
    worker.tick("account")
    assert not notifier.calls
    clock.advance(hours=21)
    assert worker.tick("account") == 1


def test_reopen_does_not_catch_up_completed_interval(reminder_system):
    db, clock, tasks, notifier, worker, key = reminder_system
    worker.tick("account")
    tasks.edit("account", key, "已完成", "")
    clock.advance(hours=25)
    tasks.edit("account", key, "进行中", "")
    worker.tick("account")
    assert not notifier.calls
    clock.advance(hours=21)
    assert worker.tick("account") == 1


def test_reopened_during_claim_cancels_past_node(reminder_system, monkeypatch):
    db, clock, tasks, notifier, worker, key = reminder_system
    worker.tick("account")
    clock.advance(hours=24)
    original = worker._eligible
    def edit_before_send(event):
        tasks.edit("account", key, "已完成", "")
        clock.advance(minutes=1)
        tasks.edit("account", key, "进行中", "")
        return original(event)
    monkeypatch.setattr(worker, "_eligible", edit_before_send)
    worker.tick("account")
    assert not notifier.calls
    assert any(e["status"] == "cancelled" for e in center(db, "account", True))


def test_settings_deduplicate_and_reject_invalid_offsets(reminder_system):
    db, clock, _, _, _, _ = reminder_system
    set_settings(db, "account", True, [120, 120], clock)
    with db.connect() as sql:
        assert json.loads(sql.execute("SELECT offsets FROM reminder_settings").fetchone()[0]) == [120]
    with pytest.raises(ValueError):
        set_settings(db, "account", True, [-1], clock)


def test_windows_falls_back_to_reminder_center(monkeypatch):
    from canvas_sync import reminders
    monkeypatch.setattr(reminders.sys, "platform", "win32")
    result = MacNotifier().send("任务", "消息")
    assert result["channel"] == "in_app" and "暂未支持" in result["error"]


def test_macos_notifier_passes_text_as_arguments(monkeypatch):
    from canvas_sync import reminders
    calls = []
    monkeypatch.setattr(reminders.sys, "platform", "darwin")
    class Result:
        returncode = 0
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()
    monkeypatch.setattr(reminders.subprocess, "run", run)
    title = '标题 "quoted" $(do-not-run)'
    assert not MacNotifier().send(title, "内容")["error"]
    assert calls[0][0][-2:] == [title, "内容"]
    assert "shell" not in calls[0][1]


def test_detached_service_singleton_offline_and_stop(tmp_path):
    # 没有账户，服务不可能发送真实通知；不创建任何 Canvas HTTP 客户端。
    Config().save(tmp_path)
    start_service(tmp_path)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not service_status(tmp_path)["healthy"]:
            time.sleep(.1)
        state = service_status(tmp_path)
        assert state["running"]
        assert "已经运行" in start_service(tmp_path)
        assert state["healthy"]
        # 绕开 start 的状态预检，让第二个真实进程竞争同一个锁。
        second = subprocess.run([sys.executable, "-m", "canvas_sync.reminders", "--home", str(tmp_path), "run"], timeout=8, capture_output=True)
        assert second.returncode == 0
        assert service_status(tmp_path)["pid"] == state["pid"]
    finally:
        stop_service(tmp_path)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and service_status(tmp_path)["running"]:
            time.sleep(.1)
    assert not service_status(tmp_path)["running"]
