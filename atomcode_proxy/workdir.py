"""工作目录解析与本机目录选择。"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("atomcode_proxy.workdir")

# 目录选择器互斥：wait_for 超时不会终止 to_thread 中的线程，残留的 Tk
# 对话框仍持锁；后续请求在此排队而非并发创建第二个 Tk root（并发多 Tk
# 实例跨线程极易崩溃）。
_chooser_lock = threading.Lock()


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
    # 串行化：同一时刻只允许一个目录选择器实例（见 _chooser_lock 注释）
    with _chooser_lock:
        # tkinter 官方仅支持主线程，在 asyncio.to_thread 的工作线程中运行可能抛
        # TclError/RuntimeException 等各类异常，统一捕获降级为“未选择”。
        try:
            root = tk.Tk()
        except Exception as exc:
            log.warning("初始化目录选择器失败: %s", exc)
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
        except Exception as exc:
            log.warning("打开目录选择器失败: %s", exc)
            return None
        finally:
            try:
                root.destroy()
            except Exception:
                pass
    return normalize_working_directory(selected)
