"""工作目录解析与本机目录选择。"""
from __future__ import annotations

import os
from pathlib import Path


def normalize_working_directory(value: str | os.PathLike[str] | None) -> str | None:
    """只接受已存在的目录，并返回绝对路径。"""
    if value is None:
        return None
    raw = os.fspath(value).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return str(resolved) if resolved.is_dir() else None


def choose_working_directory(initial_dir: str | None = None) -> str | None:
    """通过本机文件夹选择器返回用户选择的目录；取消时返回 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None

    initial = normalize_working_directory(initial_dir) or str(Path.home())
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    try:
        # 确保选择器置顶且获得焦点，避免被浏览器窗口遮挡
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=initial,
            title="选择 AtomCode 代理工作目录",
            mustexist=True,
        )
    finally:
        root.destroy()
    return normalize_working_directory(selected)
