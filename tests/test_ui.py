from pathlib import Path

from streamlit.testing.v1 import AppTest


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_first_run_demo_sync_and_offline_views(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_SYNC_HOME", str(tmp_path / "home"))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=30).run()
    assert not app.exception
    assert any("首次使用" in i.value for i in app.info)
    button(app, "体验演示数据").click().run()
    button(app, "联网刷新课程列表").click().run()
    assert not app.exception
    button(app, "全选当前结果").click().run()
    button(app, "同步选中课程（3 门）").click().run()
    assert not app.exception
    assert any("处理完成" in i.value for i in app.success)
    app.sidebar.radio[0].set_value("资料库").run()
    assert not app.exception and app.dataframe
    app.sidebar.radio[0].set_value("作业要求").run()
    assert not app.exception and len(app.selectbox) >= 3
    app.sidebar.radio[0].set_value("同步记录").run()
    assert not app.exception
    assert button(app, "重试失败项目与未完成来源").disabled


def test_settings_validation_and_no_network_on_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_SYNC_HOME", str(tmp_path / "home"))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=30).run()
    app.sidebar.radio[0].set_value("连接配置").run()
    app.text_input[0].set_value("http://unsafe.example").run()
    button(app, "测试连接并保存配置").click().run()
    assert not app.exception and any("HTTPS" in e.value for e in app.error)
    assert not (tmp_path / "home/config.json").exists()
