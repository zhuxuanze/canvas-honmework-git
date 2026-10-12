import hashlib
import os
import shutil
import uuid
from pathlib import Path

from .paths import sha_file, under
from .schema import artifact_fields


class Materializer:
    def __init__(self, store, ns, root: Path):
        self.store, self.ns, self.root = store, ns, root.resolve()
        self.prefix = hashlib.sha256(str(self.root).encode()).hexdigest()[:12] + ":"

    def _record(self, logical, path, sha, category=None):
        fields = artifact_fields(self.ns, logical, path, self.root)
        self.store.record_artifact(self.ns, path, logical, sha, fields, category)

    def _directory(self, logical, destination):
        missing = []
        current = destination
        while current != self.root and not current.exists():
            missing.append(current)
            current = current.parent
        destination.mkdir(parents=True, exist_ok=True)
        fields = artifact_fields(self.ns, logical, destination, self.root)
        if fields:
            for path in reversed(missing):
                if path == Path(fields["course_dir"]) or path.is_relative_to(fields["course_dir"]):
                    self.store.record_artifact(self.ns, path, logical, "", fields, "directory")

    def write(self, logical, relative: Path, data: Path | bytes, digest, revision):
        logical = self.prefix + logical
        old = self.store.placement(self.ns, logical)
        if old and Path(old["path"]).is_relative_to(self.root) and Path(old["path"]).parent == (self.root / relative).parent:
            destination = under(self.root, str(Path(old["path"]).relative_to(self.root)))
        else:
            old = None
            destination = under(self.root, str(relative))
            desired = destination
            count = 0
            while (self.store.owner(self.ns, destination) not in {None, logical}
                   or (destination.parent.exists() and any(
                       p.name.casefold() == destination.name.casefold() for p in destination.parent.iterdir()))):
                count += 1
                suffix = hashlib.sha256(logical.encode()).hexdigest()[:8]
                destination = desired.with_name(f"{desired.stem}~{suffix}{'-'+str(count) if count > 1 else ''}{desired.suffix}")
                destination = under(self.root, str(destination.relative_to(self.root)))
        self._directory(logical, destination.parent)
        exists = destination.exists()
        current_sha = sha_file(destination) if exists else None
        if old and exists and current_sha != old["sha"]:
            self._record(logical, destination, old["sha"])
            # 用户更改的文件保留原位。另存远程文件，返回冲突信息。
            remote = destination.with_name(f"{destination.stem}~Canvas远程-{digest[:10]}{destination.suffix}")
            remote = under(self.root, str(remote.relative_to(self.root)))
            if not remote.exists() or sha_file(remote) != digest:
                if remote.exists():
                    remote = remote.with_name(f"{remote.stem}~{uuid.uuid4().hex[:8]}{remote.suffix}")
                self._copy(data, remote)
            self._record(logical, remote, digest, "remote")
            return destination, "local_modified", remote
        if exists and current_sha == digest:
            self.store.save_placement(self.ns, logical, destination, digest, revision)
            self._record(logical, destination, digest)
            return destination, "skipped", None
        status = "updated" if old and exists else "added"
        if exists:
            history = under(self.root, str(destination.parent.relative_to(self.root)), "_Canvas历史版本")
            self._directory(logical, history)
            archive = history / f"{destination.stem}~{current_sha[:12]}{destination.suffix}"
            if not archive.exists():
                shutil.copy2(destination, archive)
            if not archive.is_symlink() and sha_file(archive) == current_sha:
                self._record(logical, archive, current_sha, "history")
        self._copy(data, destination)
        self.store.save_placement(self.ns, logical, destination, digest, revision)
        self._record(logical, destination, digest)
        return destination, status, None

    @staticmethod
    def _copy(data, destination):
        temp = destination.with_name(destination.name + f".{uuid.uuid4().hex[:8]}.part")
        try:
            if isinstance(data, bytes):
                temp.write_bytes(data)
            else:
                shutil.copyfile(data, temp)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
