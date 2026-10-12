import hashlib
import html
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .http import origin

SECRET_KEYS = re.compile(r"token|verifier|signature|credential|key|policy|expires|session|auth|jwt|x-amz", re.I)


def public_url(url: str, base=""):
    value = urljoin(base + "/", url)
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or p.username or p.password:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not SECRET_KEYS.search(k)])
    return urlunsplit((p.scheme, p.netloc, p.path, query, ""))


def refs(body: str, base: str):
    files, links = {}, {}
    soup = BeautifulSoup(body or "", "html.parser")
    for tag in soup.find_all(["a", "img", "iframe", "video", "audio", "source"]):
        url = tag.get("data-api-endpoint") or tag.get("href") or tag.get("src") or ""
        absolute = urljoin(base + "/", url)
        parsed = urlsplit(absolute)
        match = re.search(r"/(?:api/v1/)?(?:courses/\d+/)?files/(\d+)(?:/|$)", parsed.path)
        if match and origin(absolute) == origin(base):
            files[match[1]] = tag.get_text(strip=True) or tag.get("alt") or "Canvas 附件"
        elif parsed.scheme in {"http", "https"}:
            clean = public_url(absolute)
            if clean:
                links[clean] = tag.get_text(strip=True) or tag.get("alt") or "外部或未适配的资源"
    return files, links


def date_text(value, tz="Asia/Shanghai"):
    if not value:
        return "未设置截止时间"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return "截止时间格式无法识别，请查看 Canvas"


def offline_document(title, body, source, due=None, assets=None, tz="Asia/Shanghai", note="", links=None, local_links=None):
    assets = assets or {}
    soup = BeautifulSoup(body or "<p>此内容没有正文。</p>", "html.parser")
    # 保留题目结构、表格和文字；移除可执行内容及所有远程主动加载。
    for tag in soup.find_all(["script", "style", "object", "embed", "base", "meta", "link", "form", "input", "button"]):
        tag.decompose()
    for tag in list(soup.find_all(["iframe", "video", "audio", "source"])):
        if tag.parent is None:
            continue
        tag.replace_with(soup.new_string("[媒体资源未下载，请查看原页面]"))
    for tag in soup.find_all(True):
        for key in list(tag.attrs):
            if key.lower().startswith("on") or key.lower() in {"style", "srcset", "action", "formaction", "background", "poster", "srcdoc"}:
                del tag.attrs[key]
        for attr in ("href", "src"):
            if not tag.has_attr(attr):
                continue
            absolute = urljoin(source, tag.get("data-api-endpoint") or tag[attr])
            match = re.search(r"/(?:api/v1/)?(?:courses/\d+/)?files/(\d+)(?:/|$)", urlsplit(absolute).path)
            if attr == "href" and local_links and absolute in local_links:
                tag[attr] = local_links[absolute]
            elif match and origin(absolute) == origin(source) and match[1] in assets:
                tag[attr] = assets[match[1]]
            elif attr == "src":
                # 外部图片仅留说明，避免离线查看触发联网追踪或二次认证。
                del tag[attr]
                tag["alt"] = "图片未下载；请查看原页面。 " + tag.get("alt", "")
            else:
                clean = public_url(absolute)
                if clean:
                    tag[attr] = clean
                    tag["rel"] = "noopener noreferrer"
                else:
                    del tag[attr]
        tag.attrs.pop("data-api-endpoint", None)
    external = ""
    if links:
        external = '<h2>未下载的链接</h2><ul>' + ''.join(
            f'<li><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a> — 未下载，需联网或另行登录</li>'
            for url, label in links.items()) + '</ul>'
    deadline = f"<p>当前账户截止时间：{html.escape(date_text(due, tz))}</p>" if due is not False else ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline';">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.8 system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 24px;color:#182a3a}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccd6de;padding:8px}}img{{max-width:100%}}pre{{white-space:pre-wrap}}a{{color:#087c83}}.note{{background:#fff3ce;padding:12px}}</style></head>
<body><h1>{html.escape(title)}</h1>{deadline}<p><a href="{html.escape(public_url(source), quote=True)}">Canvas 原页面</a></p>
<p class="note">{html.escape(note or '本地离线副本；以 Canvas 当前内容为准。')}</p><hr>{soup}{external}</body></html>'''.encode("utf-8")


def revision(data):
    return hashlib.sha256(repr((data.get("updated_at"), data.get("modified_at"), data.get("size"), data.get("filename"))).encode()).hexdigest()[:24]
