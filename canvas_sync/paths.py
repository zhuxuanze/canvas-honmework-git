import hashlib
import os
import re
import unicodedata
from pathlib import Path


def safe_name(value: str, limit: int = 100) -> str:
    name = unicodedata.normalize("NFC", str(value))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = "未命名"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", name, re.I):
        name = "_" + name
    if len(name.encode("utf-8")) > limit:
        suffix = Path(name).suffix[:12]
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        budget = limit - len(suffix.encode()) - 10
        stem = name[:-len(suffix)] if suffix else name
        while len(stem.encode()) > budget:
            stem = stem[:-1]
        name = f"{stem}~{digest}{suffix}"
    return name


def under(root: Path, *parts: str) -> Path:
    """拒绝绝对路径、.. 和符号链接；解析后必须在用户选定的根内。"""
    root = root.resolve()
    candidate = root.joinpath(*parts)
    if any(p in {"..", "."} for part in parts for p in Path(part).parts):
        raise ValueError("不安全的相对路径")
    if any(Path(p).is_absolute() for p in parts):
        raise ValueError("不允许绝对路径")
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ValueError("资料目录中的符号链接不允许写入")
        current = current.parent
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("路径超出资料目录")
    return candidate


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path
