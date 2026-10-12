"""v0 -> v1 的兼容迁移；DDL 与回填在同一事务内。"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .paths import sha_file, under
from .tasks import upsert_canvas

VERSION = 1
STATEMENTS = [
    "ALTER TABLE courses ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE tasks(id TEXT PRIMARY KEY,ns TEXT NOT NULL,course TEXT NOT NULL,origin TEXT NOT NULL,source_id TEXT NOT NULL,
       title TEXT NOT NULL,canvas_due_raw TEXT,canvas_due TEXT,planned_due TEXT,status TEXT NOT NULL DEFAULT '未开始',notes TEXT NOT NULL DEFAULT '',
       archived INTEGER NOT NULL DEFAULT 0,requirement_html TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',
       attachments TEXT NOT NULL DEFAULT '[]',due_version INTEGER NOT NULL DEFAULT 1,schedule_since TEXT,created TEXT NOT NULL,updated TEXT NOT NULL,
       date_error TEXT NOT NULL DEFAULT '',UNIQUE(ns,course,origin,source_id))""",
    "CREATE INDEX tasks_ns ON tasks(ns,course)",
    "CREATE TABLE due_history(task TEXT,version INTEGER,before_due TEXT,after_due TEXT,changed TEXT,PRIMARY KEY(task,version))",
    """CREATE TABLE artifacts(ns TEXT,course TEXT,path TEXT,root TEXT,course_dir TEXT,logical TEXT,group_key TEXT,category TEXT,
       sha TEXT,state TEXT NOT NULL DEFAULT 'active',PRIMARY KEY(ns,path))""",
    "CREATE INDEX artifacts_course ON artifacts(ns,course,state)",
    "CREATE TABLE cleanup_runs(id TEXT PRIMARY KEY,ns TEXT,root TEXT,courses TEXT,archive INTEGER,status TEXT,created TEXT,ended TEXT)",
    "CREATE TABLE cleanup_items(run TEXT,path TEXT,payload TEXT,status TEXT,reason TEXT,PRIMARY KEY(run,path))",
    "CREATE TABLE reminder_settings(ns TEXT PRIMARY KEY,enabled INTEGER,offsets TEXT,changed TEXT)",
    """CREATE TABLE reminder_events(task TEXT,version INTEGER,offset INTEGER,trigger_at TEXT,status TEXT,snapshot TEXT,
       claimed_at TEXT,channel TEXT,error TEXT NOT NULL DEFAULT '',batch TEXT,seen INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(task,version,offset))""",
    "CREATE TABLE service_state(id INTEGER PRIMARY KEY CHECK(id=1),pid INTEGER,token TEXT,heartbeat TEXT,last_check TEXT,active_ns TEXT,status TEXT,error TEXT,stop_requested INTEGER DEFAULT 0)",
]


def artifact_fields(ns, logical, path, root=None):
    """课程归属来自稳定 logical，不用课程名猜测。根路径校验含旧版根哈希。"""
    pieces = logical.split(":")
    if len(pieces) < 3:
        return None
    path = Path(path)
    if not path.is_absolute():
        return None
    marker = f"Canvas-{ns[:10]}"
    try:
        index = path.parts.index(marker)
        course_dir = Path(*path.parts[:index + 3])
        found_root = Path(*path.parts[:index])
        if found_root.resolve() != found_root:
            return None
        if root is not None and found_root != Path(root):
            return None
        if hashlib.sha256(str(found_root).encode()).hexdigest()[:12] != pieces[0]:
            return None
        under(found_root, str(path.relative_to(found_root)))
    except (ValueError, IndexError, OSError):
        return None
    if pieces[2] in {"assignment", "module", "page"} and len(pieces) >= 4:
        group = ":".join(pieces[1:4])
    elif pieces[2] == "index":
        group = f"{pieces[1]}:index"
    else:
        group = f"{pieces[1]}:folder:{path.parent}"
    category = "index" if pieces[2] == "index" else "document" if pieces[-1] in {"document", "doc"} else "file"
    if "snapshot" in pieces:
        category = "snapshot"
    return {"course": pieces[1], "root": str(found_root), "course_dir": str(course_dir), "group_key": group, "category": category}


def record_artifact(db, ns, path, logical, sha, fields, category=None):
    if not fields:
        return
    db.execute("INSERT INTO artifacts(ns,course,path,root,course_dir,logical,group_key,category,sha,state) VALUES(?,?,?,?,?,?,?,?,?,'active') "
               "ON CONFLICT(ns,path) DO UPDATE SET sha=excluded.sha,state='active'",
               (ns, fields["course"], str(path), fields["root"], fields["course_dir"], logical, fields["group_key"], category or fields["category"], sha))


def migrate(db):
    for statement in STATEMENTS:
        db.execute(statement)
    timestamp = datetime.now(timezone.utc).isoformat()
    for row in db.execute("SELECT * FROM placements").fetchall():
        fields = artifact_fields(row["ns"], row["logical"], row["path"])
        record_artifact(db, row["ns"], row["path"], row["logical"], row["sha"], fields)
    # 旧资源记录中的远程副本，有原文件归属与远程完整哈希才回填。
    for row in db.execute("SELECT * FROM resources").fetchall():
        data = json.loads(row["payload"])
        remote, digest = data.get("remote_path"), data.get("sha")
        primary = db.execute("SELECT * FROM artifacts WHERE ns=? AND course=? AND path=?", (row["ns"], row["course"], data.get("path", ""))).fetchone()
        if not remote or not digest or not primary:
            continue
        fields = artifact_fields(row["ns"], primary["logical"], remote)
        if not fields or fields["course_dir"] != primary["course_dir"] or fields["course"] != row["course"]:
            continue
        try:
            candidate = under(Path(fields["root"]), str(Path(remote).relative_to(fields["root"])))
            if candidate.is_file() and sha_file(candidate) == digest:
                record_artifact(db, row["ns"], candidate, primary["logical"], digest, fields, "remote")
        except (OSError, ValueError):
            pass
    # 老版未登记历史版本：须有当前路径关联和已保存的完整内容哈希证据。
    for row in db.execute("SELECT * FROM artifacts").fetchall():
        p = Path(row["path"])
        history = p.parent / "_Canvas历史版本"
        fields = dict(row)
        try:
            history = under(Path(row["root"]), str(history.relative_to(row["root"])))
            if not history.is_dir():
                continue
            tokens = row["logical"].split(":")
            fid = tokens[tokens.index("file") + 1] if "file" in tokens and tokens.index("file") + 1 < len(tokens) else None
            known = {r[0] for r in db.execute("SELECT sha FROM blobs WHERE ns=? AND id=?", (row["ns"], fid))} | {row["sha"]}
            for candidate in history.iterdir():
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if not re.fullmatch(re.escape(p.stem) + r"~[0-9a-f]{12}" + re.escape(p.suffix), candidate.name):
                    continue
                digest = sha_file(candidate)
                if digest in known and candidate.stem.endswith("~" + digest[:12]):
                    record_artifact(db, row["ns"], candidate, row["logical"], digest, fields, "history")
        except (OSError, ValueError):
            pass
    for row in db.execute("SELECT * FROM resources WHERE kind='assignment'").fetchall():
        data = json.loads(row["payload"])
        body = None
        for value in (data.get("remote_path"), data.get("path")):
            if not value:
                continue
            artifact = db.execute("SELECT * FROM artifacts WHERE ns=? AND path=?", (row["ns"], value)).fetchone()
            if not artifact or artifact["course"] != row["course"]:
                continue
            try:
                path = under(Path(artifact["root"]), str(Path(value).relative_to(artifact["root"])))
                soup = BeautifulSoup(path.read_text("utf-8"), "html.parser")
                body = str(soup.body or soup)
                break
            except (OSError, ValueError, UnicodeError):
                continue
        upsert_canvas(db, row["ns"], row["course"], {"id": row["id"], "name": data.get("title"), "description": body,
            "due_at": data.get("due"), "html_url": data.get("source"), "attachments": data.get("attachments", [])}, timestamp)
    db.execute(f"PRAGMA user_version={VERSION}")
