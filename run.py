"""一键启动：python run.py"""
import os
import sys

# PyInstaller console=False（无窗口）打包时，sys.stdout / sys.stderr 为 None。
# uvicorn 的日志格式器会调用 sys.stderr.isatty()，第三方库也可能 write()，
# 对 None 操作会抛 AttributeError 导致服务静默崩溃、托盘无法启动。
# 必须在导入 uvicorn 等库之前重定向到 os.devnull。
if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

from atomcode_proxy.__main__ import main

if __name__ == "__main__":
    main()