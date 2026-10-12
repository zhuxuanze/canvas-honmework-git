import json
from pathlib import Path

import httpx
import pytest

from canvas_sync.canvas import Canvas
from canvas_sync.content import date_text, offline_document, public_url
from canvas_sync.errors import PermissionDenied
from canvas_sync.http import ReadOnlyHTTP
from canvas_sync.materialize import Materializer
from canvas_sync.paths import safe_name, under


def test_files_and_truncated_module_items_pagination(system):
    server, api, db, ns, courses, root, sync = system
    assert len(list(api.files("1"))) == 6
    module = list(api.modules("1"))[0]
    assert len(module["items"]) == 1
    assert len(list(api.items("1", module))) == 4
    assert server.calls["/api/v1/courses/1/files"] == 3
    assert server.calls["/api/v1/courses/1/modules/701/items"] == 2


def test_deduplicate_across_entries_and_courses(system):
    server, _, db, ns, courses, root, sync = system
    result = sync(courses[:2])
    assert result["network_downloads"] == 6
    assert server.calls["/download/11"] == 1
    row = db.resource(ns, "1", "file", "11")
    assert len(row["locations"]) >= 3
    assert all(Path(p).exists() for p in row["locations"])


def test_incremental_and_remote_update_preserve_history(system):
    server, _, db, ns, _, root, sync = system
    first = sync()
    assert first["added"] == 9 and not first["incomplete"]
    second = sync()
    assert second["network_downloads"] == 0 and second["skipped"] == 9
    path = Path(db.resource(ns, 1, "file", 12)["path"])
    old = path.read_bytes()
    server.contents["12"] = b"student,score\nAlice,99\n"
    server.version += 1
    updated = sync()
    assert updated["updated"] >= 1
    assert path.read_bytes() == server.contents["12"]
    assert any(p.read_bytes() == old for p in path.parent.joinpath("_Canvas历史版本").glob("*.csv"))


def test_missing_materialized_file_restored_from_cache(system):
    server, _, db, ns, _, root, sync = system
    sync()
    path = Path(db.resource(ns, 1, "file", 12)["path"])
    path.unlink()
    result = sync()
    assert path.read_bytes() == server.contents["12"]
    assert result["network_downloads"] == 0


def test_corrupt_cache_is_downloaded_again(system):
    server, _, db, ns, _, root, sync = system
    sync()
    cache = list(root.glob(".canvas-sync/*/cache/12/*"))[0]
    cache.write_bytes(b"broken")
    result = sync()
    assert result["network_downloads"] == 1 and server.calls["/download/12"] == 2
    assert cache.read_bytes() == server.contents["12"]


def test_notebook_csv_co_located_and_no_execution(system):
    _, _, db, ns, _, root, sync = system
    sync()
    doc = Path(db.resource(ns, 1, "assignment", 501)["path"])
    notebook = doc.parent / "analysis.ipynb"
    assert notebook.is_file() and (doc.parent / "data.csv").is_file()
    source = "".join(json.loads(notebook.read_text())["cells"][0]["source"])
    assert "open('data.csv'" in source
    assert json.loads(notebook.read_text())["cells"][0]["outputs"] == []


def test_local_notebook_modified_preserved_and_complete_remote_group(system):
    server, _, db, ns, _, root, sync = system
    sync()
    doc = Path(db.resource(ns, 1, "assignment", 501)["path"])
    notebook = doc.parent / "analysis.ipynb"
    notebook.write_text('{"my_work":true}', encoding="utf-8")
    server.contents["12"] = b"student,score\nNew,100\n"
    server.version += 1
    result = sync()
    assert result["local_modified"] > 0
    assert notebook.read_text() == '{"my_work":true}'
    snapshots = list(doc.parent.glob("_Canvas远程组合/*/analysis.ipynb"))
    assert snapshots and snapshots[0].read_bytes() == server.contents["11"]
    assert snapshots[0].with_name("data.csv").read_bytes() == server.contents["12"]


def test_no_attachments_assignment_and_current_user_due(system):
    _, _, db, ns, _, root, sync = system
    sync()
    report = db.resource(ns, 1, "assignment", 502)
    assert "阅读与思考" in Path(report["path"]).read_text()
    assignment = db.resource(ns, 1, "assignment", 501)
    document = Path(assignment["path"]).read_text()
    assert "2026-10-20 23:59" in document
    assert "2026-12-01" not in document
    assert "<table>" in document and "<ol>" in document
    assert 'src="diagram.png"' in document and (Path(assignment["path"]).parent / "diagram.png").exists()
    assert "未下载" in document


def test_files_permission_denied_other_entries_continue(system):
    server, _, db, ns, _, root, sync = system
    server.forbid_files = True
    result = sync()
    assert result["incomplete"]
    assert db.resource(ns, 1, "assignment", 501)["path"]
    assert db.resource(ns, 1, "file", 11)["path"]
    statuses = {s["name"]: s["status"] for s in db.sources(ns, 1)}
    assert statuses["Files"] == "denied" and statuses["Assignments"] == "success"


def test_session_expired_marked_incomplete(system):
    server, _, db, ns, _, root, sync = system
    server.expired = True
    result = sync()
    assert result["auth_expired"] and result["incomplete"]
    assert not list(root.rglob("*.ipynb"))


def test_same_name_files_cannot_overwrite(system):
    server, _, db, ns, _, root, sync = system
    sync()
    p1 = Path(db.resource(ns, 1, "file", 12)["path"])
    p2 = Path(db.resource(ns, 1, "file", 16)["path"])
    assert p1 != p2
    assert p1.read_bytes() == server.contents["12"] and p2.read_bytes() == server.contents["16"]


@pytest.mark.parametrize("name", ["../../evil.csv", "/tmp/evil.csv", "..\\..\\evil.csv", "CON", "AUX.csv", "中文" * 200 + ".ipynb", "a\x00b.txt", "<>:?*|"])
def test_unsafe_and_long_names(name, tmp_path):
    safe = safe_name(name)
    assert len(safe.encode()) <= 100 and "/" not in safe and "\\" not in safe
    assert under(tmp_path, safe).parent == tmp_path


def test_symlink_and_traversal_rejected(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    for path in ("../evil", "link/evil", "/private/tmp/evil"):
        with pytest.raises(ValueError):
            under(tmp_path, path)


def test_case_collision_and_unmanaged_files(system):
    _, _, db, ns, _, root, _ = system
    root.mkdir()
    (root / "DATA.csv").write_bytes(b"my work")
    writer = Materializer(db, ns, root)
    import hashlib
    sha = hashlib.sha256(b"remote").hexdigest()
    p, _, _ = writer.write("test", Path("data.csv"), b"remote", sha, sha)
    assert p.name.casefold() != "data.csv" and (root / "DATA.csv").read_bytes() == b"my work"


def test_offline_html_security_and_no_secret_urls():
    document = offline_document("<title>", '<script>alert(1)</script><p onclick="x()">安全</p><a href="/files/1?verifier=secret&access_token=token">附件</a><img src="https://external.test/x.png"><iframe src="https://external.test/video"></iframe>', "https://canvas.test/courses/1/assignments/1", assets={}).decode()
    assert "<script" not in document and "onclick" not in document
    assert "secret" not in document and "token" not in document
    assert 'src="https://external' not in document and "未下载" in document
    assert "Content-Security-Policy" in document
    assert public_url("javascript:alert(1)") == ""


def test_namespace_isolation_and_course_selection(system):
    _, _, db, ns, courses, _, _ = system
    db.select(ns, ["1", "3"])
    assert {c["id"] for c in db.courses(ns) if c["selected"]} == {"1", "3"}
    db.term(ns, "1", "自定义学期")
    assert db.courses(ns)[0]["term_override"] == "自定义学期"
    assert db.courses("other") == [] and db.resources("other") == []


def test_retry_failed_file_and_source(system):
    server, api, db, ns, _, root, sync = system
    original = server.handle
    failed = True
    def handler(request):
        if failed and request.url.path == "/download/12":
            return httpx.Response(403)
        return original(request)
    api.http.client.close()
    api.http.client = httpx.Client(transport=httpx.MockTransport(handler))
    result = sync()
    assert result["denied"] == 1 and db.resource(ns, 1, "assignment", 501)["path"]
    failed = False
    result = sync()
    assert result["network_downloads"] == 1 and not result["incomplete"]
    assert db.resource(ns, 1, "file", 12)["status"] != "denied"
