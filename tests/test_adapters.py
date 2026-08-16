import asyncio

import httpx

from atomcode_proxy.app import create_app
from atomcode_proxy.config import Config


class FakeDaemon:
    def __init__(self):
        self.calls = []

    async def chat_with_session(self, message, **kwargs):
        self.calls.append((message, kwargs))
        yield {"type": "text", "content": "ok"}
        yield {"type": "done", "stop_reason": "stopped"}


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
            {"role": "system", "content": "system"},
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
        assert daemon.calls[0][1]["history"] == [{"role": "system", "content": "system from top level"}]

    asyncio.run(run())
