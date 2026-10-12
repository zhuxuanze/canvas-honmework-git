import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .paths import private_dir


def normalize_base(value: str, allow_local: bool = False) -> str:
    p = urlsplit(value.strip())
    local = allow_local and p.hostname in {"127.0.0.1", "localhost", "::1"}
    if (p.scheme != "https" and not (local and p.scheme == "http")) or not p.hostname:
        raise ValueError("请输入 HTTPS Canvas 根地址，例如 https://canvas.example.edu")
    if p.username or p.password or p.query or p.fragment or p.path not in {"", "/"}:
        raise ValueError("只填写 Canvas 根地址，不包含账号、路径或查询参数")
    return f"{p.scheme}://{p.netloc.lower()}"


def home_dir() -> Path:
    override = os.environ.get("CANVAS_SYNC_HOME")
    if override:
        return private_dir(Path(override).expanduser())
    if sys.platform == "darwin":
        p = Path.home() / "Library/Application Support/CanvasLocalSync"
    elif os.name == "nt":
        p = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CanvasLocalSync"
    else:
        p = Path.home() / ".local/share/canvas-local-sync"
    return private_dir(p)


@dataclass
class Config:
    base_url: str = ""
    output_dir: str = str(Path.home() / "Downloads/Canvas资料")
    auth_mode: str = "token"
    timezone: str = "Asia/Shanghai"
    profile: str = ""
    user_name: str = ""
    user_id: str = ""
    concurrency: int = 3

    @classmethod
    def load(cls, home: Path):
        p = home / "config.json"
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text("utf-8"))
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})

    def save(self, home: Path):
        private_json(home / "config.json", asdict(self))


def private_json(path: Path, data):
    private_dir(path.parent)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    if os.name != "nt":
        path.chmod(0o600)


def profile_id(base: str, user_id) -> str:
    return hashlib.sha256(f"{base}|{user_id}".encode()).hexdigest()[:20]


def token_key(base: str) -> str:
    return hashlib.sha256(base.encode()).hexdigest()


def read_token(base: str) -> str | None:
    import keyring
    try:
        return keyring.get_password("CanvasLocalSync", token_key(base))
    except Exception:
        return None


def save_token(base: str, token: str):
    import keyring
    try:
        backend = keyring.get_keyring()
        # 不接受可选第三方明文文件后端。
        if not backend.__class__.__module__.startswith(("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService")):
            raise RuntimeError()
        keyring.set_password("CanvasLocalSync", token_key(base), token)
    except Exception:
        raise ValueError("系统钥匙串不可用，请选择“仅本次使用”；不会保存到明文配置") from None
