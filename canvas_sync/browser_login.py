"""独立进程的可见浏览器登录；不读取或保存密码。"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import normalize_base, private_json, token_key
from .paths import private_dir


def session_dir(home: Path, base: str):
    return private_dir(home / "sessions" / token_key(base)[:20])


def load_cookies(home: Path, base: str):
    p = session_dir(home, base) / "session.json"
    if not p.exists():
        return []
    return json.loads(p.read_text("utf-8")).get("cookies", [])


def launch(home: Path, base: str):
    directory = session_dir(home, base)
    status = directory / "status.json"
    if status.exists():
        current = json.loads(status.read_text("utf-8"))
        if current.get("state") == "waiting":
            try:
                os.kill(current["pid"], 0)
                return
            except (ProcessLookupError, OSError, KeyError):
                pass
    private_json(status, {"state": "starting", "message": "正在启动独立登录浏览器"})
    subprocess.Popen([sys.executable, "-m", "canvas_sync.browser_login", "--home", str(home), "--base", base],
                     cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(home: Path, base: str):
    from playwright.sync_api import sync_playwright
    directory = session_dir(home, base)
    status = directory / "status.json"
    context = None
    try:
        with sync_playwright() as playwright:
            browser_profile = private_dir(directory / "browser-profile")
            prefs = browser_profile / "Default/Preferences"
            data = json.loads(prefs.read_text("utf-8")) if prefs.exists() else {}
            data["credentials_enable_service"] = False
            data.setdefault("profile", {})["password_manager_enabled"] = False
            private_json(prefs, data)
            # 学校登录依然手动完成。已有 Chrome/Edge 可减少额外浏览器安装成本。
            for channel in (None, "chrome", "msedge"):
                try:
                    options = {"headless": False, "args": ["--disable-save-password-bubble"],
                               "accept_downloads": False, "viewport": {"width": 1100, "height": 780}}
                    if channel:
                        options["channel"] = channel
                    context = playwright.chromium.launch_persistent_context(str(browser_profile), **options)
                    break
                except Exception:
                    continue
            if context is None:
                raise RuntimeError("browser unavailable")
            page = context.pages[0] if context.pages else context.new_page()
            private_json(status, {"state": "waiting", "pid": os.getpid(),
                                  "message": "请在浏览器手动完成学校登录；会话接口验证最长等待 15 分钟"})
            page.goto(base, wait_until="domcontentloaded", timeout=60000)
            for _ in range(300):
                try:
                    response = context.request.get(base + "/api/v1/users/self/profile", timeout=10000,
                                                   max_redirects=0)
                    text = response.text().removeprefix("while(1);")
                    identity = json.loads(text)
                    if response.ok and isinstance(identity, dict) and identity.get("id"):
                        cookies = context.cookies([base])
                        private_json(directory / "session.json", {"cookies": cookies, "verified_at": time.time()})
                        private_json(status, {"state": "ready", "message": "浏览器会话可以读取账户 API，请回到界面测试连接"})
                        context.close()
                        return
                except Exception:
                    pass  # 登录页/SSO 响应不写日志，继续等待用户。
                time.sleep(3)
            private_json(status, {"state": "unavailable", "message": "尚未验证 API。学校可能禁止会话调用、尚未登录或登录超时。可重新登录或使用学校允许的 Token。页面抓取需要真实响应另行适配。"})
            context.close()
    except Exception:
        private_json(status, {"state": "error", "message": "浏览器未成功启动或已关闭。请安装 Chromium（安装浏览器脚本）或使用已安装的 Chrome/Edge，关闭此前的登录窗口后重试。"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    run(Path(args.home), normalize_base(args.base))
