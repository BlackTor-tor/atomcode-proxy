import asyncio

import httpx

from atomcode_proxy.app import create_app
from atomcode_proxy.config import Config


class FakeDaemon:
    def __init__(self):
        self.calls = []

    async def resolve_provider(self, model, aliases=None, default_provider=None):
        return default_provider or "test-provider"

    async def chat_with_session(self, message, **kwargs):
        self.calls.append((message, kwargs))
        yield {"type": "text", "content": "ok"}
        yield {"type": "done", "stop_reason": "stopped"}


class ErrorDaemon(FakeDaemon):
    """模拟 daemon 返回 error 事件（如 Provider not found）。"""

    async def chat_with_session(self, message, **kwargs):
        self.calls.append((message, kwargs))
        yield {"type": "error", "message": "Provider 'x' not found"}


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
