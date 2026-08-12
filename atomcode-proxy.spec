# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: 打包 atomcode-proxy 为单文件 exe。

注意：
- .env 绝不打包进 exe（由 exe 旁的 .env 提供配置）。
- assets/ 目录（Logo 等）必须打包进 exe，运行时解压到 _MEIPASS。
- 不收集 uvloop（Windows 不可用）。
"""
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_ROOT = Path(SPECPATH).resolve()

# 从 atomcode_proxy/__init__.py 读取版本号（供后续 versioninfo/命名使用）
_init_py = (SPEC_ROOT / "atomcode_proxy" / "__init__.py").read_text(encoding="utf-8")
_match = re.search(r'__version__\s*=\s*"([^"]+)"', _init_py)
VERSION = _match.group(1) if _match else "0.0.0"

# uvicorn 全部子模块（uvicorn 内部按需 import，静态分析收集不全）
hiddenimports = collect_submodules("uvicorn")

# 可选依赖：环境未装则跳过（容错）
for _mod in ("websockets", "httptools", "watchfiles", "pystray", "PIL"):
    try:
        __import__(_mod)
        hiddenimports.append(_mod)
    except ImportError:
        pass

# pystray 和 Pillow 的依赖收集
for _mod in collect_submodules("pystray") + collect_submodules("PIL"):
    if _mod not in hiddenimports:
        hiddenimports.append(_mod)

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["uvloop", "PySide6"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="atomcode-proxy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    onefile=True,
    icon="assets/logo.ico",
)
