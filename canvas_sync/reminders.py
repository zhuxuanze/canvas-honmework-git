"""离线 DDL 计算与单例后台进程；不调用 Canvas，不读取凭据。"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, home_dir, profile_id
from .content import date_text
from .errors import SyncError
from .lock import sync_lock
from .paths import private_dir
from .store import Store
from .tasks import effective_due, iso, utc_time

DEFAULT_OFFSETS = [1440, 120]
DEMO_NS = profile_id("https://demo.canvas.invalid", "9001")


class MacNotifier:
    def send(self, title, message):
        if sys.platform != "darwin":
            return {"channel": "in_app", "error": "此系统暂未支持系统通知，已记录在提醒中心"}
        script = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run'
        try:
            result = subprocess.run(["/usr/bin/osascript", "-e", script, title, message], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return {"channel": "macos", "error": "系统通知命令不可用或超时，请检查通知权限"}
        if result.returncode:
            return {"channel": "macos", "error": "系统通知命令失败：" + (result.stderr.strip()[:500] or f"退出码 {result.returncode}")}
        return {"channel": "macos", "error": ""}  # 表示调用被接受，不表示横幅确实显示。


def settings(store, ns):
    with store.connect() as db:
        row = db.execute("SELECT * FROM reminder_settings WHERE ns=?", (ns,)).fetchone()
    return {"enabled": bool(row["enabled"]), "offsets": json.loads(row["offsets"]), "changed": row["changed"]} if row else {"enabled": True, "offsets": DEFAULT_OFFSETS[:], "changed": None}


def set_settings(store, ns, enabled, offsets, clock=None):
    if len(offsets) > 2 or any(isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > 525600 for n in offsets):
        raise ValueError("最多设置两个节点，提前时间需为 0 到 365 天之间的整分钟")
    offsets = sorted(set(offsets), reverse=True)
    old = settings(store, ns)
    if old["enabled"] == bool(enabled) and old["offsets"] == offsets:
        return
    timestamp = iso((clock or (lambda: datetime.now(timezone.utc)))())
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO reminder_settings VALUES(?,?,?,?)", (ns, int(bool(enabled)), json.dumps(offsets), timestamp))
        db.execute("UPDATE reminder_events SET status='cancelled' WHERE status='pending' AND task IN (SELECT id FROM tasks WHERE ns=?)", (ns,))


def remaining(due, current):
    minutes = math.ceil((utc_time(due) - current).total_seconds() / 60)
    if minutes <= 0:
        return f"已逾期 {abs(minutes)} 分钟"
    if minutes >= 60:
        return f"剩余 {minutes // 60} 小时 {minutes % 60} 分钟"
    return f"剩余 {minutes} 分钟"


class ReminderWorker:
    def __init__(self, store, notifier=None, clock=None, namespace_guard=None):
        self.store = store
        self.notifier = notifier or MacNotifier()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.namespace_guard = namespace_guard or (lambda ns: ns != DEMO_NS)

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE reminder_events SET status='unknown',error='上次发送中断，系统接收状态未知；不自动重发' WHERE status='sending'")

    def _eligible(self, event):
        with self.store.connect() as db:
            row = db.execute("SELECT t.*,COALESCE(c.archived,0) AS course_archived FROM tasks t LEFT JOIN courses c ON t.ns=c.ns AND t.course=c.id WHERE t.id=?", (event["task"],)).fetchone()
        preferences = settings(self.store, row["ns"]) if row else {"enabled": False, "offsets": []}
        baseline = max(utc_time(row["created"]), utc_time(row["schedule_since"]) or utc_time(row["created"]),
                       utc_time(preferences["changed"]) or utc_time(row["created"])) if row else None
        return bool(row and row["status"] != "已完成" and not row["archived"] and not row["course_archived"]
                    and not row["date_error"] and utc_time(event["trigger_at"]) >= baseline
                    and row["due_version"] == event["version"] and preferences["enabled"] and event["offset"] in preferences["offsets"]
                    and self.namespace_guard(row["ns"]))

    def tick(self, ns):
        current = self.clock().astimezone(timezone.utc)
        timestamp = iso(current)
        preferences = settings(self.store, ns)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            service = db.execute("SELECT * FROM service_state WHERE id=1").fetchone()
            last = utc_time(service["last_check"]) if service and service["active_ns"] == ns and service["last_check"] else None
            catchup = last is None or (current - last).total_seconds() > 90
            tasks = db.execute("SELECT t.*,COALESCE(c.archived,0) AS course_archived,c.payload AS course_payload FROM tasks t LEFT JOIN courses c ON t.ns=c.ns AND t.course=c.id WHERE t.ns=?", (ns,)).fetchall()
            for row in tasks:
                task = dict(row)
                due = effective_due(task)
                eligible = preferences["enabled"] and self.namespace_guard(ns) and due and not task["date_error"] and task["status"] != "已完成" and not task["archived"] and not task["course_archived"]
                if not eligible:
                    db.execute("UPDATE reminder_events SET status='cancelled' WHERE task=? AND status='pending'", (task["id"],))
                    continue
                baseline = max(utc_time(task["created"]), utc_time(task["schedule_since"]) or utc_time(task["created"]),
                               utc_time(preferences["changed"]) or utc_time(task["created"]))
                course_name = json.loads(task["course_payload"])["name"] if task["course_payload"] else "个人任务"
                snapshot = json.dumps({"course": course_name, "title": task["title"], "due": due,
                                       "date_source": "Canvas DDL" if task["canvas_due"] else "本地计划"}, ensure_ascii=False)
                for offset in preferences["offsets"]:
                    trigger = utc_time(due) - timedelta(minutes=offset)
                    state = "pending" if trigger >= baseline else "expired"
                    db.execute("INSERT OR IGNORE INTO reminder_events(task,version,offset,trigger_at,status,snapshot) VALUES(?,?,?,?,?,?)",
                               (task["id"], task["due_version"], offset, iso(trigger), state, snapshot))
                    db.execute("UPDATE reminder_events SET snapshot=? WHERE task=? AND version=? AND offset=? AND status='pending'",
                               (snapshot, task["id"], task["due_version"], offset))
                    # 重新打开/启用只恢复未来节点，已发送与未知结果永不自动重发。
                    if trigger > current and trigger >= baseline:
                        db.execute("UPDATE reminder_events SET status='pending',snapshot=? WHERE task=? AND version=? AND offset=? AND status='cancelled'",
                                   (snapshot, task["id"], task["due_version"], offset))
            pending = db.execute("SELECT e.* FROM reminder_events e JOIN tasks t ON e.task=t.id WHERE t.ns=? AND e.status='pending' AND e.trigger_at<=? ORDER BY e.trigger_at", (ns, timestamp)).fetchall()
            events = [dict(e) for e in pending]
            for event in events:
                db.execute("UPDATE reminder_events SET status='sending',claimed_at=? WHERE task=? AND version=? AND offset=? AND status='pending'",
                           (timestamp, event["task"], event["version"], event["offset"]))
            db.execute("INSERT INTO service_state(id,last_check,heartbeat,active_ns,status,error) VALUES(1,?,?,?,'running','') "
                       "ON CONFLICT(id) DO UPDATE SET last_check=excluded.last_check,heartbeat=excluded.heartbeat,active_ns=excluded.active_ns",
                       (timestamp, timestamp, ns))
        batches = [events] if catchup and events else []
        if not catchup:
            by_task = {}
            for event in events:
                by_task.setdefault(event["task"], []).append(event)
            batches = list(by_task.values())
        last_error = None
        for batch in batches:
            eligible = [e for e in batch if self._eligible(e)]
            cancelled = [e for e in batch if e not in eligible]
            for e in cancelled:
                self._finish(e, "cancelled", "", "", "")
            if not eligible:
                continue
            identifier = uuid.uuid4().hex
            unique = {e["task"]: json.loads(e["snapshot"]) for e in eligible}
            title = f"DDL 提醒：恢复后合并 {len(unique)} 个任务" if catchup else "Canvas 作业 DDL 提醒"
            details = [f'{s["course"]} · {s["title"]}\n{s["date_source"]}：{date_text(s["due"])}，{remaining(s["due"], current)}' for s in unique.values()]
            message = "\n\n".join(details[:3])
            if len(details) > 3:
                message += f"\n还有 {len(details)-3} 项，请查看本地提醒中心。"
            try:
                result = self.notifier.send(title, message)
                channel, error = result.get("channel", "in_app"), result.get("error", "")
                state = "failed" if error and channel != "in_app" else "sent"
            except Exception:
                channel, error, state = "system", "系统通知适配器失败；提醒中心记录已保留，不自动重复发送", "failed"
            last_error = error
            for event in eligible:
                self._finish(event, state, channel, error, identifier)
        if last_error is not None:
            with self.store.connect() as db:
                db.execute("UPDATE service_state SET error=? WHERE id=1", (last_error,))
        return len(events)

    def _finish(self, event, state, channel, error, batch):
        with self.store.connect() as db:
            db.execute("UPDATE reminder_events SET status=?,channel=?,error=?,batch=? WHERE task=? AND version=? AND offset=?",
                       (state, channel, error, batch, event["task"], event["version"], event["offset"]))


def center(store, ns, all_events=False):
    with store.connect() as db:
        sql = "SELECT e.*,t.title AS current_title FROM reminder_events e JOIN tasks t ON e.task=t.id WHERE t.ns=?"
        if not all_events:
            sql += " AND e.status IN ('sending','sent','failed','unknown')"
        return [dict(r) for r in db.execute(sql + " ORDER BY COALESCE(e.claimed_at,e.trigger_at) DESC", (ns,))]


def mark_seen(store, ns):
    with store.connect() as db:
        db.execute("UPDATE reminder_events SET seen=1 WHERE task IN (SELECT id FROM tasks WHERE ns=?)", (ns,))


def service_status(home):
    store = Store(Path(home) / "state.sqlite3")
    with store.connect() as db:
        row = db.execute("SELECT * FROM service_state WHERE id=1").fetchone()
    state = dict(row) if row else {"status": "stopped", "heartbeat": None, "last_check": None, "error": ""}
    # 不能仅凭 PID 判断是否仍属于本服务，也不发送任何信号到 PID。
    alive = False
    try:
        with sync_lock(Path(home) / "reminders.lock"):
            pass
    except SyncError:
        alive = True
    state["running"] = alive
    state["healthy"] = alive and bool(state.get("heartbeat")) and (datetime.now(timezone.utc) - utc_time(state["heartbeat"])).total_seconds() <= 90
    return state


def start_service(home):
    home = private_dir(Path(home).expanduser().resolve())
    if service_status(home)["running"]:
        return "提醒服务已经运行"
    kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    subprocess.Popen([sys.executable, "-m", "canvas_sync.reminders", "--home", str(home), "run"],
                     cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    return "已请求启动后台提醒服务，请刷新状态确认"


def stop_service(home):
    store = Store(Path(home) / "state.sqlite3")
    with store.connect() as db:
        db.execute("UPDATE service_state SET stop_requested=1 WHERE id=1")
    return "已请求停止服务（通常 1 秒内完成），请刷新状态确认"


def run_service(home):
    home = private_dir(Path(home).resolve())
    try:
        with sync_lock(home / "reminders.lock"):
            store = Store(home / "state.sqlite3")
            token = uuid.uuid4().hex
            with store.connect() as db:
                db.execute("INSERT INTO service_state(id,pid,token,heartbeat,status,error,stop_requested) VALUES(1,?,?,?,'running','',0) "
                           "ON CONFLICT(id) DO UPDATE SET pid=excluded.pid,token=excluded.token,heartbeat=excluded.heartbeat,status='running',error='',stop_requested=0",
                           (os.getpid(), token, iso(datetime.now(timezone.utc))))
            worker = ReminderWorker(store, namespace_guard=lambda ns: ns != DEMO_NS and Config.load(home).profile == ns)
            worker.recover()
            last_tick = 0.
            try:
                while True:
                    with store.connect() as db:
                        state = db.execute("SELECT stop_requested FROM service_state WHERE id=1").fetchone()
                    if state and state[0]:
                        break
                    if time.monotonic() - last_tick >= 30:
                        try:
                            config = Config.load(home)
                            if config.profile and config.profile != DEMO_NS:
                                worker.tick(config.profile)
                            with store.connect() as db:
                                db.execute("UPDATE service_state SET heartbeat=?,status=? WHERE id=1", (iso(datetime.now(timezone.utc)), "running" if config.profile else "waiting"))
                        except Exception:
                            with store.connect() as db:
                                db.execute("UPDATE service_state SET heartbeat=?,error=? WHERE id=1", (iso(datetime.now(timezone.utc)), "检查任务失败，请检查数据库和配置；服务下次继续尝试"))
                        last_tick = time.monotonic()
                    time.sleep(1)
            finally:
                with store.connect() as db:
                    db.execute("UPDATE service_state SET status='stopped',heartbeat=?,stop_requested=0 WHERE id=1 AND token=?", (iso(datetime.now(timezone.utc)), token))
    except SyncError:
        return  # 另一个进程已持有锁，不能重复启动。


def main():
    parser = argparse.ArgumentParser(description="本地 DDL 提醒服务（不联网）")
    parser.add_argument("--home", type=Path)
    parser.add_argument("command", choices=["start", "stop", "status", "run"])
    args = parser.parse_args()
    home = args.home or home_dir()
    if args.command == "run":
        run_service(home)
    elif args.command == "start":
        print(start_service(home))
    elif args.command == "stop":
        print(stop_service(home))
    else:
        print(json.dumps(service_status(home), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
