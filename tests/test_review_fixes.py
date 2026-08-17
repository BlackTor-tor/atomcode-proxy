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
from tests.test_adapters import ErrorDaemon, FakeDaemon
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


def test_anthropic_stream_event_order_matches_official_contract(tmp_path):
    """官方事件序：content_block_stop -> message_delta(stop_reason, usage) -> message_stop。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = FakeDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/messages",
                json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 200
        body = response.text
        i_block_stop = body.index("event: content_block_stop")
        i_delta = body.index("event: message_delta")
        i_msg_stop = body.index("event: message_stop")
        assert i_block_stop < i_delta < i_msg_stop
        # message_delta 透出真实 usage（来自 daemon tokens 事件），而非恒为 0/估算
        assert '"input_tokens": 3' in body

    asyncio.run(run())


def test_anthropic_stream_surfaces_daemon_error_with_message_delta(tmp_path):
    """错误路径也必须发出 message_delta（stop_reason 非 null）并以 message_stop 收尾。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = ErrorDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/messages",
                json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 200
        body = response.text
        assert "[proxy error]" in body
        assert "Provider 'x' not found" in body
        assert body.index("event: content_block_stop") < body.index("event: message_delta")
        assert '"stop_reason": "end_turn"' in body
        assert "event: message_stop" in body

    asyncio.run(run())


def test_resolver_scope_count_is_bounded():
    """scope 总量必须有上限：API Key 为任意非空值，无界 scope 会造成内存泄漏。"""
    resolver = ConversationKeyResolver(max_entries=3)
    for i in range(5):
        scope = f"scope-{i}"
        key = resolver.resolve(scope, [{"role": "user", "content": "hi"}])
        resolver.remember(scope, key, [{"role": "user", "content": "hi"}])

    assert len(resolver._entries) <= 3
    # 最新的 scope 必须保留，最旧的被淘汰
    assert "scope-4" in resolver._entries
    assert "scope-0" not in resolver._entries


class ScriptedDaemon(FakeDaemon):
    """按调用顺序返回预设应答的模拟 daemon。"""

    def __init__(self, replies):
        super().__init__()
        self._replies = list(replies)

    async def chat_with_session(self, message, **kwargs):
        self.calls.append((message, kwargs))
        reply = self._replies.pop(0)
        yield {"type": "text", "content": reply}
        yield {"type": "done", "stop_reason": "stopped"}


def test_distinct_conversations_with_identical_prefix_stay_isolated(tmp_path):
    """同一客户端问了相同问题的两个会话：应答并入指纹后不得串会话。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = ScriptedDaemon(["answer-a", "answer-b", "answer-c"])
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        headers = {"User-Agent": "claude-code-test"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 会话 A 第 1 轮与会话 B 第 1 轮发送完全相同的消息
            for _ in range(2):
                response = await client.post(
                    "/v1/messages",
                    headers=headers,
                    json={"model": "x", "messages": [{"role": "user", "content": "hello"}]},
                )
                assert response.status_code == 200
            # 会话 A 第 2 轮：携带 A 自己的应答继续追问
            response = await client.post(
                "/v1/messages",
                headers=headers,
                json={
                    "model": "x",
                    "messages": [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "answer-a"},
                        {"role": "user", "content": "more"},
                    ],
                },
            )

        assert response.status_code == 200
        keys = [kwargs["conversation_key"] for _, kwargs in daemon.calls]
        # A 第 2 轮必须回到 A 自己的会话，而不是被前缀匹配并入更新的会话 B
        assert keys[2] == keys[0]
        assert keys[1] != keys[0]

    asyncio.run(run())
