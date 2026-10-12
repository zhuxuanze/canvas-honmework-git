import hashlib
import os
import shutil
import uuid
from pathlib import Path

from .paths import sha_file, under


class Materializer:
    def __init__(self, store, ns, root: Path):
        self.store, self.ns, self.root = store, ns, root.resolve()
        self.prefix = hashlib.sha256(str(self.root).encode()).hexdigest()[:12] + ":"

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
        destination.parent.mkdir(parents=True, exist_ok=True)
        exists = destination.exists()
        current_sha = sha_file(destination) if exists else None
        if old and exists and current_sha != old["sha"]:
            # 用户更改的文件保留原位。另存远程文件，返回冲突信息。
            remote = destination.with_name(f"{destination.stem}~Canvas远程-{digest[:10]}{destination.suffix}")
            remote = under(self.root, str(remote.relative_to(self.root)))
            if not remote.exists() or sha_file(remote) != digest:
                if remote.exists():
                    remote = remote.with_name(f"{remote.stem}~{uuid.uuid4().hex[:8]}{remote.suffix}")
                self._copy(data, remote)
            return destination, "local_modified", remote
        if exists and current_sha == digest:
            self.store.save_placement(self.ns, logical, destination, digest, revision)
            return destination, "skipped", None
        status = "updated" if old and exists else "added"
        if exists:
            history = under(self.root, str(destination.parent.relative_to(self.root)), "_Canvas历史版本")
            history.mkdir(parents=True, exist_ok=True)
            archive = history / f"{destination.stem}~{current_sha[:12]}{destination.suffix}"
            if not archive.exists():
                shutil.copy2(destination, archive)
        self._copy(data, destination)
        self.store.save_placement(self.ns, logical, destination, digest, revision)
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
