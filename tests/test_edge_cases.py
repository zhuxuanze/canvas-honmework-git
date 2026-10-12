import hashlib
import json
from pathlib import Path

import httpx
import pytest

from canvas_sync.content import refs
from canvas_sync.http import ReadOnlyHTTP


def change_handler(api, handler):
    api.http.close()
    api.http.client = httpx.Client(transport=httpx.MockTransport(handler))


def test_partial_pagination_keeps_first_page_and_reports_source(system):
    server, api, db, ns, _, root, sync = system
    original = server.handle
    def handler(request):
        if request.url.path.endswith("/files") and request.url.params.get("page") == "2":
            return httpx.Response(503)
        return original(request)
    change_handler(api, handler)
    result = sync()
    source = next(s for s in db.sources(ns, 1) if s["name"] == "Files")
    assert source["status"] == "partial" and "2 项" in source["detail"]
    assert result["incomplete"] and db.resource(ns, 1, "file", 11)["path"]


def test_locked_module_is_not_read_or_progressed(system):
    server, api, db, ns, _, root, sync = system
    original = server.handle
    def handler(request):
        assert request.method == "GET"
        assert "mark_read" not in request.url.path and "progress" not in request.url.path
        if request.url.path.endswith("/modules"):
            return httpx.Response(200, json=[{"id": 701, "name": "未解锁模块", "state": "locked", "items_count": 4}])
        return original(request)
    change_handler(api, handler)
    result = sync()
    assert result["incomplete"]
    assert server.calls["/api/v1/courses/1/modules/701/items"] == 0
    assert db.resource(ns, 1, "assignment", 501)["path"]  # 其他已授权入口仍然可用。


def test_nested_csv_notebook_relative_paths(system):
    server, api, db, ns, _, root, sync = system
    server.contents["11"] = json.dumps({"cells": [{"source": ["open('data/data.csv')"], "cell_type": "code", "metadata": {}, "outputs": [], "execution_count": None}], "nbformat": 4, "nbformat_minor": 5, "metadata": {}}).encode()
    original = server.handle
    def handler(request):
        if request.url.path.endswith("/folders"):
            return httpx.Response(200, json=[{"id": 100, "name": "course files", "parent_folder_id": None}, {"id": 101, "name": "第一周", "parent_folder_id": 100}, {"id": 102, "name": "data", "parent_folder_id": 101}])
        response = original(request)
        if request.url.path.endswith("/files") or request.url.path.startswith("/api/v1/files/"):
            data = response.json()
            for f in data if isinstance(data, list) else [data]:
                if f.get("id") == 12:
                    f["folder_id"] = 102
            return httpx.Response(200, json=data, headers=dict(response.headers))
        return response
    change_handler(api, handler)
    sync()
    doc = Path(db.resource(ns, 1, "assignment", 501)["path"])
    assert (doc.parent / "analysis.ipynb").is_file()
    assert (doc.parent / "data/data.csv").read_bytes() == server.contents["12"]


def test_module_offline_links_point_to_saved_documents(system):
    _, _, db, ns, _, root, sync = system
    sync()
    docs = list(root.glob("Canvas-*/*/*/课程资料/Modules/*/_Canvas要求.html"))
    text = docs[0].read_text()
    assert "../../../" in text and "作业" in text and "课程页面" in text
    assert "未下载的链接" not in text


def test_metadata_does_not_export_signed_urls(system):
    server, api, db, ns, _, root, sync = system
    original_file = server.file
    def file(rid):
        data = original_file(rid)
        data["url"] += "?verifier=super-secret"
        return data
    server.file = file
    sync()
    exported = json.dumps(db.resources(ns), ensure_ascii=False)
    assert "super-secret" not in exported and "verifier" not in exported
    assert b"super-secret" not in db.path.read_bytes()


def test_canvas_image_data_api_endpoint():
    files, links = refs('<img src="https://cdn.test/image.png" data-api-endpoint="https://canvas.test/api/v1/courses/1/files/55">', "https://canvas.test")
    assert "55" in files


def test_changed_term_creates_new_directory_and_preserves_old(system):
    _, _, db, ns, courses, root, sync = system
    sync()
    old = Path(db.resource(ns, 1, "assignment", 501)["path"])
    course = dict(courses[0], term_override="自定义学期")
    sync([course])
    new = Path(db.resource(ns, 1, "assignment", 501)["path"])
    assert new != old and old.exists() and new.exists()


def test_same_named_courses_use_stable_ids(system):
    _, _, db, ns, courses, root, sync = system
    selected = [dict(c, name="同名课程") for c in courses[:2]]
    sync(selected)
    one = Path(db.resource(ns, 1, "assignment", 501)["path"])
    two = Path(db.resource(ns, 2, "assignment", 501)["path"])
    assert one != two and "课程1" in str(one) and "课程2" in str(two)


def test_missing_body_does_not_replace_cached_requirements(system):
    server, api, db, ns, _, root, sync = system
    sync()
    doc = Path(db.resource(ns, 1, "assignment", 501)["path"])
    before = doc.read_bytes()
    original = server.handle
    def handler(request):
        if request.url.path.endswith("/assignments"):
            return httpx.Response(200, json=[{"id": 501, "name": "作业一：Python 数据分析"}])
        if request.url.path.endswith("/assignments/501"):
            return httpx.Response(200, json={"id": 501, "name": "作业一：Python 数据分析"})
        return original(request)
    change_handler(api, handler)
    result = sync()
    assert result["incomplete"] and doc.read_bytes() == before
    assert db.resource(ns, 1, "assignment", 501)["status"] == "failed"


def test_unknown_list_shape_reports_partial_instead_of_success(system):
    server, api, db, ns, _, root, sync = system
    original = server.handle
    def handler(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[server.file("11"), {"error": "unexpected"}])
        return original(request)
    change_handler(api, handler)
    result = sync()
    assert result["incomplete"]
    assert next(s for s in db.sources(ns, 1) if s["name"] == "Files")["status"] == "partial"


def test_locked_module_item_error_resolved_after_unlock(system):
    server, api, db, ns, _, root, sync = system
    original = server.handle
    locked = True
    def handler(request):
        response = original(request)
        if locked and request.url.path.endswith("/items"):
            data = response.json()
            for item in data:
                if item["id"] == 1:
                    item["content_details"] = {"locked_for_user": True}
            return httpx.Response(200, json=data, headers=dict(response.headers))
        return response
    change_handler(api, handler)
    assert sync()["denied"] == 1
    locked = False
    assert not sync()["incomplete"]
    assert db.resource(ns, 1, "module_item", "701:1")["status"] == "skipped"


@pytest.mark.parametrize("suffix", [".py", ".ipynb", ".command", ".ps1", ".exe", ".pl", ".js", ".sh"])
def test_script_open_only_reveals_in_file_manager(tmp_path, monkeypatch, suffix):
    from canvas_sync import platform
    calls = []
    monkeypatch.setattr(platform.sys, "platform", "darwin")
    monkeypatch.setattr(platform.subprocess, "Popen", calls.append)
    file = tmp_path / ("script" + suffix)
    file.write_text("test")
    platform.open_local(str(file), str(tmp_path))
    assert calls == [["open", "-R", str(file)]]
