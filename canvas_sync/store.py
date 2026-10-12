import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS courses(
              ns TEXT, id TEXT, payload TEXT, selected INTEGER DEFAULT 0,
              term_override TEXT DEFAULT '', fetched TEXT, PRIMARY KEY(ns,id));
            CREATE TABLE IF NOT EXISTS resources(
              ns TEXT, course TEXT, kind TEXT, id TEXT, payload TEXT,
              PRIMARY KEY(ns,course,kind,id));
            CREATE TABLE IF NOT EXISTS blobs(
              ns TEXT, id TEXT, revision TEXT, path TEXT, sha TEXT, size INTEGER,
              PRIMARY KEY(ns,id,revision));
            CREATE TABLE IF NOT EXISTS placements(
              ns TEXT, logical TEXT, path TEXT, sha TEXT, revision TEXT,
              PRIMARY KEY(ns,logical), UNIQUE(ns,path));
            CREATE TABLE IF NOT EXISTS sources(
              ns TEXT, course TEXT, name TEXT, status TEXT, detail TEXT, fetched TEXT,
              PRIMARY KEY(ns,course,name));
            CREATE TABLE IF NOT EXISTS runs(
              id INTEGER PRIMARY KEY, ns TEXT, started TEXT, ended TEXT, payload TEXT);
            ''')
            from .schema import VERSION, migrate
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > VERSION:
                raise ValueError("数据库版本比程序新，请使用匹配的程序版本")
            if version < VERSION:
                if existed:
                    backups = self.path.parent / "backups"
                    backups.mkdir(parents=True, exist_ok=True)
                    backup_path = backups / f"state-before-v{VERSION}-{uuid.uuid4().hex}.sqlite3"
                    with sqlite3.connect(backup_path) as target:
                        db.backup(target)
                    backup_path.chmod(0o600)
                db.execute("BEGIN IMMEDIATE")
                # 另一个进程可能已完成升级，锁内再次核对。
                if db.execute("PRAGMA user_version").fetchone()[0] < VERSION:
                    migrate(db)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def courses(self, ns):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM courses WHERE ns=? ORDER BY id", (ns,)).fetchall()
        result = []
        for row in rows:
            data = json.loads(row["payload"])
            data.update(selected=bool(row["selected"]), archived=bool(row["archived"]), term_override=row["term_override"], fetched=row["fetched"])
            result.append(data)
        return result

    def save_course(self, ns, course):
        with self.connect() as db:
            db.execute("INSERT INTO courses(ns,id,payload,fetched) VALUES(?,?,?,?) "
                       "ON CONFLICT(ns,id) DO UPDATE SET payload=excluded.payload,fetched=excluded.fetched",
                       (ns, str(course["id"]), json.dumps(course, ensure_ascii=False), now()))

    def select(self, ns, ids):
        with self.connect() as db:
            db.execute("UPDATE courses SET selected=0 WHERE ns=?", (ns,))
            db.executemany("UPDATE courses SET selected=1 WHERE ns=? AND id=? AND archived=0", [(ns, str(i)) for i in ids])

    def archive_course(self, ns, cid, archived=True):
        timestamp = now()
        with self.connect() as db:
            db.execute("UPDATE courses SET archived=?,selected=CASE WHEN ? THEN 0 ELSE selected END WHERE ns=? AND id=?", (int(archived), int(archived), ns, str(cid)))
            if archived:
                db.execute("UPDATE reminder_events SET status='cancelled' WHERE status='pending' AND task IN (SELECT id FROM tasks WHERE ns=? AND course=?)", (ns, str(cid)))
            else:
                db.execute("UPDATE tasks SET schedule_since=? WHERE ns=? AND course=?", (timestamp, ns, str(cid)))

    def artifact(self, ns, path):
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE ns=? AND path=?", (ns, str(path))).fetchone()
        return dict(row) if row else None

    def artifacts(self, ns, cid=None):
        with self.connect() as db:
            sql, args = "SELECT * FROM artifacts WHERE ns=?", [ns]
            if cid is not None:
                sql += " AND course=?"
                args.append(str(cid))
            return [dict(r) for r in db.execute(sql, args)]

    def record_artifact(self, ns, path, logical, sha, fields, category=None):
        from .schema import record_artifact
        with self.connect() as db:
            record_artifact(db, ns, path, logical, sha, fields, category)

    def term(self, ns, cid, value):
        with self.connect() as db:
            db.execute("UPDATE courses SET term_override=? WHERE ns=? AND id=?", (value, ns, str(cid)))

    def save_resource(self, ns, cid, kind, rid, payload):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO resources VALUES(?,?,?,?,?)",
                       (ns, str(cid), kind, str(rid), json.dumps(payload, ensure_ascii=False)))

    def resource(self, ns, cid, kind, rid):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM resources WHERE ns=? AND course=? AND kind=? AND id=?",
                             (ns, str(cid), kind, str(rid))).fetchone()
        return json.loads(row[0]) if row else None

    def resources(self, ns, cid=None):
        with self.connect() as db:
            sql = "SELECT course,kind,id,payload FROM resources WHERE ns=?"
            args = [ns]
            if cid is not None:
                sql += " AND course=?"
                args.append(str(cid))
            rows = db.execute(sql, args).fetchall()
        return [dict(json.loads(r["payload"]), course_id=r["course"], kind=r["kind"], id=r["id"]) for r in rows]

    def blob(self, ns, rid, rev):
        with self.connect() as db:
            row = db.execute("SELECT * FROM blobs WHERE ns=? AND id=? AND revision=?", (ns, str(rid), rev)).fetchone()
        return dict(row) if row else None

    def save_blob(self, ns, rid, rev, path, sha, size):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO blobs VALUES(?,?,?,?,?,?)", (ns, str(rid), rev, str(path), sha, size))

    def placement(self, ns, logical):
        with self.connect() as db:
            row = db.execute("SELECT * FROM placements WHERE ns=? AND logical=?", (ns, logical)).fetchone()
        return dict(row) if row else None

    def owner(self, ns, path):
        with self.connect() as db:
            row = db.execute("SELECT logical FROM placements WHERE ns=? AND lower(path)=lower(?)", (ns, str(path))).fetchone()
        return row[0] if row else None

    def save_placement(self, ns, logical, path, sha, revision):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO placements VALUES(?,?,?,?,?)", (ns, logical, str(path), sha, revision))

    def source(self, ns, cid, name, status, detail):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?)", (ns, str(cid), name, status, detail, now()))

    def sources(self, ns, cid=None):
        with self.connect() as db:
            sql, args = "SELECT * FROM sources WHERE ns=?", [ns]
            if cid is not None:
                sql += " AND course=?"
                args.append(str(cid))
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def start_run(self, ns):
        with self.connect() as db:
            return db.execute("INSERT INTO runs(ns,started,payload) VALUES(?,?,?)", (ns, now(), '{}')).lastrowid

    def end_run(self, run, result):
        with self.connect() as db:
            db.execute("UPDATE runs SET ended=?,payload=? WHERE id=?", (now(), json.dumps(result, ensure_ascii=False), run))

    def last_run(self, ns):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE ns=? ORDER BY id DESC LIMIT 1", (ns,)).fetchone()
        return dict(row, result=json.loads(row["payload"])) if row else None
