"""可预览、可重试的本地清理；绝不递归永久删除。"""
import json
import os
import stat
import uuid
from contextlib import ExitStack
from pathlib import Path

from .index import existing_path, write_index
from .lock import sync_lock
from .paths import sha_file, under
from .schema import artifact_fields
from .store import now


def system_trash(path):
    from send2trash import send2trash
    send2trash(str(path))


def guard(item):
    root, path = Path(item["root"]), Path(item["path"])
    if root.resolve() != root or root.is_symlink():
        raise ValueError("资料根目录已改变或含符号链接")
    candidate = under(root, str(path.relative_to(root)))
    directory = under(root, str(Path(item["course_dir"]).relative_to(root)))
    if not candidate.is_relative_to(directory):
        raise ValueError("路径不属于该课程目录")
    fields = artifact_fields(item["ns"], item["logical"], candidate, root)
    if not fields or fields["course"] != item["course"] or fields["course_dir"] != str(directory):
        raise ValueError("文件归属记录无法验证")
    return candidate


def fingerprint(path, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("符号链接不会清理")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("目录类型已改变")
        return [info.st_dev, info.st_ino]
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("只处理常规文件")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, sha_file(path)]


class Cleanup:
    def __init__(self, store, trash=None):
        self.store, self.trash = store, trash or system_trash

    def preview(self, ns, courses, root, include_groups=(), archive=False):
        selected = {str(c) for c in courses}
        allowed_courses = {c["id"] for c in self.store.courses(ns)}
        if not selected or not selected <= allowed_courses:
            raise ValueError("请先选择当前账户中的课程")
        artifacts = [a for a in self.store.artifacts(ns) if a["course"] in selected and a["state"] == "active"]
        items, protected, group_dirs = [], set(), {}
        known = {a["path"] for a in artifacts}
        for artifact in artifacts:
            item = dict(artifact, action="trash", reason="", size=0, modified=False)
            directory = item["category"] == "directory"
            try:
                path = guard(item)
                item["root_identity"] = fingerprint(Path(item["root"]), True)
                item["identity"] = fingerprint(path, directory)
                if not directory:
                    item["size"] = item["identity"][2]
                    item["modified"] = item["identity"][-1] != artifact["sha"]
                    if item["modified"]:
                        protected.add(item["group_key"])
                    group_dir = path.parent
                    if ":assignment:" in item["group_key"] or ":module:" in item["group_key"] or ":page:" in item["group_key"]:
                        rel = path.relative_to(item["course_dir"])
                        depth = 3 if rel.parts[0] == "课程资料" and len(rel.parts) > 2 and rel.parts[1] == "Modules" else 2
                        group_dir = Path(item["course_dir"]).joinpath(*rel.parts[:depth])
                    group_dirs.setdefault(item["group_key"], set()).add(group_dir)
            except FileNotFoundError:
                item.update(action="missing", reason="文件已经不存在，执行时只修正记录")
            except (OSError, ValueError) as error:
                item.update(action="blocked", reason=str(error))
            items.append(item)
        # 仅检查已验证课程目录；不跟随任何链接，也不把未知文件登记为工具文件。
        directories = {(a["root"], a["course_dir"], a["course"]) for a in artifacts}
        for registered_root, course_dir, cid in sorted(directories):
            try:
                if Path(registered_root).resolve() != Path(registered_root):
                    continue
                base = under(Path(registered_root), str(Path(course_dir).relative_to(registered_root)))
                if not base.is_dir():
                    continue
                def onerror(error):
                    items.append({"ns": ns, "course": cid, "path": str(error.filename), "root": registered_root,
                                  "course_dir": course_dir, "category": "unknown", "action": "blocked", "reason": "无法读取目录，未纳入清理", "size": 0})
                for parent, folders, files in os.walk(base, followlinks=False, onerror=onerror):
                    for name in list(folders):
                        p = Path(parent) / name
                        if p.is_symlink():
                            folders.remove(name)
                            files.append(name)
                        elif str(p) not in known:
                            items.append({"ns": ns, "course": cid, "path": str(p), "root": registered_root, "course_dir": course_dir,
                                          "category": "directory", "action": "keep", "reason": "旧版或用户目录未登记，默认保留", "size": 0})
                    for name in files:
                        p = Path(parent) / name
                        if str(p) in known:
                            continue
                        symbolic = p.is_symlink()
                        items.append({"ns": ns, "course": cid, "path": str(p), "root": registered_root, "course_dir": course_dir,
                                      "category": "unknown", "action": "blocked" if symbolic else "keep",
                                      "reason": "符号链接不会清理" if symbolic else "用户新增或旧版归属无法确认，默认保留", "size": p.lstat().st_size if p.exists() or symbolic else 0})
                        for group, roots in group_dirs.items():
                            if any(p.is_relative_to(d) for d in roots):
                                protected.add(group)
            except (OSError, ValueError):
                continue
        include_groups = set(include_groups)
        if not include_groups <= protected:
            raise ValueError("所选清理文件组已变化，请重新预览")
        for item in items:
            if item.get("group_key") in protected and item["action"] == "trash" and item["category"] != "directory":
                if item["group_key"] not in include_groups:
                    item.update(action="keep", reason="本地修改或用户新增文件所在的配套组，默认整组保留")
                else:
                    item["reason"] = "已明确选择清理此托管文件组；用户自建文件仍保留"
        identifier = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO cleanup_runs VALUES(?,?,?,?,?,?,?,NULL)",
                       (identifier, ns, str(Path(root).expanduser().resolve()), json.dumps(sorted(selected)), int(archive), "preview", now()))
            for item in items:
                db.execute("INSERT OR IGNORE INTO cleanup_items VALUES(?,?,?,?,?)", (identifier, item["path"], json.dumps(item, ensure_ascii=False), "preview", ""))
        return self.report(ns, identifier, protected=sorted(protected))

    def report(self, ns, identifier, protected=None):
        with self.store.connect() as db:
            run = db.execute("SELECT * FROM cleanup_runs WHERE id=? AND ns=?", (identifier, ns)).fetchone()
            if not run:
                raise ValueError("清理预览不存在或不属于当前账户")
            rows = db.execute("SELECT * FROM cleanup_items WHERE run=? ORDER BY path", (identifier,)).fetchall()
        result = dict(run)
        result["items"] = [dict(json.loads(r["payload"]), result=r["status"], result_reason=r["reason"]) for r in rows]
        result["protected_groups"] = protected if protected is not None else sorted({r.get("group_key") for r in result["items"] if r.get("group_key") and r["action"] == "keep" and r["category"] != "directory"})
        ready = [r for r in result["items"] if r["action"] == "trash" and r["category"] != "directory"]
        result["file_count"] = len(ready)
        result["bytes"] = sum(r["size"] for r in ready)
        return result

    def _result(self, run, item, status, reason=""):
        with self.store.connect() as db:
            db.execute("UPDATE cleanup_items SET status=?,reason=? WHERE run=? AND path=?", (status, reason, run, item["path"]))
            if status in {"trashed", "missing"}:
                db.execute("UPDATE artifacts SET state=? WHERE ns=? AND path=?", (status, item["ns"], item["path"]))
                db.execute("DELETE FROM placements WHERE ns=? AND path=?", (item["ns"], item["path"]))

    def execute(self, ns, identifier):
        report = self.report(ns, identifier)
        if report["status"] != "preview":
            raise ValueError("该预览已经执行或中断，请重新预览后再确认")
        items = report["items"]
        roots = sorted({i["root"] for i in items if i.get("logical") and Path(i["root"]).is_dir() and Path(i["root"]).resolve() == Path(i["root"])})
        with ExitStack() as stack:
            for root in roots:
                stack.enter_context(sync_lock(under(Path(root), ".canvas-sync", ns, "sync.lock")))
            with self.store.connect() as db:
                changed = db.execute("UPDATE cleanup_runs SET status='running' WHERE id=? AND status='preview'", (identifier,)).rowcount
                if changed != 1:
                    raise ValueError("该预览已经执行或中断，请重新预览")
            for item in sorted(items, key=lambda i: (i["category"] == "directory", -len(Path(i["path"]).parts))):
                if item["action"] in {"keep", "blocked"}:
                    self._result(identifier, item, "kept" if item["action"] == "keep" else "failed", item["reason"])
                    continue
                try:
                    actual = self.store.artifact(ns, item["path"])
                    if not actual or actual["state"] != "active" or any(actual[k] != item[k] for k in ("course", "logical", "root", "sha")):
                        raise ValueError("托管记录已变化，请重新预览")
                    path = guard(item)
                    if item["action"] == "missing":
                        if path.exists():
                            raise ValueError("文件已恢复，请重新预览")
                        self._result(identifier, item, "missing", "原文件已缺失，已修正记录")
                        continue
                    if fingerprint(Path(item["root"]), True) != item["root_identity"]:
                        raise ValueError("资料根目录身份已变化，请重新预览")
                    directory = item["category"] == "directory"
                    if fingerprint(path, directory) != item["identity"]:
                        raise ValueError("预览后内容或文件身份已变化，请重新预览")
                    if directory and any(path.iterdir()):
                        self._result(identifier, item, "kept", "目录包含保留或失败项目，未移动")
                        continue
                    self._result(identifier, item, "processing", "正在移入系统废纸篓")
                    self.trash(path)
                    if path.exists() or path.is_symlink():
                        raise OSError("废纸篓接口返回后原路径仍存在，未认定成功")
                    self._result(identifier, item, "trashed", "已移入系统废纸篓")
                except (OSError, ValueError, ImportError) as error:
                    self._result(identifier, item, "failed", str(error))
                except Exception:
                    self._result(identifier, item, "failed", "系统废纸篓操作失败；没有改成永久删除")
            selected = json.loads(report["courses"])
            for cid in selected:
                self.reconcile(ns, cid)
                if report["archive"]:
                    self.store.archive_course(ns, cid, True)
            with self.store.connect() as db:
                db.executemany("UPDATE courses SET selected=0 WHERE ns=? AND id=?", [(ns, cid) for cid in selected])
                db.execute("UPDATE cleanup_runs SET status='finished',ended=? WHERE id=?", (now(), identifier))
        return self.report(ns, identifier)

    def reconcile(self, ns, cid):
        for row in self.store.resources(ns, cid):
            candidates = list(dict.fromkeys([row.get("path", "")] + row.get("locations", [])))
            live = [p for p in candidates if existing_path(self.store, ns, p, cid)]
            remote = row.get("remote_path", "")
            row["path"] = live[0] if live else ""
            row["locations"] = live
            if not existing_path(self.store, ns, remote, cid):
                row["remote_path"] = ""
            if not live and (any(candidates) or remote or row.get("status") == "cleared"):
                row.update(status="cleared", reason="本地资料已清理或缺失，任务历史保留；重新选择并同步可恢复")
            self.store.save_resource(ns, cid, row["kind"], row["id"], row)
        course = next(c for c in self.store.courses(ns) if c["id"] == str(cid))
        directories = {(a["root"], a["course_dir"]) for a in self.store.artifacts(ns, cid)}
        for root, directory in directories:
            try:
                if Path(root).resolve() != Path(root):
                    continue
                p = under(Path(root), str(Path(directory).relative_to(root)))
                if p.is_dir() and any(p.iterdir()):
                    write_index(self.store, ns, cid, course, p, root, note="已执行本地清理，保留文件仍可查看；共享缓存未清理。")
            except (ValueError, OSError):
                # 文件处理结果已持久化；索引失败独立记录，允许再次预览/执行重试。
                self.store.source(ns, cid, "清理后索引", "failed", "无法更新此目录的索引，请检查目录权限和路径")

    def recent(self, ns):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM cleanup_runs WHERE ns=? ORDER BY created DESC LIMIT 10", (ns,))]
