import asyncio
import time

from atomcode_proxy.daemon import AtomCodeDaemon


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"success": True}
        self.text = ""

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self):
        self.posts = []

    async def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        if path == "/sessions":
            return FakeResponse(payload={"session_id": "session-1"})
        return FakeResponse()

    async def aclose(self):
        return None


class NoModelsClient(FakeClient):
    """模拟 /models 接口不可用。"""

    async def get(self, path, **kwargs):
        return FakeResponse(status_code=500)


def test_stop_chat_posts_session_id_to_daemon():
    async def run():
        daemon = AtomCodeDaemon()
        client = FakeClient()
        daemon._client = client

        await daemon.stop_chat("session-1")

        assert client.posts == [("/chat/stop", {"json": {"session_id": "session-1"}})]

    asyncio.run(run())


def test_cancelled_chat_stops_daemon_before_releasing_session():
    async def run():
        daemon = AtomCodeDaemon()
        client = FakeClient()
        daemon._client = client

        async def fake_stream(*args, **kwargs):
            await asyncio.sleep(60)
            yield {"type": "done"}

        daemon._chat_stream_raw = fake_stream

        async def consume():
            async for _ in daemon.chat_with_session(
                "hello",
                working_dir="F:/workspace",
                conversation_key="conversation-1",
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("chat task was expected to be cancelled")

        assert ("/chat/stop", {"json": {"session_id": "session-1"}}) in client.posts

    asyncio.run(run())


def test_new_session_imports_previous_messages_once():
    async def run():
        daemon = AtomCodeDaemon()
        client = FakeClient()
        daemon._client = client

        async def fake_stream(*args, **kwargs):
            yield {"type": "done", "stop_reason": "stopped"}

        daemon._chat_stream_raw = fake_stream
        history = [{"role": "system", "content": "system"}]

        async for _ in daemon.chat_with_session(
            "first",
            working_dir="F:/workspace",
            conversation_key="conversation-1",
            history=history,
        ):
            pass
        async for _ in daemon.chat_with_session(
            "second",
            working_dir="F:/workspace",
            conversation_key="conversation-1",
            history=history,
        ):
            pass

        imports = [entry for entry in client.posts if entry[0].endswith("/messages")]
        assert len(imports) == 1
        assert imports[0][1]["json"] == {"messages": history}

    asyncio.run(run())


def test_resolve_provider_falls_back_to_default_for_unknown_model():
    """未知模型名（如 claude-*/gpt-*）不在 daemon provider 名单时回退默认 provider。"""

    async def run():
        daemon = AtomCodeDaemon(default_provider="AtomGit-deepseek-v4-flash")
        # 预填 provider 名单缓存，避免测试发起真实网络请求
        daemon._providers_cache = {"AtomGit-Qwen-Qwen3-VL-8B-Instruct", "AtomGit-deepseek-v4-flash"}
        daemon._providers_cached_at = time.monotonic()

        assert await daemon.resolve_provider("claude-sonnet-4-5") == "AtomGit-deepseek-v4-flash"
        # 已知 provider 名仍直接透传
        assert await daemon.resolve_provider("AtomGit-Qwen-Qwen3-VL-8B-Instruct") == "AtomGit-Qwen-Qwen3-VL-8B-Instruct"
        # 别名映射优先级最高
        assert (
            await daemon.resolve_provider("my-alias", aliases={"my-alias": "AtomGit-Qwen-Qwen3-VL-8B-Instruct"})
            == "AtomGit-Qwen-Qwen3-VL-8B-Instruct"
        )
        # 无模型名用默认值
        assert await daemon.resolve_provider(None) == "AtomGit-deepseek-v4-flash"

    asyncio.run(run())


def test_resolve_provider_passes_through_when_provider_list_unavailable():
    """provider 名单获取失败（空集）时维持透传，交由 error 事件处理浮出错误。"""

    async def run():
        daemon = AtomCodeDaemon(default_provider="AtomGit-deepseek-v4-flash")
        daemon._client = NoModelsClient()

        assert await daemon.resolve_provider("claude-sonnet-4-5") == "claude-sonnet-4-5"

    asyncio.run(run())
