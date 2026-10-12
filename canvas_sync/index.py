"""仅使用本地记录生成索引；同步与清理共用。"""
import hashlib
import html
import os
from pathlib import Path
from urllib.parse import quote

from .content import date_text
from .materialize import Materializer
from .paths import under
from .store import now


def existing_path(store, ns, value, course=None):
    if not value:
        return False
    artifact = store.artifact(ns, value)
    if not artifact or artifact["state"] != "active" or (course is not None and artifact["course"] != str(course)):
        return False
    try:
        root = Path(artifact["root"])
        if root.resolve() != root:
            return False
        path = under(root, str(Path(value).relative_to(root)))
        return path.is_file()
    except (OSError, ValueError):
        return False


def write_index(store, ns, cid, course, course_dir, root, tz="Asia/Shanghai", note=""):
    root, course_dir = Path(root), Path(course_dir)
    relative = course_dir.relative_to(root)
    rows = []
    for row in store.resources(ns, cid):
        local = row.get("path")
        available = existing_path(store, ns, local, cid)
        href = quote(os.path.relpath(local, course_dir).replace(os.sep, "/")) if available else row.get("source", "")
        label = html.escape(row.get("title", ""))
        linked = f'<a href="{html.escape(href, quote=True)}">{label}</a>' if href else label
        reason = row.get("reason", "")
        if not available and row.get("status") == "cleared":
            reason += " 本地资料已清理；作业要求摘要仍可在任务清单查看。"
        rows.append(f'<tr><td>{html.escape(row["kind"])}</td><td>{linked}</td><td>{html.escape(date_text(row.get("due"), tz) if row["kind"] == "assignment" else "")}</td><td>{html.escape(row.get("status", ""))}</td><td>{html.escape(reason)}</td><td><a href="{html.escape(row.get("source", ""), quote=True)}">Canvas</a></td></tr>')
    sources = store.sources(ns, cid)
    body = "<h2>入口获取情况</h2><ul>" + ''.join(f'<li>{html.escape(s["name"])}：{html.escape(s["status"])} — {html.escape(s["detail"])}</li>' for s in sources) + "</ul><h2>本地资源</h2><table><tr><th>类型</th><th>标题</th><th>截止时间</th><th>状态</th><th>说明</th><th>来源</th></tr>" + ''.join(rows) + "</table>"
    payload = f'<!doctype html><html lang="zh"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>课程索引</title><style>body{{font:15px/1.7 system-ui;padding:24px}}td,th{{border:1px solid #ddd;padding:8px}}table{{border-collapse:collapse}}</style><h1>{html.escape(course["name"])}</h1><p>生成时间：{html.escape(date_text(now(), tz))}；默认不删除旧资料。</p><p>{html.escape(note)}</p>{body}</html>'.encode()
    digest = hashlib.sha256(payload).hexdigest()
    return Materializer(store, ns, root).write(f'{cid}:index', relative / "课程索引.html", payload, digest, digest)
