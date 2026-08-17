import asyncio
import time

from atomcode_proxy.daemon import AtomCodeDaemon, SessionBusyError


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


class CountingClient(FakeClient):
    """每次 /sessions 返回递增的 session_id。"""

    def __init__(self):
        super().__init__()
        self._next_id = 0

    async def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        if path == "/sessions":
            self._next_id += 1
            return FakeResponse(payload={"session_id": f"session-{self._next_id}"})
        return FakeResponse()


def test_chat_retries_once_on_session_busy_409():
    """daemon 返回 409（session busy）时应 stop 旧 session、重建并重试一次。"""

    async def run():
        daemon = AtomCodeDaemon()
        client = CountingClient()
        daemon._client = client

        first = True

        async def flaky_stream(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise SessionBusyError("session-1")
            yield {"type": "done", "stop_reason": "stopped"}

        daemon._chat_stream_raw = flaky_stream

        out = []
        async for ev in daemon.chat_with_session(
            "hi", working_dir="F:/workspace", conversation_key="conversation-1"
        ):
            out.append(ev)

        assert any(e.get("type") == "done" for e in out)
        # 409 后：stop 旧 session + 重建（第二次 /sessions）
        sessions = [entry for entry in client.posts if entry[0] == "/sessions"]
        stops = [entry for entry in client.posts if entry[0] == "/chat/stop"]
        assert len(sessions) == 2
        assert len(stops) >= 1

    asyncio.run(run())


def test_chat_retry_raises_after_second_409():
    """连续两次 409 时不应无限重试，应浮出错误。"""

    async def run():
        daemon = AtomCodeDaemon()
        client = CountingClient()
        daemon._client = client

        async def always_busy(*args, **kwargs):
            if False:
                yield {}
            raise SessionBusyError("session-busy")

        daemon._chat_stream_raw = always_busy

        async def consume():
            async for _ in daemon.chat_with_session(
                "hi", working_dir="F:/workspace", conversation_key="conversation-1"
            ):
                pass

        try:
            await consume()
        except SessionBusyError:
            pass
        else:
            raise AssertionError("第二次 409 应抛出 SessionBusyError")

        # 只重试一次：/sessions 恰好两次（首次 + 一次重试）
        sessions = [entry for entry in client.posts if entry[0] == "/sessions"]
        assert len(sessions) == 2

    asyncio.run(run())


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
