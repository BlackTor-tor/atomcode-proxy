import asyncio
import json
import time
from pathlib import Path

import atomcode_proxy.daemon as daemon_module
from atomcode_proxy.daemon import AtomCodeDaemon, AtomCodeDaemonError, SessionBusyError


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


def test_chat_stream_without_terminal_event_raises_and_stops_session():
    """daemon SSE 提前 EOF 时必须报错，不能把半截回复当作正常结束。"""

    async def run():
        daemon = AtomCodeDaemon()
        client = FakeClient()
        daemon._client = client

        async def truncated_stream(*args, **kwargs):
            yield {"type": "text", "content": "partial"}

        daemon._chat_stream_raw = truncated_stream

        events = []
        try:
            async for event in daemon.chat_with_session(
                "hello",
                working_dir="F:/workspace",
                conversation_key="conversation-1",
            ):
                events.append(event)
        except AtomCodeDaemonError as exc:
            assert "ended before terminal event" in str(exc)
            assert exc.status_code == 502
        else:
            raise AssertionError("提前 EOF 必须抛出 AtomCodeDaemonError")

        assert events == [{"type": "text", "content": "partial"}]
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


# ---------- atomcode 5.0.6+ 动态 daemon token 适配 ----------


def test_read_dynamic_daemon_token_reads_local_token_file(tmp_path, monkeypatch):
    """本地 daemon 的随机 token 从 ~/.atomcode/daemon-<port>.json 读取；远程返回 None。"""
    (tmp_path / ".atomcode").mkdir()
    (tmp_path / ".atomcode" / "daemon-13456.json").write_text(
        json.dumps({"pid": 1, "port": 13456, "token": "abc123"}), encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert daemon_module._read_dynamic_daemon_token("http://127.0.0.1:13456") == "abc123"
    assert daemon_module._read_dynamic_daemon_token("http://localhost:13456") == "abc123"
    # 远程 daemon 不读本机文件
    assert daemon_module._read_dynamic_daemon_token("http://remote.example.com:13456") is None
    # 文件缺失/损坏返回 None
    assert daemon_module._read_dynamic_daemon_token("http://127.0.0.1:9999") is None
    (tmp_path / ".atomcode" / "daemon-13456.json").write_text("not-json", encoding="utf-8")
    assert daemon_module._read_dynamic_daemon_token("http://127.0.0.1:13456") is None


def test_daemon_constructor_prefers_dynamic_token(tmp_path, monkeypatch):
    """token 文件存在时优先使用动态 token；不存在时回退配置的静态 token。"""

    async def run():
        (tmp_path / ".atomcode").mkdir()
        (tmp_path / ".atomcode" / "daemon-13456.json").write_text(
            json.dumps({"pid": 1, "port": 13456, "token": "random-tok"}), encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        daemon = AtomCodeDaemon(daemon_token="atomcode_webui")
        assert daemon._client.headers["Authorization"] == "Bearer random-tok"
        await daemon.close()

        (tmp_path / ".atomcode" / "daemon-13456.json").unlink()
        daemon = AtomCodeDaemon(daemon_token="atomcode_webui")
        assert daemon._client.headers["Authorization"] == "Bearer atomcode_webui"
        await daemon.close()

    asyncio.run(run())


class AuthRotatingModelsClient(FakeClient):
    """鉴权头不是新 token 时 /models 返回 401，换头后返回 200。"""

    def __init__(self):
        super().__init__()
        self.headers = {"Authorization": "Bearer atomcode_webui"}
        self.get_calls = 0

    async def get(self, path, **kwargs):
        self.get_calls += 1
        if path != "/models":
            return FakeResponse()
        if self.headers.get("Authorization") != "Bearer new-random-token":
            return FakeResponse(status_code=401)
        return FakeResponse(payload=[{"provider": "AtomGit-deepseek-v4-flash"}])


def test_list_models_refreshes_token_on_401(monkeypatch):
    """daemon 重启换 token 后 401：重读 token 文件刷新鉴权头并重试成功。"""

    async def run():
        # 构造时不读真实文件，保持初始静态 token
        monkeypatch.setattr(daemon_module, "_read_dynamic_daemon_token", lambda base_url: None)
        daemon = AtomCodeDaemon()
        client = AuthRotatingModelsClient()
        daemon._client = client
        # 模拟 daemon 已重启，token 文件里是新 token
        monkeypatch.setattr(daemon_module, "_read_dynamic_daemon_token", lambda base_url: "new-random-token")

        models = await daemon.list_models()

        assert models == [{"provider": "AtomGit-deepseek-v4-flash"}]
        assert client.headers["Authorization"] == "Bearer new-random-token"
        assert client.get_calls == 2  # 首次 401 + 刷新后重试一次

    asyncio.run(run())


def test_list_models_401_surfaces_when_token_file_unchanged(monkeypatch):
    """token 文件未变化（重读无新值）时 401 按原语义上抛，不做无限重试。"""

    async def run():
        monkeypatch.setattr(daemon_module, "_read_dynamic_daemon_token", lambda base_url: None)
        daemon = AtomCodeDaemon()
        client = AuthRotatingModelsClient()
        daemon._client = client

        try:
            await daemon.list_models()
        except daemon_module.AtomCodeDaemonError as exc:
            assert "401" in str(exc)
        else:
            raise AssertionError("token 未刷新时应保留 401 错误")
        assert client.get_calls == 1

    asyncio.run(run())


class FakeStreamResponse:
    def __init__(self, status_code, lines=()):
        self.status_code = status_code
        self._lines = list(lines)

    async def aread(self):
        return b"unauthorized"

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class AuthRotatingChatClient(FakeClient):
    """鉴权头不是新 token 时 /chat 流返回 401，换头后返回正常 SSE。"""

    def __init__(self):
        super().__init__()
        self.headers = {"Authorization": "Bearer atomcode_webui"}
        self.stream_calls = 0

    async def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        if path == "/sessions":
            return FakeResponse(payload={"session_id": "session-1"})
        return FakeResponse()

    def stream(self, method, path, **kwargs):
        self.stream_calls += 1
        if self.headers.get("Authorization") != "Bearer new-random-token":
            return FakeStreamCtx(FakeStreamResponse(401))
        return FakeStreamCtx(
            FakeStreamResponse(200, lines=['data: {"type": "done", "stop_reason": "stopped"}'])
        )


def test_chat_stream_refreshes_token_on_401_and_retries(monkeypatch):
    """chat 流式请求 401 时刷新鉴权头重试一次，事件正常返回。"""

    async def run():
        monkeypatch.setattr(daemon_module, "_read_dynamic_daemon_token", lambda base_url: None)
        daemon = AtomCodeDaemon()
        client = AuthRotatingChatClient()
        daemon._client = client
        monkeypatch.setattr(daemon_module, "_read_dynamic_daemon_token", lambda base_url: "new-random-token")

        events = []
        async for ev in daemon.chat_with_session(
            "hi", working_dir="F:/workspace", conversation_key="conversation-1"
        ):
            events.append(ev)

        assert any(e.get("type") == "done" for e in events)
        assert client.stream_calls == 2
        assert client.headers["Authorization"] == "Bearer new-random-token"

    asyncio.run(run())
