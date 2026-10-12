from datetime import datetime, timedelta, timezone

import pytest

from canvas_sync.tasks import Tasks, effective_due, utc_time


def test_sync_dedup_preserves_status_and_notes_and_revises_due(system):
    server, api, db, ns, _, root, sync = system
    sync()
    tasks = Tasks(db)
    assignment = next(t for t in tasks.list(ns) if t["source_id"] == "501")
    tasks.edit(ns, assignment["id"], "进行中", "我已经开始做了")
    sync()
    assert len(tasks.list(ns)) == 2
    new = tasks.get(ns, assignment["id"])
    assert new["status"] == "进行中" and new["notes"] == "我已经开始做了"
    data = server.assignment()
    data["name"] = "改名作业"
    data["due_at"] = "2026-10-25T15:00:00Z"
    data["html_url"] = api.base + "/courses/1/assignments/501"
    tasks.canvas(ns, "1", data)
    task = tasks.get(ns, assignment["id"])
    assert task["title"] == "改名作业" and task["status"] == "进行中" and task["notes"] == new["notes"]
    history = tasks.history(ns, task["id"])
    assert history[0]["before_due"] == data["due_at"].replace("25T15:00", "20T15:59")
    assert history[0]["after_due"] == data["due_at"]


def test_task_created_even_if_attachment_download_fails(system):
    server, api, db, ns, _, root, sync = system
    import httpx
    original = server.handle
    def handler(request):
        return httpx.Response(403) if request.url.path.startswith("/download/") else original(request)
    api.http.client.close()
    api.http.client = httpx.Client(transport=httpx.MockTransport(handler))
    sync()
    tasks = Tasks(db).list(ns)
    assert len(tasks) == 2 and "计算平均值" in tasks[0]["summary"]
    assert all(not a["available"] for a in Tasks(db).attachments(tasks[0]))


def test_manual_task_status_reopen_archive_and_date_clear(system):
    _, _, db, ns, _, _, _ = system
    tasks = Tasks(db)
    key = tasks.add(ns, "复习", planned_due="2026-10-13T23:59:00+08:00", notes="第三章")
    assert tasks.get(ns, key)["planned_due"] == "2026-10-13T15:59:00+00:00"
    tasks.edit(ns, key, "已完成", "完成笔记", None)
    assert tasks.list(ns) == []
    assert tasks.list(ns, show_completed=True)[0]["status"] == "已完成"
    tasks.edit(ns, key, "未开始", "重新复习", None, True)
    assert tasks.list(ns) == []
    tasks.edit(ns, key, "进行中", "继续", None, False, "新标题")
    assert tasks.list(ns)[0]["title"] == "新标题"
    assert effective_due(tasks.get(ns, key)) is None


def test_quick_views_use_shanghai_and_sort_null_last(system):
    _, _, db, ns, _, _, _ = system
    current = datetime(2026, 10, 12, 16, 30, tzinfo=timezone.utc)  # 上海 10/13 00:30
    tasks = Tasks(db, lambda: current)
    overdue = tasks.add(ns, "今天已过", planned_due="2026-10-13T00:00:00+08:00")
    future = tasks.add(ns, "今天以后", planned_due="2026-10-13T23:59:00+08:00")
    later = tasks.add(ns, "下周以后", planned_due="2026-10-25T23:59:00+08:00")
    missing = tasks.add(ns, "无日期")
    assert {t["id"] for t in tasks.list(ns, view="今天到期")} == {overdue, future}
    assert [t["id"] for t in tasks.list(ns, view="已逾期")] == [overdue]
    assert [t["id"] for t in tasks.list(ns, view="未来 7 天")] == [future]
    assert [t["id"] for t in tasks.list(ns, view="无日期")] == [missing]
    assert tasks.list(ns)[-1]["id"] == missing


def test_archived_course_hidden_and_sync_does_not_unarchive(system):
    _, _, db, ns, courses, _, sync = system
    sync()
    db.archive_course(ns, "1")
    assert Tasks(db).list(ns) == []
    db.save_course(ns, courses[0])
    assert db.courses(ns)[0]["archived"]
    db.select(ns, ["1"])
    assert not db.courses(ns)[0]["selected"]
    assert sync()["network_downloads"] == 0
    db.archive_course(ns, "1", False)
    assert len(Tasks(db).list(ns)) == 2


def test_due_semantics_original_kept_same_instant_not_revised(system):
    _, _, db, ns, _, _, _ = system
    tasks = Tasks(db)
    first = {"id": 1, "name": "作业", "due_at": "2026-11-01T01:30:00-04:00", "description": "内容"}
    key = tasks.canvas(ns, "1", first)
    task = tasks.get(ns, key)
    assert task["canvas_due"] == "2026-11-01T05:30:00+00:00"
    tasks.canvas(ns, "1", dict(first, due_at="2026-11-01T05:30:00Z"))
    assert tasks.history(ns, key) == []
    with pytest.raises(ValueError, match="计划日期替代"):
        tasks.edit(ns, key, "未开始", "", "2026-11-02T00:00:00+08:00")


def test_invalid_dates_not_inferred_and_namespaces_isolated(system):
    _, _, db, ns, _, _, _ = system
    tasks = Tasks(db)
    key = tasks.canvas(ns, "1", {"id": 1, "name": "作业", "description": "", "due_at": "坏日期"})
    assert tasks.get(ns, key)["date_error"] and effective_due(tasks.get(ns, key)) is None
    assert tasks.list("other") == []
    with pytest.raises(ValueError):
        tasks.add(ns, "任务", planned_due="2026-10-12T12:00:00")
