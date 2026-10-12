import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .errors import AuthExpired, InvalidResponse, NetworkError, PermissionDenied


def origin(url: str):
    p = urlsplit(url)
    return (p.scheme.lower(), (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80))


def is_login(body: bytes) -> bool:
    text = body[:65536].decode("utf-8", errors="ignore").lower()
    return ("<html" in text or "<form" in text) and bool(re.search(
        r'type\s*=\s*["\x27]?password|/login|sign[ -]?in|log[ -]?in|统一身份|登录', text))


class ReadOnlyHTTP:
    """仅 GET；逐跳重新计算认证头，跨域永远不发送 Canvas 凭据。"""
    def __init__(self, base: str, token: str | None = None, cookies=None,
                 transport=None, sleep=time.sleep, attempts=4):
        self.base = base.rstrip("/")
        self.token = token
        self.cookies = cookies or []
        self.sleep = sleep
        self.attempts = attempts
        self.client = httpx.Client(transport=transport, timeout=httpx.Timeout(30, connect=15),
                                   follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def _headers(self, url):
        headers = {"User-Agent": "CanvasLocalSync/0.1", "Accept": "application/json, */*"}
        if origin(url) == origin(self.base):
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            p = urlsplit(url)
            valid = [c for c in self.cookies if c.get("domain", "").lstrip(".") and
                     (c.get("domain", "").lstrip(".").lower() == p.hostname
                      or p.hostname.endswith("." + c.get("domain", "").lstrip(".").lower()))
                     and (c.get("expires", -1) <= 0 or c["expires"] > time.time())
                     and (not c.get("secure") or p.scheme == "https")
                     and (p.path == c.get("path", "/") or p.path.startswith(c.get("path", "/").rstrip("/") + "/"))]
            if valid:
                headers["Cookie"] = "; ".join(f'{c["name"]}={c["value"]}' for c in valid)
        return headers

    def _delay(self, attempt, retry_after=None):
        delay = 2 ** attempt + random.random() * 0.3
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError):
                    pass
        self.sleep(max(0, min(delay, 30)))

    def _open(self, url, params=None, api=False):
        next_url = urljoin(self.base + "/", url)
        if api and origin(next_url) != origin(self.base):
            raise InvalidResponse("API 或分页链接指向其他域名，已停止读取")
        for hop in range(7):
            parsed = urlsplit(next_url)
            if parsed.username or parsed.password or parsed.scheme not in {"https", "http"}:
                raise InvalidResponse("不安全的下载地址")
            if parsed.scheme == "http" and origin(next_url) != origin(self.base):
                raise InvalidResponse("下载跳转到了不安全的 HTTP 域名")
            if hop and re.search(r"/(login|signin|sso)(/|\?|$)", parsed.path, re.I):
                raise AuthExpired("Canvas 会话失效或跳转到登录，请重新登录")
            for attempt in range(self.attempts):
                try:
                    # 不用 build_request，避免 Client 的跨响应 Cookie jar 自动参与认证。
                    headers = self._headers(next_url)
                    headers["Accept"] = "application/json" if api else "*/*"
                    request = httpx.Request("GET", next_url, params=params, headers=headers)
                    response = self.client.send(request, stream=True)
                except httpx.HTTPError:
                    if attempt + 1 == self.attempts:
                        raise NetworkError("网络连接失败或超时，已完成有限重试") from None
                    self._delay(attempt)
                    continue
                if response.status_code == 429 or response.status_code in {500, 502, 503, 504}:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    response.close()
                    if attempt + 1 == self.attempts:
                        raise NetworkError(f"服务器返回 HTTP {status}，重试次数已用完")
                    self._delay(attempt, retry_after)
                    continue
                break
            if response.status_code in {301, 302, 303, 307, 308}:
                target = response.headers.get("Location")
                response.close()
                if not target:
                    raise InvalidResponse("下载跳转缺少目标地址")
                next_url = urljoin(str(response.url), target)
                params = None
                if api and origin(next_url) != origin(self.base):
                    raise AuthExpired("接口跳转到外部登录系统，请重新登录")
                continue
            if response.status_code == 401:
                response.close()
                raise AuthExpired("Canvas 认证失效，请重新登录或检查 Token")
            if response.status_code in {403, 404}:
                code = response.status_code
                response.close()
                raise PermissionDenied(f"HTTP {code}：无权限、资源不存在或尚未解锁")
            if not 200 <= response.status_code < 300:
                code = response.status_code
                response.close()
                raise NetworkError(f"服务器返回 HTTP {code}")
            return response
        raise InvalidResponse("下载跳转过多，已停止")

    def json(self, url, params=None):
        response = self._open(url, params, api=True)
        try:
            body = response.read()
            if is_login(body):
                raise AuthExpired("接口返回登录页面，请重新登录")
            try:
                text = body.decode("utf-8-sig")
                # 浏览器会话调用 Canvas JSON 接口可能有防 JSON 劫持前缀。
                text = re.sub(r"^\s*(?:while\s*\(1\)\s*;|for\s*\(;;\)\s*;)", "", text)
                data = json.loads(text)
            except (ValueError, UnicodeError):
                raise InvalidResponse("接口未返回可识别的 JSON；学校会话可能不支持此 API") from None
            return data, response.links
        except httpx.HTTPError:
            raise NetworkError("读取接口响应时连接中断，请重试") from None
        finally:
            response.close()

    def paginate(self, url, params=None):
        params = {"per_page": 100, **(params or {})}
        seen = set()
        while url:
            if url in seen:
                raise InvalidResponse("分页链接形成循环，列表可能不完整")
            seen.add(url)
            data, links = self.json(url, params)
            if not isinstance(data, list):
                raise InvalidResponse("列表接口返回了非列表数据")
            yield from data
            url = links.get("next", {}).get("url")
            params = None  # Link 是不透明的绝对 URL，不重新拼接参数。

    def download(self, url: str, destination: Path, expected_size: int | None, filename: str):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + f".{uuid.uuid4().hex}.part")
        for attempt in range(self.attempts):
            response = None
            try:
                response = self._open(url)
                total = 0
                head = bytearray()
                with temp.open("wb") as stream:
                    for chunk in response.iter_bytes(1024 * 128):
                        if len(head) < 65536:
                            head.extend(chunk[:65536 - len(head)])
                        stream.write(chunk)
                        total += len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if is_login(bytes(head)):
                    raise AuthExpired("下载返回了登录页面，请重新登录")
                if Path(filename).suffix.lower() not in {".html", ".htm"} and (
                    "text/html" in response.headers.get("Content-Type", "").lower()
                    or bytes(head).lstrip().lower().startswith((b"<!doctype html", b"<html"))):
                    raise InvalidResponse("下载返回了 HTML 页面，未保存为附件")
                if expected_size is not None and total != expected_size:
                    raise NetworkError("文件大小与 Canvas 元数据不符，可能下载中断")
                if filename.lower().endswith(".ipynb"):
                    try:
                        notebook = json.loads(temp.read_text("utf-8"))
                        if not isinstance(notebook, dict) or "cells" not in notebook or "nbformat" not in notebook:
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        raise InvalidResponse("下载内容不是有效的 Jupyter notebook") from None
                os.replace(temp, destination)
                return
            except (httpx.HTTPError, NetworkError):
                if attempt + 1 == self.attempts:
                    raise NetworkError("文件下载中断或不完整，已完成有限重试") from None
                self._delay(attempt)
            finally:
                if response:
                    response.close()
                temp.unlink(missing_ok=True)
