# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: 打包 atomcode-proxy 为单文件 exe。

注意：
- 严禁添加 datas，.env 绝不打包进 exe（由 exe 旁的 .env 提供配置）。
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
for _mod in ("websockets", "httptools", "watchfiles"):
    try:
        __import__(_mod)
        hiddenimports.append(_mod)
    except ImportError:
        pass

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["uvloop"],
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
    console=True,
    onefile=True,
)
