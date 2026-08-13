"""检查更新模块：查询 GitHub Releases 获取最新版本信息。

- check_for_update()：对比当前版本与 GitHub 最新 release
- 下载由 app.py 流式代理转发，浏览器保存到其默认下载路径
"""
from __future__ import annotations

import logging
import re
import time

import httpx

log = logging.getLogger("atomcode_proxy.updater")

# GitHub 仓库信息
GITHUB_OWNER = "BlackTor-tor"
GITHUB_REPO = "atomcode-proxy"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"

# HTTP 请求通用头
_UA = {"User-Agent": "atomcode-proxy-updater"}

# 检查结果缓存：避免频繁点击/刷新触发 GitHub API 匿名限流（60 次/小时/IP）
CACHE_TTL = 300.0  # 5 分钟
_last_check: tuple[str, float, dict | None] | None = None  # (current_version, monotonic 时间戳, 结果；None=无更新)


def _parse_version(version: str) -> tuple[int, ...]:
    """将版本字符串解析为整数元组，如 'v0.1.5' -> (0, 1, 5)。

    非数字部分自动忽略；预发布后缀（-rc1 / +build2 等）不参与比较；
    空版本回退为 (0,)。
    """
    if not version:
        return (0,)
    core = re.split(r"[-+]", version.strip().lstrip("vV"))[0]
    parts = re.findall(r"\d+", core)
    return tuple(int(p) for p in parts) if parts else (0,)


async def check_for_update(current_version: str) -> dict | None:
    """查询 GitHub Releases 获取最新版本信息。

    - API 匿名限流（403 rate limit）时自动降级：改用 GitHub 网页端
      /releases/latest 的 302 重定向解析最新 tag（不受 API 限流影响）。
    - 成功结果缓存 5 分钟，避免频繁点击/刷新触发限流。
    - 返回 None 表示当前已是最新；所有渠道均失败时抛出 RuntimeError，
      由调用方区分「失败」与「无更新」两种状态。
    返回 dict 包含 latest_version / download_url / release_url 等。
    """
    global _last_check
    now = time.monotonic()
    if _last_check is not None and _last_check[0] == current_version and now - _last_check[1] < CACHE_TTL:
        log.info("复用检查结果缓存（%.0f 秒内）", CACHE_TTL)
        return _last_check[2]

    info = await _fetch_latest_via_api()
    if info is None:
        info = await _fetch_latest_via_redirect()
    if info is None:
        raise RuntimeError("查询 GitHub Releases 失败（API 与降级渠道均不可用）")

    tag = info.get("tag", "")
    latest_version = tag.lstrip("vV")
    result: dict | None
    if _parse_version(latest_version) <= _parse_version(current_version):
        log.info("当前版本 %s 已是最新（latest=%s）", current_version, latest_version)
        result = None
    else:
        result = {
            "latest_version": latest_version,
            "current_version": current_version,
            "download_url": info.get("download_url", ""),
            "asset_name": info.get("asset_name", ""),
            "release_url": info.get("release_url", f"{GITHUB_RELEASES_URL}/tag/{tag}"),
            "release_notes": info.get("release_notes", ""),
            "published_at": info.get("published_at", ""),
        }
    _last_check = (current_version, now, result)
    return result


async def _fetch_latest_via_api() -> dict | None:
    """渠道一：GitHub REST API（匿名限流 60 次/小时/IP，可能 403）。

    成功返回 {'tag', 'download_url', 'asset_name', 'release_url',
    'release_notes', 'published_at'}；失败返回 None。
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(GITHUB_API_URL, headers=_UA, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("GitHub API 查询失败，尝试降级渠道: %s", exc)
        return None

    tag = data.get("tag_name") or ""
    if not tag:
        log.warning("GitHub API 返回缺少 tag_name，尝试降级渠道")
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

    return {
        "tag": tag,
        "download_url": download_url,
        "asset_name": asset_name,
        "release_url": data.get("html_url", f"{GITHUB_RELEASES_URL}/tag/{tag}"),
        "release_notes": data.get("body", ""),
        "published_at": data.get("published_at", ""),
    }


async def _fetch_latest_via_redirect() -> dict | None:
    """渠道二（降级）：网页端 /releases/latest 的 302 重定向解析 tag。

    GitHub 网页端与资产下载端点不受 API 匿名限流影响。资产名按仓库
    约定的 atomcode-proxy-<版本>-windows-x64.exe 命名构造。
    """
    url = f"{GITHUB_RELEASES_URL}/latest"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            resp = await client.get(url, headers=_UA)
            if resp.status_code not in (301, 302):
                log.warning("降级渠道未重定向: %s", resp.status_code)
                return None
            location = resp.headers.get("location", "")
    except Exception as exc:
        log.warning("降级渠道查询失败: %s", exc)
        return None

    m = re.search(r"/releases/tag/([^/?]+)", location)
    tag = m.group(1) if m else ""
    if not tag:
        log.warning("降级渠道无法解析 tag，location=%r", location)
        return None

    log.info("降级渠道解析到最新 tag: %s", tag)
    version = tag.lstrip("vV")
    asset_name = f"atomcode-proxy-{version}-windows-x64.exe"
    return {
        "tag": tag,
        "download_url": f"{GITHUB_RELEASES_URL}/download/{tag}/{asset_name}",
        "asset_name": asset_name,
        "release_url": f"{GITHUB_RELEASES_URL}/tag/{tag}",
        "release_notes": "",
        "published_at": "",
    }
