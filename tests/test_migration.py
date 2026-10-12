import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from canvas_sync import schema
from canvas_sync.config import profile_id
from canvas_sync.store import Store
from canvas_sync.tasks import Tasks


def legacy(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    root = tmp_path / "资料"
    ns = profile_id("https://canvas.test", 42)
    relative = Path(f"Canvas-{ns[:10]}") / "秋季" / "课程__课程1" / "作业" / "作业__9" / "_Canvas要求.html"
    file = root / relative
    file.parent.mkdir(parents=True)
    file.write_text('<html><body><h2>原有要求</h2><table><tr><td>评分</td></tr></table></body></html>', encoding="utf-8")
    logical = hashlib.sha256(str(root).encode()).hexdigest()[:12] + ":1:assignment:9:document"
    with monkeypatch.context() as patch:
        patch.setattr(schema, "VERSION", 0)
        store = Store(path)
        store.save_course(ns, {"id": "1", "name": "课程", "term": "秋季", "history": False})
        with store.connect() as db:
            db.execute("UPDATE courses SET selected=1,term_override='手动学期'")
        store.save_placement(ns, logical, file, hashlib.sha256(file.read_bytes()).hexdigest(), "rev")
        store.save_resource(ns, 1, "assignment", 9, {"title": "作业", "path": str(file), "due": "2026-10-20T15:59:00Z", "source": "https://canvas.test/courses/1/assignments/9"})
    return path, root, ns, file


def test_upgrade_retains_data_backfills_tasks_and_is_idempotent(tmp_path, monkeypatch):
    path, root, ns, file = legacy(tmp_path, monkeypatch)
    before = file.read_bytes()
    store = Store(path)
    courses = store.courses(ns)
    assert courses[0]["selected"] and courses[0]["term_override"] == "手动学期" and not courses[0]["archived"]
    task = Tasks(store).list(ns)[0]
    assert "原有要求" in task["summary"] and "<table>" in task["requirement_html"]
    assert task["canvas_due_raw"] == "2026-10-20T15:59:00Z"
    assert store.artifact(ns, file)
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
    Store(path)
    assert len(Tasks(store).list(ns)) == 1
    assert file.read_bytes() == before
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == schema.VERSION


def test_failed_upgrade_rolls_back_and_preserves_backup(tmp_path, monkeypatch):
    path, _, ns, _ = legacy(tmp_path, monkeypatch)
    original = schema.migrate
    def broken(db):
        original(db)
        raise RuntimeError("模拟中断")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "migrate", broken)
        with pytest.raises(RuntimeError):
            Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone()
        assert "archived" not in [r[1] for r in db.execute("PRAGMA table_info(courses)")]
        assert db.execute("SELECT selected FROM courses WHERE ns=?", (ns,)).fetchone()[0] == 1
    assert list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(Store(path).courses(ns)) == 1


def test_missing_legacy_body_creates_honest_summary(tmp_path, monkeypatch):
    path, _, ns, file = legacy(tmp_path, monkeypatch)
    file.unlink()
    task = Tasks(Store(path)).list(ns)[0]
    assert "要求尚未缓存" in task["summary"]


def test_legacy_unverifiable_history_not_claimed(tmp_path, monkeypatch):
    path, _, ns, file = legacy(tmp_path, monkeypatch)
    history = file.parent / "_Canvas历史版本"
    history.mkdir()
    candidate = history / "_Canvas要求~0123456789ab.html"
    candidate.write_text("个人文件")
    store = Store(path)
    assert store.artifact(ns, candidate) is None and candidate.exists()


def test_legacy_history_requires_matching_full_hash(tmp_path, monkeypatch):
    path, _, ns, file = legacy(tmp_path, monkeypatch)
    history = file.parent / "_Canvas历史版本"
    history.mkdir()
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    verified = history / f"{file.stem}~{digest[:12]}{file.suffix}"
    verified.write_bytes(file.read_bytes())
    # 相同的哈希前缀命名不是完整内容证据。
    other = history / f"{file.stem}~000000000000{file.suffix}"
    other.write_text("用户文件")
    store = Store(path)
    assert store.artifact(ns, verified)["category"] == "history"
    assert store.artifact(ns, other) is None


def test_legacy_remote_copy_verified_and_used_for_requirements(tmp_path, monkeypatch):
    path, _, ns, file = legacy(tmp_path, monkeypatch)
    remote = file.with_name("_Canvas要求~Canvas远程-1234567890.html")
    remote.write_text("<h2>远程最新要求</h2><script>do_not_run()</script>")
    digest = hashlib.sha256(remote.read_bytes()).hexdigest()
    file.write_text("我修改的旧要求")
    with sqlite3.connect(path) as db:
        payload = json.loads(db.execute("SELECT payload FROM resources").fetchone()[0])
        payload.update(remote_path=str(remote), sha=digest)
        db.execute("UPDATE resources SET payload=?", (json.dumps(payload),))
    store = Store(path)
    assert store.artifact(ns, remote)["category"] == "remote"
    task = Tasks(store).list(ns)[0]
    assert "远程最新要求" in task["summary"] and "<script>" not in task["requirement_html"]
    assert file.read_text() == "我修改的旧要求"
