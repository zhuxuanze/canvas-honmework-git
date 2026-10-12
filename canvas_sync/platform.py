import os
import subprocess
import sys
from pathlib import Path


def open_local(path: str, root: str):
    p = Path(path).resolve()
    base = Path(root).expanduser().resolve()
    if not p.is_relative_to(base) or not p.exists():
        raise ValueError("文件不存在或不在资料目录中")
    viewable = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".html", ".htm"}
    if p.suffix.lower() not in viewable and p.is_file():
        # 脚本/notebook 只在文件管理器定位，绝不使用可能执行的文件关联。
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        elif os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{p}"])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    elif os.name == "nt":
        os.startfile(p)
    else:
        subprocess.Popen(["xdg-open", str(p)])
