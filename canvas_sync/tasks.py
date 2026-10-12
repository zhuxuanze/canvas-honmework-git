"""独立于下载文件和 Canvas 登录的本地任务。"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .content import offline_document, public_url

STATUSES = ("未开始", "进行中", "已完成")


def utc_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("日期必须包含时区")
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def effective_due(task):
    return task.get("canvas_due") or task.get("planned_due") or None


def clean_requirements(body, title, source):
    document = offline_document(title, body, source, due=False).decode("utf-8")
    soup = BeautifulSoup(document, "html.parser")
    return document, soup.get_text("\n", strip=True)


def task_key(ns, course, origin, source_id):
    return hashlib.sha256(json.dumps([ns, str(course), origin, str(source_id)]).encode()).hexdigest()


def upsert_canvas(db, ns, course, assignment, timestamp):
    aid = str(assignment["id"])
    key = task_key(ns, course, "canvas", aid)
    row = db.execute("SELECT * FROM tasks WHERE id=?", (key,)).fetchone()
    due = assignment.get("due_at")
    error = ""
    try:
        normalized = iso(utc_time(due)) if due else None
    except (ValueError, TypeError):
        normalized = None
        error = "Canvas 截止时间格式无法识别，未安排提醒"
    title = str(assignment.get("name") or "未命名作业")
    source = public_url(assignment.get("html_url") or "")
    body = assignment.get("description")
    requirement_html, summary = clean_requirements(body, title, source) if body is not None else ("", "要求尚未缓存，请重新同步")
    if row:
        previous = effective_due(dict(row))
        current = normalized or row["planned_due"]
        changed = previous != current
        version = row["due_version"] + int(changed)
        db.execute("UPDATE tasks SET title=?,canvas_due_raw=?,canvas_due=?,requirement_html=?,summary=?,source=?,attachments=?,updated=?,due_version=?,date_error=? WHERE id=?",
                   (title, due, normalized, requirement_html, summary, source,
                    json.dumps(assignment.get("attachments", [])), timestamp, version, error, key))
        if changed:
            _date_changed(db, key, row["canvas_due_raw"] or row["planned_due"], due or row["planned_due"], version, timestamp)
    else:
        db.execute("INSERT INTO tasks(id,ns,course,origin,source_id,title,canvas_due_raw,canvas_due,requirement_html,summary,source,attachments,created,updated,date_error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (key, ns, str(course), "canvas", aid, title, due, normalized, requirement_html,
                    summary, source, json.dumps(assignment.get("attachments", [])), timestamp, timestamp, error))
    return key


def _date_changed(db, key, before, after, version, timestamp):
    db.execute("INSERT INTO due_history(task,version,before_due,after_due,changed) VALUES(?,?,?,?,?)", (key, version, before, after, timestamp))
    db.execute("UPDATE reminder_events SET status='cancelled' WHERE task=? AND status='pending'", (key,))
    # 改期只安排未来节点；更新时间也作为重新打开/恢复提醒的基线。
    db.execute("UPDATE tasks SET schedule_since=? WHERE id=?", (timestamp, key))


class Tasks:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def canvas(self, ns, course, assignment):
        timestamp = iso(self.clock())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return upsert_canvas(db, ns, course, assignment, timestamp)

    def get(self, ns, key):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE ns=? AND id=?", (ns, key)).fetchone()
        return dict(row) if row else None

    def list(self, ns, course=None, status=None, search="", view="全部", show_completed=False, show_archived=False):
        with self.store.connect() as db:
            rows = db.execute("SELECT t.*,COALESCE(c.archived,0) AS course_archived FROM tasks t LEFT JOIN courses c ON t.ns=c.ns AND t.course=c.id WHERE t.ns=?", (ns,)).fetchall()
        current = self.clock().astimezone(timezone.utc)
        result = []
        for row in rows:
            task = dict(row)
            if not show_archived and (task["archived"] or task["course_archived"]):
                continue
            if not show_completed and task["status"] == "已完成":
                continue
            if course and task["course"] != str(course):
                continue
            if status and task["status"] != status:
                continue
            if search.casefold() not in task["title"].casefold():
                continue
            due = utc_time(effective_due(task))
            if view == "今天到期" and (not due or due.astimezone(ZoneInfo("Asia/Shanghai")).date() != current.astimezone(ZoneInfo("Asia/Shanghai")).date()):
                continue
            if view == "未来 7 天" and (not due or not current <= due < current + timedelta(days=7)):
                continue
            if view == "已逾期" and (not due or due >= current):
                continue
            if view == "无日期" and due:
                continue
            result.append(task)
        return sorted(result, key=lambda t: (effective_due(t) is None, effective_due(t) or "", t["title"]))

    def add(self, ns, title, course="personal", planned_due=None, notes=""):
        title = str(title).strip()
        if not title:
            raise ValueError("任务标题不能为空")
        due = iso(utc_time(planned_due)) if planned_due else None
        key = task_key(ns, course, "manual", uuid.uuid4().hex)
        timestamp = iso(self.clock())
        with self.store.connect() as db:
            if course != "personal" and not db.execute("SELECT 1 FROM courses WHERE ns=? AND id=?", (ns, str(course))).fetchone():
                raise ValueError("所选课程不存在")
            db.execute("INSERT INTO tasks(id,ns,course,origin,source_id,title,planned_due,notes,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (key, ns, str(course), "manual", key, title, due, notes, timestamp, timestamp))
        return key

    def edit(self, ns, key, status, notes, planned_due=None, archived=False, title=None):
        if status not in STATUSES:
            raise ValueError("任务状态不合法")
        due = iso(utc_time(planned_due)) if planned_due else None
        timestamp = iso(self.clock())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE ns=? AND id=?", (ns, key)).fetchone()
            if not row:
                raise ValueError("任务不存在")
            data = dict(row)
            if data["canvas_due"] and due != data["planned_due"]:
                raise ValueError("Canvas 已提供截止时间，此处不能用计划日期替代")
            if title is not None and data["origin"] != "manual":
                raise ValueError("Canvas 任务标题由同步更新")
            new_title = str(title).strip() if title is not None else data["title"]
            if not new_title:
                raise ValueError("任务标题不能为空")
            current = data["canvas_due"] or due
            changed = effective_due(data) != current
            version = data["due_version"] + int(changed)
            reactivated = (data["status"] == "已完成" and status != "已完成") or (data["archived"] and not archived)
            db.execute("UPDATE tasks SET status=?,notes=?,planned_due=?,archived=?,title=?,updated=?,due_version=? WHERE id=?",
                       (status, notes, due, int(bool(archived)), new_title, timestamp, version, key))
            if changed:
                _date_changed(db, key, effective_due(data), current, version, timestamp)
            if status == "已完成" or archived:
                db.execute("UPDATE reminder_events SET status='cancelled' WHERE task=? AND status='pending'", (key,))
            if reactivated:
                db.execute("UPDATE tasks SET schedule_since=? WHERE id=?", (timestamp, key))

    def history(self, ns, key):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT h.* FROM due_history h JOIN tasks t ON h.task=t.id WHERE t.ns=? AND t.id=? ORDER BY h.version DESC", (ns, key))]

    def attachments(self, task):
        from .index import existing_path
        result = []
        ids = json.loads(task.get("attachments") or "[]")
        for rid in ids:
            row = self.store.resource(task["ns"], task["course"], "file", rid) or {}
            candidates = row.get("locations", []) + [row.get("path", "")]
            paths = []
            for value in candidates:
                if not value:
                    continue
                if existing_path(self.store, task["ns"], value, task["course"]):
                    paths.append(value)
            result.append({"id": str(rid), "title": row.get("title", f"附件 {rid}"), "path": paths[0] if paths else "", "available": bool(paths)})
        return result
