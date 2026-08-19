import asyncio

import httpx

from atomcode_proxy.app import create_app
from atomcode_proxy.config import Config
from atomcode_proxy.daemon import AtomCodeDaemon


class FakeDaemon:
    def __init__(self):
        self.calls = []

    async def resolve_provider(self, model, aliases=None, default_provider=None):
        return default_provider or "test-provider"

    async def chat_with_session(self, message, **kwargs):
        self.calls.append((message, kwargs))
        yield {"type": "text", "content": "ok"}
        yield {"type": "tokens", "prompt": 3, "completion": 1}
        yield {"type": "done", "stop_reason": "stopped"}


class ErrorDaemon(FakeDaemon):
    """模拟 daemon 返回 error 事件（如 Provider not found）。"""

    async def chat_with_session(self, message, **kwargs):
        self.calls.append((message, kwargs))
        yield {"type": "error", "message": "Provider 'x' not found"}


class PrematureEofDaemon(AtomCodeDaemon):
    """输出部分文本后提前 EOF，用于验证异常流不会伪装成 completed。"""

    def __init__(self):
        super().__init__()
        self.stopped_sessions = []

    async def resolve_provider(self, model, aliases=None, default_provider=None):
        return model or default_provider or "test-provider"

    async def _create_session(self, working_dir):
        return "session-premature-eof"

    async def _chat_stream_raw(self, *args, **kwargs):
        yield {"type": "text", "content": "partial"}

    async def stop_chat(self, session_id):
        self.stopped_sessions.append(session_id)


def test_openai_adapter_passes_request_directory_and_history(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = FakeDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"X-Conversation-ID": "cursor-1", "X-Working-Directory": str(tmp_path)},
                json={
                    "model": "test-model",
                    "messages": [
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "first"},
                        {"role": "assistant", "content": "answer"},
                        {"role": "user", "content": "second"},
                    ],
                },
            )

        assert response.status_code == 200
        assert daemon.calls[0][0] == "second"
        assert daemon.calls[0][1]["working_dir"] == str(tmp_path.resolve())
        assert daemon.calls[0][1]["conversation_key"].endswith(":cursor-1")
        assert daemon.calls[0][1]["history"] == [
            {"role": "user", "content": "[system] system"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
        ]

    asyncio.run(run())


def test_anthropic_adapter_uses_default_directory_for_clients_without_override(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = FakeDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/messages",
                headers={"User-Agent": "claude-code-test"},
                json={
                    "model": "test-model",
                    "system": "system from top level",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        assert response.status_code == 200
        assert daemon.calls[0][1]["working_dir"] == str(tmp_path.resolve())
        assert daemon.calls[0][1]["history"] == [
            {"role": "user", "content": "[system] system from top level"}
        ]

    asyncio.run(run())


def test_openai_adapter_surfaces_daemon_error_event_as_502(tmp_path):
    """daemon 返回 error 事件时应浮出为 502，而非静默空响应。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = ErrorDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 502
        assert "Provider 'x' not found" in response.json()["error"]["message"]

    asyncio.run(run())


def test_anthropic_adapter_surfaces_daemon_error_event_as_502(tmp_path):
    """daemon 返回 error 事件时应浮出为 502，而非静默空响应。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = ErrorDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/messages",
                json={"model": "x", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 502
        assert "Provider 'x' not found" in response.json()["error"]["message"]

    asyncio.run(run())


def test_responses_endpoint_non_stream_returns_output_text(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = FakeDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "x", "input": "hello"},
            )

        assert response.status_code == 200
        obj = response.json()
        assert obj["object"] == "response"
        assert obj["status"] == "completed"
        assert obj["output"][0]["type"] == "message"
        assert obj["output"][0]["content"][0]["type"] == "output_text"
        assert obj["output"][0]["content"][0]["text"] == "ok"
        # prompt 取自 input，无历史
        assert daemon.calls[0][0] == "hello"

    asyncio.run(run())


def test_responses_endpoint_translates_input_items_and_instructions(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = FakeDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/responses",
                json={
                    "model": "x",
                    "instructions": "be helpful",
                    "input": [
                        {"type": "message", "role": "user", "content": "first question"},
                        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
                        {"type": "function_call", "name": "run", "arguments": "{}", "call_id": "c1"},
                        {"type": "function_call_output", "call_id": "c1", "output": "result data"},
                        {"type": "reasoning", "summary": []},
                        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second question"}]},
                    ],
                },
            )

        assert response.status_code == 200
        message, kwargs = daemon.calls[0]
        assert message == "second question"
        assert kwargs["history"] == [
            {"role": "user", "content": "[system] be helpful"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
            {"role": "assistant", "content": "[function_call] run {}"},
            {"role": "user", "content": "[function_call_output] result data"},
        ]

    asyncio.run(run())


def test_responses_endpoint_stream_emits_codex_event_sequence(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = FakeDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "x", "input": "hello", "stream": True},
            )

        assert response.status_code == 200
        body = response.text
        assert "event: response.created" in body
        assert "event: response.output_item_added" in body
        assert "event: response.output_text.delta" in body
        assert '"delta": "ok"' in body
        assert "event: response.output_text.done" in body
        assert "event: response.completed" in body
        assert '"status": "completed"' in body

    asyncio.run(run())


def test_responses_stream_reports_premature_eof_as_failed(tmp_path):
    """daemon 提前 EOF 时 Responses 流必须失败，不能把部分文本标记为完成。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = PrematureEofDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/responses",
                    json={"model": "x", "input": "hello", "stream": True},
                )
        finally:
            await daemon._client.aclose()

        body = response.text
        assert response.status_code == 200
        assert '"delta": "partial"' in body
        assert "event: response.failed" in body
        assert "ended before terminal event" in body
        assert "event: response.completed" not in body
        assert daemon.stopped_sessions == ["session-premature-eof"]

    asyncio.run(run())


def test_responses_endpoint_surfaces_daemon_error_event_as_502(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = ErrorDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "x", "input": "hello"},
            )

        assert response.status_code == 502
        assert "Provider 'x' not found" in response.json()["error"]["message"]

    asyncio.run(run())


def test_chat_completions_stream_emits_frame_sequence_and_optional_usage(tmp_path):
    """流式帧序列契约：首块 role 声明 -> 文本切块 -> finish_reason ->
    （include_usage 时）usage 收尾 chunk -> [DONE]。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = FakeDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "x",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert response.status_code == 200
        body = response.text
        first_line = body.split("\n", 1)[0]
        assert '"delta": {"role": "assistant"' in first_line
        assert '"content": "ok"' in body
        i_finish = body.index('"finish_reason": "stop"')
        i_usage = body.index('"choices": []')
        i_done = body.index("data: [DONE]")
        assert i_finish < i_usage < i_done
        # usage 来自 daemon 的 tokens 事件，而非估算
        assert '"prompt_tokens": 3' in body
        assert '"completion_tokens": 1' in body

    asyncio.run(run())


def test_chat_completions_stream_omits_usage_without_stream_options(tmp_path):
    """未请求 include_usage 时不发 usage chunk（保持与官方默认行为一致）。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 200
        assert '"choices": []' not in response.text

    asyncio.run(run())


def test_chat_completions_stream_surfaces_daemon_error_as_text(tmp_path):
    """流式请求遇到 daemon error 事件：以 [proxy error] 文本浮出并正常收尾。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        daemon = ErrorDaemon()
        app.state.daemon = daemon
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 200
        assert "[proxy error]" in response.text
        assert "Provider 'x' not found" in response.text
        assert response.text.endswith("data: [DONE]\n\n")

    asyncio.run(run())
