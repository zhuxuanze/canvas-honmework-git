from pathlib import Path

from streamlit.testing.v1 import AppTest

from canvas_sync.config import Config, profile_id
from canvas_sync.demo import BASE
from canvas_sync.store import Store
from canvas_sync.tasks import Tasks


def button(app, label):
    return next(b for b in app.button if b.label == label)


def field(app, kind, label):
    return next(f for f in getattr(app, kind) if f.label == label)


def demo_app(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CANVAS_SYNC_HOME", str(home))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=30).run()
    button(app, "体验演示数据").click().run()
    button(app, "联网刷新课程列表").click().run()
    button(app, "同步这门课程").click().run()
    assert not app.exception
    return app, home, profile_id(BASE, "9001")


def test_task_manual_add_edit_filter_and_completed_hidden(tmp_path, monkeypatch):
    app, home, ns = demo_app(tmp_path, monkeypatch)
    app.sidebar.radio[0].set_value("任务清单").run()
    assert not app.exception
    field(app, "text_input", "新任务标题").set_value("复习第3章")
    field(app, "text_area", "新任务备注").set_value("重点复习算法")
    button(app, "添加本地任务").click().run()
    assert not app.exception
    store = Store(home / "state.sqlite3")
    key = next(t["id"] for t in Tasks(store).list(ns) if t["title"] == "复习第3章")
    app.selectbox(key="task_pick").set_value(key).run()
    field(app, "selectbox", "本地状态").set_value("已完成")
    field(app, "text_area", "本地备注").set_value("完成了")
    button(app, "保存本地任务").click().run()
    assert not app.exception
    task = Tasks(store).get(ns, key)
    assert task["status"] == "已完成" and task["notes"] == "完成了"
    assert key not in [t["id"] for t in Tasks(store).list(ns)]
    app.checkbox(key="task_completed").check().run()
    app.selectbox(key="task_pick").set_value(key).run()
    field(app, "selectbox", "本地状态").set_value("进行中")
    button(app, "保存本地任务").click().run()
    assert Tasks(store).get(ns, key)["status"] == "进行中"


def test_cleanup_preview_confirmation_and_missing_attachments(tmp_path, monkeypatch):
    from canvas_sync import cleanup
    trash = tmp_path / "模拟废纸篓"
    trash.mkdir()
    calls = []
    def fake(path):
        calls.append(path)
        path.rename(trash / (str(len(calls)) + "-" + path.name))
    monkeypatch.setattr(cleanup, "system_trash", fake)
    app, home, ns = demo_app(tmp_path, monkeypatch)
    app.sidebar.radio[0].set_value("课程管理").run()
    app.multiselect(key=f"cleanup_courses:{ns}").set_value(["1"]).run()
    button(app, "预览清理范围").click().run()
    assert not calls and not app.exception
    identifier = app.session_state.cleanup_preview
    assert button(app, "确认执行清理").disabled
    app.checkbox(key=f"cleanup_confirm:{identifier}").check().run()
    button(app, "确认执行清理").click().run()
    assert calls and not app.exception
    assert any("已取消" in m.value for m in app.markdown)
    app.sidebar.radio[0].set_value("任务清单").run()
    assert not app.exception
    attachment_buttons = [b for b in app.button if b.label == "打开附件位置"]
    assert attachment_buttons and all(b.disabled for b in attachment_buttons)
    assert any("读取数据" in t.value for t in app.text)


def test_demo_reminder_controls_safe_and_settings_persist(tmp_path, monkeypatch):
    app, home, ns = demo_app(tmp_path, monkeypatch)
    app.sidebar.radio[0].set_value("提醒中心").run()
    assert not app.exception
    assert button(app, "发送测试通知").disabled and button(app, "启动提醒服务").disabled
    field(app, "checkbox", "启用 DDL 提醒").uncheck()
    button(app, "保存提醒设置").click().run()
    from canvas_sync.reminders import settings
    assert not settings(Store(home / "state.sqlite3"), ns)["enabled"]


def test_test_notification_uses_mock_notifier_and_displays_receipt_limit(tmp_path, monkeypatch):
    home = tmp_path / "real-like-home"
    monkeypatch.setenv("CANVAS_SYNC_HOME", str(home))
    Config(base_url="https://canvas.test", user_id="42", profile=profile_id("https://canvas.test", 42)).save(home)
    from canvas_sync import reminders
    calls = []
    def fake(self, title, message):
        calls.append((title, message))
        return {"channel": "test", "error": ""}
    monkeypatch.setattr(reminders.MacNotifier, "send", fake)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=30).run()
    app.sidebar.radio[0].set_value("提醒中心").run()
    button(app, "发送测试通知").click().run()
    assert calls and not app.exception
    assert any("不代表已展示" in s.value for s in app.success)


def test_service_start_and_stop_controls_use_cooperative_request(tmp_path, monkeypatch):
    home = tmp_path / "service-ui"
    monkeypatch.setenv("CANVAS_SYNC_HOME", str(home))
    Config(base_url="https://canvas.test", user_id="42", profile=profile_id("https://canvas.test", 42)).save(home)
    from canvas_sync import reminders
    active, calls = {"running": False}, []
    def state(home):
        return {"running": active["running"], "healthy": active["running"], "last_check": None, "error": ""}
    def start(home):
        calls.append("start")
        active["running"] = True
        return "已请求启动"
    def stop(home):
        calls.append("stop")
        active["running"] = False
        return "已请求停止"
    monkeypatch.setattr(reminders, "service_status", state)
    monkeypatch.setattr(reminders, "start_service", start)
    monkeypatch.setattr(reminders, "stop_service", stop)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=30).run()
    app.sidebar.radio[0].set_value("提醒中心").run()
    assert button(app, "停止提醒服务").disabled
    button(app, "启动提醒服务").click().run()
    button(app, "刷新提醒服务状态").click().run()
    assert button(app, "启动提醒服务").disabled and not button(app, "停止提醒服务").disabled
    button(app, "停止提醒服务").click().run()
    assert calls == ["start", "stop"] and not app.exception
