"""检查更新模块：查询 GitHub Releases 获取最新版本并下载。

- check_for_update()：对比当前版本与 GitHub 最新 release
- download_latest_release()：下载最新 release 的 exe 资产到本地下载目录
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger("atomcode_proxy.updater")

# GitHub 仓库信息
GITHUB_OWNER = "BlackTor-tor"
GITHUB_REPO = "atomcode-proxy"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"

# HTTP 请求通用头
_UA = {"User-Agent": "atomcode-proxy-updater"}


def _parse_version(version: str) -> tuple[int, ...]:
    """将版本字符串解析为整数元组，如 'v0.1.5' -> (0, 1, 5)。

    非数字部分自动忽略，空版本回退为 (0,)。
    """
    version = version.strip().lstrip("vV")
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else (0,)


async def check_for_update(current_version: str) -> dict | None:
    """查询 GitHub Releases 获取最新版本信息。

    返回 None 表示无更新或查询失败（失败时 log 记录异常）。
    返回 dict 包含 latest_version / download_url / release_url 等。
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(GITHUB_API_URL, headers=_UA, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("查询 GitHub Releases 失败: %s", exc)
        return None

    tag = data.get("tag_name", "")
    latest_version = tag.lstrip("vV")

    if _parse_version(latest_version) <= _parse_version(current_version):
        log.info("当前版本 %s 已是最新（latest=%s）", current_version, latest_version)
        return None

    # 查找 .exe 资产
    download_url = ""
    asset_name = ""
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(".exe"):
            download_url = asset.get("browser_download_url", "")
            asset_name = name
            break

    release_url = data.get("html_url", f"{GITHUB_RELEASES_URL}/tag/{tag}")

    return {
        "latest_version": latest_version,
        "current_version": current_version,
        "download_url": download_url,
        "asset_name": asset_name,
        "release_url": release_url,
        "release_notes": data.get("body", ""),
        "published_at": data.get("published_at", ""),
    }


async def download_latest_release(update_info: dict) -> Path | None:
    """下载最新版本 exe 到用户下载目录，返回本地文件路径。

    失败返回 None（由调用方展示兜底提示）。
    """
    download_url = update_info.get("download_url", "")
    if not download_url:
        log.warning("无可用下载链接")
        return None

    # 下载目录：优先用户 Downloads 文件夹，回退到主目录
    downloads_dir = Path.home() / "Downloads"
    if not downloads_dir.exists():
        downloads_dir = Path.home()
    downloads_dir.mkdir(parents=True, exist_ok=True)

    # 从 URL 或 update_info 提取文件名
    filename = download_url.rsplit("/", 1)[-1]
    if not filename:
        filename = update_info.get("asset_name") or f"atomcode-proxy-{update_info.get('latest_version', 'unknown')}-windows-x64.exe"
    target = downloads_dir / filename

    try:
        log.info("开始下载更新: %s -> %s", download_url, target)
        timeout = httpx.Timeout(300.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", download_url, headers=_UA) as resp:
                resp.raise_for_status()
                with open(target, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
        log.info("下载完成: %s (%.2f MB)", target, target.stat().st_size / 1024 / 1024)
        return target
    except Exception as exc:
        log.warning("下载更新失败: %s", exc)
        # 清理不完整文件
        if target.exists():
            try:
                target.unlink()
            except Exception:
                pass
        return None
