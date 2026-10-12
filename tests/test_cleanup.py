import hashlib
from pathlib import Path

import pytest

from canvas_sync.cleanup import Cleanup
from canvas_sync.tasks import Tasks


class FakeTrash:
    def __init__(self, root):
        self.root = root
        root.mkdir(exist_ok=True)
        self.calls = []
        self.fail_name = None

    def __call__(self, path):
        self.calls.append(path)
        if path.name == self.fail_name:
            raise OSError("模拟废纸篓不可用")
        target = self.root / (str(len(self.calls)) + "-" + path.name)
        path.rename(target)


def cleaner(system, tmp_path):
    _, _, db, ns, _, root, _ = system
    trash = FakeTrash(tmp_path / "模拟废纸篓")
    return Cleanup(db, trash), trash


def test_same_name_courses_and_shared_cache_untouched(system, tmp_path):
    _, _, db, ns, courses, root, sync = system
    sync([dict(c, name="同名课程") for c in courses[:2]])
    other = Path(db.resource(ns, 2, "file", 11)["path"])
    cache = {str(p): p.read_bytes() for p in root.glob(".canvas-sync/*/cache/*/*")}
    cleanup, trash = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    assert preview["file_count"] > 0 and preview["bytes"] > 0
    result = cleanup.execute(ns, preview["id"])
    assert all(i["result"] != "failed" for i in result["items"])
    assert other.exists() and all(Path(p).read_bytes() == body for p, body in cache.items())
    assert db.resource(ns, 1, "file", 11)["path"] == ""
    assert len(Tasks(db).list(ns)) == 4


def test_unmanaged_and_modified_notebook_group_retained_by_default(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    doc = Path(db.resource(ns, 1, "assignment", 501)["path"])
    notebook = doc.parent / "analysis.ipynb"
    notebook.write_text("my work")
    personal = doc.parent / "我的笔记.txt"
    personal.write_text("自己的文件")
    cleanup, _ = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    assert "1:assignment:501" in preview["protected_groups"]
    cleanup.execute(ns, preview["id"])
    assert notebook.read_text() == "my work"
    assert personal.exists() and (doc.parent / "data.csv").exists()
    assert doc.exists()


def test_explicit_modified_group_cleanup_keeps_user_added_file(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    doc = Path(db.resource(ns, 1, "assignment", 501)["path"])
    notebook = doc.parent / "analysis.ipynb"
    notebook.write_text("my work")
    personal = doc.parent / "自己的文件.txt"
    personal.write_text("保留")
    cleanup, trash = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root, ["1:assignment:501"])
    cleanup.execute(ns, preview["id"])
    assert not notebook.exists() and personal.exists()
    assert any(p.read_text() == "my work" for p in trash.root.iterdir() if p.is_file() and p.name.endswith(".ipynb"))


def test_history_remote_and_old_term_paths_all_recorded(system, tmp_path):
    server, _, db, ns, courses, root, sync = system
    sync()
    primary = Path(db.resource(ns, 1, "file", 12)["path"])
    server.contents["12"] = b"new,content\n"
    server.version += 1
    sync()
    assert any(a["category"] == "history" for a in db.artifacts(ns, "1"))
    primary.write_text("local edit")
    sync()
    assert any(a["category"] == "remote" for a in db.artifacts(ns, "1"))
    old_doc = Path(db.resource(ns, 1, "assignment", 501)["path"])
    sync([dict(courses[0], term_override="另一个学期")])
    assert db.artifact(ns, old_doc) and old_doc.exists()
    cleanup, _ = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    assert any(i["path"] == str(old_doc) for i in preview["items"])


def test_partial_failure_no_permanent_fallback_and_retry(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    cleanup, trash = cleaner(system, tmp_path)
    trash.fail_name = "lecture.pdf"
    path = Path(db.resource(ns, 1, "file", 13)["path"])
    preview = cleanup.preview(ns, ["1"], root)
    result = cleanup.execute(ns, preview["id"])
    assert path.exists()
    assert any(i["result"] == "failed" and "模拟废纸篓" in i["result_reason"] for i in result["items"])
    assert any(i["result"] == "trashed" for i in result["items"])
    trash.fail_name = None
    retry = cleanup.preview(ns, ["1"], root)
    cleanup.execute(ns, retry["id"])
    assert not path.exists()


def test_path_traversal_and_symlink_never_touched(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    victim = tmp_path / "outside.txt"
    victim.write_text("cannot touch")
    file = Path(db.resource(ns, 1, "file", 13)["path"])
    file.unlink()
    file.symlink_to(victim)
    with db.connect() as sql:
        source = dict(sql.execute("SELECT * FROM artifacts WHERE ns=? AND path=?", (ns, str(file))).fetchone())
        sql.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)", (ns, "1", str(victim), source["root"], source["course_dir"], source["logical"], source["group_key"], "file", hashlib.sha256(victim.read_bytes()).hexdigest(), "active"))
    cleanup, trash = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    cleanup.execute(ns, preview["id"])
    assert victim.read_text() == "cannot touch" and file.is_symlink()
    assert file not in trash.calls and victim not in trash.calls


def test_file_changed_after_preview_skipped(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    file = Path(db.resource(ns, 1, "file", 13)["path"])
    cleanup, trash = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    file.write_text("edited after preview")
    result = cleanup.execute(ns, preview["id"])
    assert file.read_text() == "edited after preview" and file not in trash.calls
    assert any("预览后" in i["result_reason"] for i in result["items"])


def test_root_replaced_by_symlink_after_preview_is_not_followed(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    file = Path(db.resource(ns, 1, "file", 13)["path"])
    cleanup, trash = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    moved = root.with_name("原目录")
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    result = cleanup.execute(ns, preview["id"])
    assert file.exists() and not trash.calls
    assert any("符号链接" in i["result_reason"] for i in result["items"])
    assert not Tasks(db).attachments(Tasks(db).list(ns)[0])[0]["available"]


def test_interrupted_trash_repreview_repairs_missing_record(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    cleanup, trash = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    class Interrupted(BaseException):
        pass
    def interrupted(path):
        trash(path)
        raise Interrupted()
    cleanup.trash = interrupted
    with pytest.raises(Interrupted):
        cleanup.execute(ns, preview["id"])
    journal = cleanup.report(ns, preview["id"])
    pending = next(i for i in journal["items"] if i["result"] == "processing")
    assert not Path(pending["path"]).exists()
    cleanup.trash = trash
    restored = Cleanup(db, trash)
    retry = restored.preview(ns, ["1"], root)
    result = restored.execute(ns, retry["id"])
    assert db.artifact(ns, pending["path"])["state"] == "missing"
    assert any(i["result"] == "missing" for i in result["items"])
    assert db.resource(ns, 1, "file", 13)["path"] == ""


def test_cleanup_shares_sync_lock(system, tmp_path):
    from canvas_sync.lock import sync_lock
    from canvas_sync.errors import SyncError
    _, _, db, ns, _, root, sync = system
    sync()
    cleanup, trash = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    with sync_lock(root / ".canvas-sync" / ns / "sync.lock"):
        with pytest.raises(SyncError):
            cleanup.execute(ns, preview["id"])
    assert not trash.calls and cleanup.report(ns, preview["id"])["status"] == "preview"


def test_reconcile_and_resync_restore_without_losing_tasks(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    tasks = Tasks(db)
    task = next(t for t in tasks.list(ns) if t["source_id"] == "501")
    tasks.edit(ns, task["id"], "进行中", "保留状态")
    db.select(ns, ["1"])
    cleanup, _ = cleaner(system, tmp_path)
    cleanup.execute(ns, cleanup.preview(ns, ["1"], root)["id"])
    assert not db.courses(ns)[0]["selected"]
    saved = tasks.get(ns, task["id"])
    assert "读取数据" in saved["summary"] and saved["notes"] == "保留状态"
    assert all(not a["available"] for a in tasks.attachments(saved))
    db.select(ns, ["1"])
    result = sync()
    assert result["network_downloads"] == 0
    assert all(a["available"] for a in tasks.attachments(saved))
    assert tasks.get(ns, task["id"])["status"] == "进行中"


def test_archive_option_retains_tasks_and_can_be_reversed(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    cleanup, _ = cleaner(system, tmp_path)
    cleanup.execute(ns, cleanup.preview(ns, ["1"], root, archive=True)["id"])
    assert db.courses(ns)[0]["archived"] and Tasks(db).list(ns) == []
    assert len(Tasks(db).list(ns, show_archived=True)) == 2
    db.archive_course(ns, "1", False)
    assert len(Tasks(db).list(ns)) == 2


def test_missing_file_and_reused_preview(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    path = Path(db.resource(ns, 1, "file", 13)["path"])
    path.unlink()
    cleanup, _ = cleaner(system, tmp_path)
    preview = cleanup.preview(ns, ["1"], root)
    result = cleanup.execute(ns, preview["id"])
    assert any(i["result"] == "missing" for i in result["items"])
    with pytest.raises(ValueError, match="已经执行"):
        cleanup.execute(ns, preview["id"])
    with pytest.raises(ValueError):
        cleanup.report("other-user", preview["id"])


def test_kept_index_has_no_invalid_local_links(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    index = next(Path(a["path"]) for a in db.artifacts(ns, "1") if a["category"] == "index")
    user = index.parent / "个人笔记.txt"
    user.write_text("keep")
    cleanup, _ = cleaner(system, tmp_path)
    cleanup.execute(ns, cleanup.preview(ns, ["1"], root)["id"])
    assert index.exists() and user.exists()
    body = index.read_text()
    assert "本地资料已清理" in body
    assert 'href="%E4%BD%9C%E4%B8%9A/' not in body


def test_cleanup_preserves_reason_for_never_downloaded_resource(system, tmp_path):
    _, _, db, ns, _, root, sync = system
    sync()
    db.save_resource(ns, "1", "module_item", "locked", {"title": "尚未解锁", "source": "https://canvas.test/courses/1/modules", "status": "denied", "reason": "尚未达到解锁条件"})
    cleanup, _ = cleaner(system, tmp_path)
    cleanup.execute(ns, cleanup.preview(ns, ["1"], root)["id"])
    saved = db.resource(ns, "1", "module_item", "locked")
    assert saved["status"] == "denied" and saved["reason"] == "尚未达到解锁条件"
