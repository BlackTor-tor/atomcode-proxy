"""审查修复项的回归测试。

覆盖：
- Host 头白名单校验（防 DNS rebinding）
- _stop_chat_shielded 重新抛出 CancelledError
- 会话超量按 LRU 淘汰并停止被淘汰 session
- Anthropic 流式 reasoning -> thinking block
- _parse_version 预发布比较
- ConversationKeyResolver 指纹消息数截断
- 下载文件名净化
"""
import asyncio

import httpx

import atomcode_proxy.daemon as daemon_module
from atomcode_proxy.app import _sanitize_download_filename, create_app
from atomcode_proxy.config import Config
from atomcode_proxy.conversation import ConversationKeyResolver
from atomcode_proxy.daemon import AtomCodeDaemon
from atomcode_proxy.updater import _parse_version
from tests.test_adapters import FakeDaemon
from tests.test_daemon_lifecycle import FakeResponse


class CountingSessionClient:
    """每次 /sessions 返回递增的 session_id，记录所有请求。"""

    def __init__(self):
        self.posts = []
        self._next_id = 0

    async def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        if path == "/sessions":
            self._next_id += 1
            return FakeResponse(payload={"session_id": f"session-{self._next_id}"})
        return FakeResponse()

    async def aclose(self):
        return None


def test_settings_api_rejects_foreign_host_header(tmp_path):
    """DNS rebinding 场景：Host 头非本机地址的请求必须被拒绝。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            evil = await client.post(
                "/api/validate-dir",
                headers={"host": "evil.example.com:8765"},
                json={"path": str(tmp_path)},
            )
            local = await client.post(
                "/api/validate-dir",
                headers={"host": "127.0.0.1:8765"},
                json={"path": str(tmp_path)},
            )
            ipv6 = await client.post(
                "/api/validate-dir",
                headers={"host": "[::1]:8765"},
                json={"path": str(tmp_path)},
            )

        assert evil.status_code == 403
        assert local.status_code == 200
        assert ipv6.status_code == 200

    asyncio.run(run())


def test_settings_api_allows_port_without_suffix_on_default_http_port(tmp_path):
    """监听 80（HTTP 默认端口）时，客户端 Host 头省略 :80，不应被误拒。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path), port=80))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post(
                "/api/validate-dir",
                headers={"host": "127.0.0.1"},
                json={"path": str(tmp_path)},
            )

        assert response.status_code == 200

    asyncio.run(run())


def test_stop_chat_shielded_propagates_cancel_and_completes_stop():
    """取消传播到 _stop_chat_shielded 时：stop 请求仍完成，且 CancelledError 必须重新抛出。"""

    async def run():
        daemon = AtomCodeDaemon()
        posts = []

        async def slow_stop(session_id):
            await asyncio.sleep(0.2)
            posts.append(("/chat/stop", {"json": {"session_id": session_id}}))

        daemon.stop_chat = slow_stop

        task = asyncio.create_task(daemon._stop_chat_shielded("session-9"))
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("CancelledError 必须向外传播")

        await asyncio.sleep(0.3)
        assert ("/chat/stop", {"json": {"session_id": "session-9"}}) in posts

    asyncio.run(run())


def test_state_eviction_is_lru_and_stops_evicted_session():
    """会话超 MAX_CONVERSATIONS 时：按 last_used 淘汰最旧者，并停止其 daemon session。"""

    async def run():
        original = daemon_module.MAX_CONVERSATIONS
        daemon_module.MAX_CONVERSATIONS = 3
        try:
            daemon = AtomCodeDaemon()
            client = CountingSessionClient()
            daemon._client = client

            async def ensure(key):
                state = await daemon._state_for(key, "F:/workspace")
                await daemon._ensure_session(state, [])

            for key in ("conv-1", "conv-2", "conv-3"):
                await ensure(key)
            # 显式错开 last_used，使 conv-1 成为 LRU
            daemon._states["conv-1"].last_used = 100.0
            daemon._states["conv-2"].last_used = 200.0
            daemon._states["conv-3"].last_used = 300.0

            await ensure("conv-4")

            assert "conv-1" not in daemon._states
            assert "conv-2" in daemon._states
            # 被淘汰的 conv-1（session-1）必须收到 stop 请求，避免 daemon 残留任务
            assert ("/chat/stop", {"json": {"session_id": "session-1"}}) in client.posts
        finally:
            daemon_module.MAX_CONVERSATIONS = original

    asyncio.run(run())


class ReasoningDaemon(FakeDaemon):
    """先输出 reasoning 再输出 text 的模拟 daemon。"""

    async def chat_with_session(self, message, **kwargs):
        self.calls.append((message, kwargs))
        yield {"type": "reasoning", "content": "thinking hard"}
        yield {"type": "text", "content": "answer"}
        yield {"type": "done", "stop_reason": "stopped"}


def test_anthropic_stream_maps_reasoning_to_thinking_block(tmp_path):
    """reasoning 事件应输出为 thinking block（index 0），text 在其后的独立 block（index 1）。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = ReasoningDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/messages",
                json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 200
        body = response.text
        assert '"content_block": {"type": "thinking", "thinking": ""}' in body
        assert '"delta": {"type": "thinking_delta", "thinking": "thinking hard"}' in body
        # thinking 结束后 text block 使用下一个索引
        assert '"index": 1' in body
        assert '"delta": {"type": "text_delta", "text": "answer"}' in body
        assert '"stop_reason": "end_turn"' in body

    asyncio.run(run())


def test_anthropic_non_stream_includes_thinking_content(tmp_path):
    """非流式响应应把 reasoning 聚合为 thinking content block，置于 text 之前。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = ReasoningDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/messages",
                json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 200
        content = response.json()["content"]
        assert content[0] == {"type": "thinking", "thinking": "thinking hard"}
        assert content[1] == {"type": "text", "text": "answer"}

    asyncio.run(run())


def test_parse_version_orders_prerelease_below_release():
    assert _parse_version("v0.1.13-rc1") < _parse_version("v0.1.13")
    assert _parse_version("0.1.13") < _parse_version("0.2.0")
    assert _parse_version("0.1.13") > _parse_version("0.1.12")
    assert _parse_version("") == ((0,), 0)


def test_resolver_fingerprint_truncation_keeps_prefix_matching():
    resolver = ConversationKeyResolver(max_messages=2)
    messages = [
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "m2"},
        {"role": "user", "content": "m3"},
    ]
    resolver.remember("scope", "generated:scope:k", messages)
    entry = resolver._entries["scope"][0]
    # 指纹只保留前 2 条
    assert len(entry.last_request) == 2
    # 截断后的指纹仍是完整历史的前缀，可正常命中同一会话
    assert resolver.resolve("scope", messages) == "generated:scope:k"


def test_sanitize_download_filename():
    assert _sanitize_download_filename('evil"; rm -rf\r\n.exe') == "evil_; rm -rf__.exe"
    assert _sanitize_download_filename("") == "update.exe"
    assert _sanitize_download_filename("atomcode-proxy-0.1.13-windows-x64.exe") == (
        "atomcode-proxy-0.1.13-windows-x64.exe"
    )
