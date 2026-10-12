import json
import os
from contextlib import contextmanager

import pytest

from canvas_sync.browser_login import load_cookies, run, session_dir
from canvas_sync.config import Config, normalize_base, private_json, profile_id, save_token
from canvas_sync.errors import SyncError
from canvas_sync.lock import sync_lock


def test_config_never_stores_credentials(tmp_path):
    config = Config(base_url="https://canvas.test", profile=profile_id("https://canvas.test", 42), user_id="42")
    config.save(tmp_path)
    assert Config.load(tmp_path).user_id == "42"
    assert "token" not in json.loads((tmp_path / "config.json").read_text())
    if os.name != "nt":
        assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600


def test_no_plaintext_keyring_fallback(monkeypatch):
    import keyring
    class Plaintext:
        pass
    monkeypatch.setattr(keyring, "get_keyring", lambda: Plaintext())
    with pytest.raises(ValueError, match="仅本次使用"):
        save_token("https://canvas.test", "secret")


def test_browser_actual_response_verified_and_only_origin_cookies_saved(tmp_path, monkeypatch):
    from playwright import sync_api
    seen = {}
    class Response:
        ok = True
        def text(self):
            return 'while(1);{"id":42,"name":"学生"}'
    class Page:
        def goto(self, base, **kwargs):
            seen["base"] = base
    class Context:
        pages = [Page()]
        def get(self, url, **kwargs):
            assert url == "https://canvas.test/api/v1/users/self/profile"
            assert kwargs["max_redirects"] == 0
            return Response()
        @property
        def request(self):
            return self
        def cookies(self, urls):
            assert urls == ["https://canvas.test"]
            return [{"domain": "canvas.test", "name": "session", "value": "private", "path": "/"}]
        def close(self):
            seen["closed"] = True
    class Chromium:
        def launch_persistent_context(self, path, **kwargs):
            assert kwargs["headless"] is False
            assert kwargs["accept_downloads"] is False
            assert "browser-profile" in path
            return Context()
    class Playwright:
        chromium = Chromium()
    @contextmanager
    def fake():
        yield Playwright()
    monkeypatch.setattr(sync_api, "sync_playwright", fake)
    run(tmp_path, "https://canvas.test")
    assert seen["closed"] and load_cookies(tmp_path, "https://canvas.test")[0]["name"] == "session"
    directory = session_dir(tmp_path, "https://canvas.test")
    status = json.loads((directory / "status.json").read_text())
    assert status["state"] == "ready" and "private" not in json.dumps(status)
    prefs = json.loads((directory / "browser-profile/Default/Preferences").read_text())
    assert prefs["credentials_enable_service"] is False and prefs["profile"]["password_manager_enabled"] is False


def test_lock_prevents_parallel_sync_and_releases(tmp_path):
    path = tmp_path / "sync.lock"
    with sync_lock(path):
        with pytest.raises(SyncError, match="已有同步"):
            with sync_lock(path):
                pass
    with sync_lock(path):
        pass


@pytest.mark.parametrize("bad", ["http://canvas.test", "https://user:pass@canvas.test", "https://canvas.test/courses/1", "https://canvas.test?token=secret"])
def test_config_address_validation(bad):
    with pytest.raises(ValueError):
        normalize_base(bad)
