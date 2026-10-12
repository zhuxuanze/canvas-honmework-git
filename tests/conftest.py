import pytest

from canvas_sync.config import profile_id
from canvas_sync.demo import DemoServer
from canvas_sync.engine import SyncEngine
from canvas_sync.store import Store


@pytest.fixture
def system(tmp_path):
    server = DemoServer()
    api = server.canvas()
    db = Store(tmp_path / "state.sqlite3")
    ns = profile_id(api.base, 9001)
    courses = list(api.courses())
    for course in courses:
        db.save_course(ns, course)
    root = tmp_path / "资料"
    def sync(courses_to_sync=None):
        return SyncEngine(api, db, ns, root).sync(courses_to_sync or courses[:1])
    yield server, api, db, ns, courses, root, sync
    api.http.close()
