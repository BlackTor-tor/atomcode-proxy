"""check_for_update 更新检查主流程的回归测试。

覆盖：
- GitHub API 渠道成功返回新版本
- API 403 限流时降级 302 渠道
- 双渠道失败抛出 RuntimeError（区分"失败"与"无更新"）
- 5 分钟结果缓存
"""
import asyncio

import httpx

import atomcode_proxy.updater as updater


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeAsyncClient:
    """替换 httpx.AsyncClient：按预设队列依次返回响应/异常，记录请求 URL。"""

    responses: list = []
    requests: list = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, **kwargs):
        type(self).requests.append(url)
        resp = type(self).responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _api_response(tag="v9.9.9"):
    return FakeResponse(
        200,
        payload={
            "tag_name": tag,
            "html_url": f"https://github.com/BlackTor-tor/atomcode-proxy/releases/tag/{tag}",
            "body": "notes",
            "published_at": "2026-01-01T00:00:00Z",
            "assets": [
                {"name": f"atomcode-proxy-{tag.lstrip('v')}-windows-x64.exe",
                 "browser_download_url": f"https://example.com/{tag}.exe"}
            ],
        },
    )


def test_check_for_update_returns_new_version_via_api(monkeypatch):
    async def run():
        monkeypatch.setattr(updater.httpx, "AsyncClient", FakeAsyncClient)
        FakeAsyncClient.responses = [_api_response()]
        FakeAsyncClient.requests = []
        updater._last_check = None

        result = await updater.check_for_update("0.1.14")

        assert result is not None
        assert result["latest_version"] == "9.9.9"
        assert result["download_url"].endswith("v9.9.9.exe")

    asyncio.run(run())


def test_check_for_update_falls_back_to_redirect_channel_on_api_403(monkeypatch):
    async def run():
        monkeypatch.setattr(updater.httpx, "AsyncClient", FakeAsyncClient)
        # API 403（限流）-> 降级渠道 302 到 tag v0.2.0
        FakeAsyncClient.responses = [
            FakeResponse(403),
            FakeResponse(302, headers={"location": "https://github.com/BlackTor-tor/atomcode-proxy/releases/tag/v0.2.0"}),
        ]
        FakeAsyncClient.requests = []
        updater._last_check = None

        result = await updater.check_for_update("0.1.14")

        assert result is not None
        assert result["latest_version"] == "0.2.0"

    asyncio.run(run())


def test_check_for_update_raises_when_all_channels_fail(monkeypatch):
    async def run():
        monkeypatch.setattr(updater.httpx, "AsyncClient", FakeAsyncClient)
        FakeAsyncClient.responses = [httpx.ConnectError("boom"), httpx.ConnectError("boom2")]
        FakeAsyncClient.requests = []
        updater._last_check = None

        try:
            await updater.check_for_update("0.1.14")
        except RuntimeError:
            pass
        else:
            raise AssertionError("双渠道失败应抛 RuntimeError")

    asyncio.run(run())


def test_check_for_update_caches_result_within_ttl(monkeypatch):
    async def run():
        monkeypatch.setattr(updater.httpx, "AsyncClient", FakeAsyncClient)
        FakeAsyncClient.responses = [_api_response()]
        FakeAsyncClient.requests = []
        updater._last_check = None

        first = await updater.check_for_update("0.1.14")
        second = await updater.check_for_update("0.1.14")

        assert first is not None
        assert second == first
        # 第二次命中缓存，不应再发起任何请求
        assert len(FakeAsyncClient.requests) == 1

    asyncio.run(run())
