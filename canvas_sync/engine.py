import hashlib
import html
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from .content import date_text, offline_document, public_url, refs, revision
from .errors import AuthExpired, InvalidResponse, PermissionDenied, SyncError
from .materialize import Materializer
from .lock import sync_lock
from .paths import safe_name, sha_file, under
from .store import now
from .tasks import Tasks

SOURCE_NAMES = ("Files", "Folders", "Assignments", "Pages", "Modules")


class SyncEngine:
    def __init__(self, canvas, store, ns, root, concurrency=3, tz="Asia/Shanghai", progress=None):
        self.canvas, self.store, self.ns = canvas, store, ns
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.writer = Materializer(store, ns, self.root)
        self.concurrency = max(1, min(int(concurrency), 6))
        self.tz, self.progress = tz, progress or (lambda event: None)
        self.stats = {k: 0 for k in ("added", "updated", "skipped", "failed", "denied", "local_modified", "not_downloaded", "network_downloads")}
        self.expired = False
        self.current_course = ""

    def event(self, message, done=0, total=0):
        self.progress({"course": self.current_course, "message": message, "done": done, "total": total})

    def _source(self, cid, name, status, detail):
        self.store.source(self.ns, cid, name, status, detail)

    def _list(self, cid, name, iterator):
        result = []
        if self.expired:
            self._source(cid, name, "auth_expired", "认证失效，本次未获取")
            return result
        self.event(f"读取 {name}")
        self._source(cid, name, "fetching", "正在读取；若同步中断需重试")
        try:
            for item in iterator:
                result.append(item)
            self._source(cid, name, "success", f"已完整读取 {len(result)} 项")
        except SyncError as exc:
            status = "partial" if result else "failed"
            if isinstance(exc, PermissionDenied):
                status = "partial" if result else "denied"
            if isinstance(exc, AuthExpired):
                self.expired = True
                status = "auth_expired"
            self._source(cid, name, status, f"已读取 {len(result)} 项；{exc}")
        return result

    def _failure(self, cid, kind, rid, title, error, source=""):
        denied = isinstance(error, PermissionDenied)
        if isinstance(error, AuthExpired):
            self.expired = True
        status = "denied" if denied else "failed"
        self.stats[status] += 1
        previous = self.store.resource(self.ns, cid, kind, rid) or {}
        self.store.save_resource(self.ns, cid, kind, rid, dict(previous, title=title, status=status,
            reason=str(error), source=public_url(source) if source else previous.get("source", ""), fetched=now()))

    def sync(self, courses):
        with sync_lock(under(self.root, ".canvas-sync", self.ns, "sync.lock")):
            return self._sync(courses)

    def _sync(self, courses):
        run = self.store.start_run(self.ns)
        try:
            for course in courses:
                stored_course = next((c for c in self.store.courses(self.ns) if c["id"] == str(course["id"])), None)
                if stored_course and stored_course.get("archived"):
                    continue
                self.current_course = course["name"]
                if self.expired:
                    break
                try:
                    self.sync_course(course)
                except (OSError, ValueError):
                    self._source(course["id"], "本地整理", "failed", "本地整理失败：请检查目录权限、空间及路径长度；其他课程继续处理")
            sources = self.store.sources(self.ns)
            ids = {str(c["id"]) for c in courses}
            problems = [s for s in sources if s["course"] in ids and s["status"] != "success"]
            result = dict(self.stats, auth_expired=self.expired, incomplete=bool(problems or self.stats["failed"] or self.stats["denied"]),
                          source_problems=len(problems), courses=list(ids))
            self.store.end_run(run, result)
            return result
        except Exception:
            self.store.end_run(run, dict(self.stats, incomplete=True, interrupted=True))
            raise

    def sync_course(self, course):
        cid = str(course["id"])
        with self.store.connect() as db:
            db.execute("DELETE FROM sources WHERE ns=? AND course=?", (self.ns, cid))
        # 实例/用户前缀避免同一保存根下不同账户互相覆盖。
        course_dir = Path(f"Canvas-{self.ns[:10]}") / safe_name(course.get("term_override") or course["term"], 64) / safe_name(f'{course["name"]}__课程{cid}', 100)
        files = {str(f["id"]): f for f in self._list(cid, "Files", self.canvas.files(cid))}
        folders = {str(f["id"]): f for f in self._list(cid, "Folders", self.canvas.folders(cid))}
        assignments = {str(a["id"]): a for a in self._list(cid, "Assignments", self.canvas.assignments(cid))}
        pages = {str(p.get("page_id", p.get("url"))): p for p in self._list(cid, "Pages", self.canvas.pages(cid))}
        modules = self._list(cid, "Modules", self.canvas.modules(cid))
        groups = []
        modules_partial = False

        def add_file(rid, label="Canvas 附件"):
            rid = str(rid)
            if rid in files or self.expired:
                return
            try:
                files[rid] = self.canvas.file(rid)
            except SyncError as exc:
                self._failure(cid, "file", rid, label, exc, f"{self.canvas.base}/courses/{cid}/files/{rid}")

        for module in modules:
            mid = str(module["id"])
            name = f'Modules · {module.get("name", mid)} [{mid}]'
            if module.get("state") == "locked":
                self._source(cid, name, "denied", "模块尚未解锁；未读取模块项目")
                modules_partial = True
                continue
            items = self._list(cid, name, self.canvas.items(cid, module))
            status = next(s for s in self.store.sources(self.ns, cid) if s["name"] == name)["status"]
            modules_partial |= status != "success"
            group = {"key": f"module:{mid}", "dir": course_dir / "课程资料/Modules" / safe_name(f'{module.get("name", mid)}__模块{mid}'), "files": [], "links": {}}
            for item in items:
                typ, rid = item.get("type"), item.get("content_id")
                details = item.get("content_details") or {}
                source = public_url(item.get("html_url") or item.get("external_url") or "", self.canvas.base)
                if details.get("locked_for_user"):
                    self._failure(cid, "module_item", f'{mid}:{item["id"]}', item.get("title", "锁定项目"), PermissionDenied(details.get("lock_explanation") or "模块项目尚未解锁"), source)
                    modules_partial = True
                    continue
                try:
                    if typ == "File" and rid:
                        add_file(rid, item.get("title", "附件"))
                        group["files"].append(str(rid))
                    elif typ == "Assignment" and rid:
                        if str(rid) not in assignments and not self.expired:
                            assignments[str(rid)] = self.canvas.assignment(cid, rid)
                        if source:
                            group["links"][source] = item.get("title", "作业") + "（已另存至作业目录）"
                    elif typ == "Page" and item.get("page_url"):
                        slug = item["page_url"]
                        if not any(p.get("url") == slug for p in pages.values()) and not self.expired:
                            p = self.canvas.page(cid, slug)
                            pages[str(p.get("page_id", slug))] = p
                        if source:
                            group["links"][source] = item.get("title", "页面") + "（已另存至课程页面目录）"
                    elif typ != "SubHeader":
                        group["links"][source or f'{self.canvas.base}/courses/{cid}/modules'] = item.get("title", typ or "未适配资源") + " — 未下载"
                        self.stats["not_downloaded"] += 1
                except SyncError as exc:
                    self._failure(cid, "module_item", f'{mid}:{item["id"]}', item.get("title", "模块项目"), exc, source)
                    modules_partial = True
                else:
                    previous_error = self.store.resource(self.ns, cid, "module_item", f'{mid}:{item["id"]}')
                    if previous_error:
                        self.store.save_resource(self.ns, cid, "module_item", f'{mid}:{item["id"]}',
                            dict(previous_error, status="skipped", reason="该模块项目已成功读取，关联内容见资源索引", fetched=now()))
            groups.append(group)
        if modules_partial:
            self._source(cid, "Modules", "partial", "模块列表已读取，但部分模块项目无权限或读取失败；详见模块明细")

        for kind, docs in (("assignment", assignments), ("page", pages)):
            for rid, document in list(docs.items()):
                title = document.get("name") or document.get("title") or "未命名"
                slug = document.get("url", rid)
                source = f'{self.canvas.base}/courses/{cid}/' + (f'assignments/{rid}' if kind == "assignment" else f'pages/{slug}')
                if document.get("locked_for_user"):
                    self._failure(cid, kind, rid, title, PermissionDenied(document.get("lock_explanation") or "内容尚未解锁"), source)
                    continue
                body_key = "description" if kind == "assignment" else "body"
                if body_key not in document and self.expired:
                    self._failure(cid, kind, rid, title, AuthExpired("认证失效，正文未读取；既有离线文档保留"), source)
                    continue
                if body_key not in document and not self.expired:
                    try:
                        document = self.canvas.assignment(cid, rid) if kind == "assignment" else self.canvas.page(cid, slug)
                    except SyncError as exc:
                        self._failure(cid, kind, rid, title, exc, source)
                        continue
                if document.get("locked_for_user"):
                    self._failure(cid, kind, rid, title, PermissionDenied("详情接口显示当前账户无权读取"), source)
                    continue
                if body_key not in document and not document.get("block_editor_attributes"):
                    self._failure(cid, kind, rid, title, InvalidResponse("详情接口没有提供正文，未用空内容替换本地文档"), source)
                    continue
                body = document.get(body_key) or ""
                note = ""
                if kind == "page" and not body and document.get("block_editor_attributes"):
                    note = "Canvas Block Editor 页面暂不支持完整离线渲染，请查看原页面。"
                    self._source(cid, "Pages", "partial", note)
                file_refs, links = refs(body, self.canvas.base)
                if kind == "assignment":
                    Tasks(self.store).canvas(self.ns, cid, {"id": rid, "name": title, "description": body,
                        "html_url": source, "due_at": document.get("due_at"), "attachments": list(file_refs)})
                for fid, label in file_refs.items():
                    add_file(fid, label)
                self.stats["not_downloaded"] += len(links)
                groups.append({"key": f"{kind}:{rid}", "dir": course_dir / ("作业" if kind == "assignment" else "课程页面") / safe_name(f'{title}__{rid}'),
                               "files": list(file_refs), "links": links, "document": document, "kind": kind,
                               "id": rid, "title": title, "body": body, "source": source, "note": note})

        def folder_parts(file):
            folder = folders.get(str(file.get("folder_id")))
            if not folder:
                return []
            parts, visited = [], set()
            while folder and str(folder["id"]) not in visited:
                visited.add(str(folder["id"]))
                if not folder.get("parent_folder_id"):
                    break
                parts.insert(0, safe_name(folder.get("name", "未命名目录"), 64))
                folder = folders.get(str(folder.get("parent_folder_id")))
            return parts

        blobs = {}
        allowed = {}
        for rid, file in files.items():
            if file.get("locked_for_user") or file.get("hidden_for_user"):
                self._failure(cid, "file", rid, file.get("filename", "附件"), PermissionDenied(file.get("lock_explanation") or "当前账户不可访问或文件尚未解锁"), f'{self.canvas.base}/courses/{cid}/files/{rid}')
            else:
                allowed[rid] = file
        self.event("下载附件（默认最多 3 个并发）", 0, len(allowed))
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._blob, rid, file): (rid, file) for rid, file in allowed.items()}
            for done, future in enumerate(as_completed(futures), 1):
                rid, file = futures[future]
                try:
                    blobs[rid] = future.result()
                    if blobs[rid]["downloaded"]:
                        self.stats["network_downloads"] += 1
                except (SyncError, OSError, ValueError) as exc:
                    if not isinstance(exc, SyncError):
                        exc = SyncError("本地保存失败：请检查目录权限、空间及路径长度")
                    self._failure(cid, "file", rid, file.get("filename", "附件"), exc, f'{self.canvas.base}/courses/{cid}/files/{rid}')
                self.event(f'附件 {done}/{len(allowed)}：{file.get("filename", "文件")}', done, len(allowed))

        for rid, blob in blobs.items():
            file = files[rid]
            relative = course_dir / ("课程资料/Files" if str(file.get("folder_id")) in folders else "其他资料") / Path(*folder_parts(file)) / safe_name(file.get("filename") or file.get("display_name") or f"file-{rid}")
            try:
                path, status, remote = self.writer.write(f"{cid}:file:{rid}", relative, Path(blob["path"]), blob["sha"], blob["revision"])
            except (OSError, ValueError):
                self._failure(cid, "file", rid, file.get("filename", "附件"), SyncError("本地整理失败：请检查目录权限、空间及路径长度"), f'{self.canvas.base}/courses/{cid}/files/{rid}')
                continue
            self.stats[status] += 1
            old = self.store.resource(self.ns, cid, "file", rid) or {}
            self.store.save_resource(self.ns, cid, "file", rid, {
                "title": file.get("display_name") or file.get("filename", "附件"), "filename": file.get("filename", ""),
                "updated": file.get("updated_at"), "size": blob["size"], "sha": blob["sha"], "path": str(path),
                "remote_path": str(remote) if remote else "", "source": f'{self.canvas.base}/courses/{cid}/files/{rid}',
                "status": status, "reason": "已保留本地修改，远程版本另存" if remote else "", "fetched": now(),
                "synced": now() if status != "skipped" else old.get("synced", now()), "locations": [str(path)]})

        for group in sorted(groups, key=lambda g: 0 if g.get("kind") else 1):
            try:
                self._group(cid, group, files, blobs, folder_parts)
            except (OSError, ValueError):
                if group.get("kind"):
                    self._failure(cid, group["kind"], group["id"], group["title"], SyncError("此内容本地保存失败，请检查路径、权限和磁盘空间"), group["source"])
                else:
                    self._source(cid, "Modules", "partial", "部分模块本地保存失败，请检查路径、权限和磁盘空间")
        self._index(cid, course, course_dir)
        self.event("课程处理完成")

    def _blob(self, rid, file):
        rev = revision(file)
        if not file.get("updated_at") and not file.get("modified_at"):
            # 没有可靠修改标识时重新获取，不能武断跳过。
            rev = hashlib.sha256((rev + now()).encode()).hexdigest()[:24]
        saved = self.store.blob(self.ns, rid, rev)
        if saved:
            p = Path(saved["path"])
            try:
                p = under(self.root, str(p.relative_to(self.root)))
                if p.is_file() and p.stat().st_size == saved["size"] and sha_file(p) == saved["sha"]:
                    return dict(saved, downloaded=False)
            except (ValueError, OSError):
                pass
        if self.expired:
            raise AuthExpired("认证失效，此附件未下载")
        if not file.get("url"):
            raise SyncError("Canvas 未提供可用的附件下载地址")
        p = under(self.root, ".canvas-sync", self.ns, "cache", safe_name(rid), rev)
        self.canvas.http.download(file["url"], p, file.get("size"), file.get("filename", ""))
        sha, size = sha_file(p), p.stat().st_size
        self.store.save_blob(self.ns, rid, rev, p, sha, size)
        return {"path": str(p), "sha": sha, "size": size, "revision": rev, "downloaded": True}

    def _group(self, cid, group, files, blobs, folder_parts):
        valid = [rid for rid in group["files"] if rid in blobs]
        folder_lists = [folder_parts(files[rid]) for rid in valid]
        common = []
        if folder_lists:
            for parts in zip(*folder_lists):
                if len(set(parts)) != 1:
                    break
                common.append(parts[0])
        assets, originals, conflicts = {}, {}, False
        for rid in valid:
            blob = blobs[rid]
            relative = Path(*folder_parts(files[rid])[len(common):]) / safe_name(files[rid].get("filename") or f"file-{rid}")
            path, status, remote = self.writer.write(f'{cid}:{group["key"]}:file:{rid}', group["dir"] / relative,
                                                     Path(blob["path"]), blob["sha"], blob["revision"])
            assets[rid] = quote(path.relative_to(self.root / group["dir"]).as_posix())
            originals[rid] = relative
            conflicts |= status == "local_modified"
            row = self.store.resource(self.ns, cid, "file", rid)
            if row:
                row["locations"] = sorted(set(row.get("locations", []) + [str(path)]))
                if remote:
                    row["status"], row["reason"] = "local_modified", "某个作业/模块副本有本地修改；完整远程组合已另存"
                    self.stats["local_modified"] += 1
                self.store.save_resource(self.ns, cid, "file", rid, row)
        note = group.get("note", "")
        if len(valid) < len(group["files"]):
            note += " 部分 Canvas 附件未获取，请查看课程索引中的失败或权限原因。"
        if conflicts:
            version = hashlib.sha256(''.join(blobs[r]["sha"] for r in sorted(valid)).encode()).hexdigest()[:12]
            snapshot = group["dir"] / "_Canvas远程组合" / version
            snapshot_assets = {}
            for rid in valid:
                blob = blobs[rid]
                p, _, _ = self.writer.write(f'{cid}:{group["key"]}:snapshot:{version}:{rid}', snapshot / originals[rid],
                                            Path(blob["path"]), blob["sha"], blob["revision"])
                snapshot_assets[rid] = quote(p.relative_to(self.root / snapshot).as_posix())
            self.event("已保留本地修改，并另存完整附件组合，供 notebook 与 CSV 配套使用")
        title = group.get("title", group["dir"].name)
        source = group.get("source", f'{self.canvas.base}/courses/{cid}/modules')
        body = group.get("body", "<p>此模块的文件保留在本目录。作业与页面见课程索引。</p>")
        local_links = {}
        if not group.get("kind"):
            local_rows = self.store.resources(self.ns, cid)
            remaining = {}
            body += "<ul>"
            for url, label in group["links"].items():
                resource = next((r for r in local_rows if r.get("source") == url and r.get("path")), None)
                if resource:
                    local_links[url] = quote(os.path.relpath(resource["path"], self.root / group["dir"]).replace(os.sep, "/"))
                    body += f'<li><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></li>'
                else:
                    remaining[url] = label
            body += "</ul>"
            group["links"] = remaining
        due = group.get("document", {}).get("due_at") if group.get("kind") == "assignment" else False
        payload = offline_document(title, body, source, due, assets, self.tz, note, group["links"], local_links)
        sha = hashlib.sha256(payload).hexdigest()
        path, status, remote = self.writer.write(f'{cid}:{group["key"]}:document', group["dir"] / "_Canvas要求.html", payload, sha, sha)
        if conflicts:
            copy = offline_document(title, body, source, due, snapshot_assets, self.tz, note, group["links"])
            digest = hashlib.sha256(copy).hexdigest()
            self.writer.write(f'{cid}:{group["key"]}:snapshot:{version}:doc', snapshot / "_Canvas要求.html", copy, digest, digest)
        if group.get("kind"):
            self.stats[status] += 1
            old = self.store.resource(self.ns, cid, group["kind"], group["id"]) or {}
            self.store.save_resource(self.ns, cid, group["kind"], group["id"], {
                "title": title, "due": due if due is not False else None, "path": str(path), "sha": sha,
                "updated": group["document"].get("updated_at"), "source": source, "status": status,
                "reason": note + (" 已保留本地修改，远程版本另存。" if remote else ""), "remote_path": str(remote) if remote else "",
                "fetched": now(), "synced": now() if status != "skipped" else old.get("synced", now()),
                "attachments": group["files"], "links": group["links"]})

    def _index(self, cid, course, course_dir):
        from .index import write_index
        write_index(self.store, self.ns, cid, course, self.root / course_dir, self.root, self.tz)
