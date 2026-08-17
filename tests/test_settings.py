"""设置页与基础端点的回归测试。

覆盖：
- /settings 保存：热更新 + 持久化到用户配置文件
- GET /settings 与 GET / 的来源校验（防局域网读取）
- 设置页不回显 daemon Token 明文
- /v1/models 与 /health 端点冒烟
- 非法 JSON 请求返回 400 而非 500
"""
import asyncio
import json

import httpx

import atomcode_proxy.config as config_module
from atomcode_proxy.app import create_app
from atomcode_proxy.config import Config
from tests.test_adapters import FakeDaemon


class ModelsDaemon(FakeDaemon):
    """带模型列表能力的 fake daemon。"""

    async def list_models(self):
        return [{"provider": "provider-a"}, {"provider": "provider-b"}]


def test_settings_save_hot_updates_and_persists(tmp_path, monkeypatch):
    async def run():
        cfg_path = tmp_path / "user-config" / "atomcode-proxy-config.json"
        monkeypatch.setattr(config_module, "user_config_path", lambda: cfg_path)

        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.post(
                "/settings",
                data={
                    "ATOMCODE_DEFAULT_PROVIDER": "MyProvider",
                    "ATOMCODE_APPROVAL_MODE": "plan",
                    "ATOMCODE_PROXY_PORT": "9000",
                    "ATOMCODE_DAEMON_TOKEN": "",
                    "ATOMCODE_PROXY_WORKDIR": "",
                },
            )

        assert response.status_code == 200
        # 运行时热更新立即生效
        assert app.state.config.default_provider == "MyProvider"
        assert app.state.config.approval_mode == "plan"
        # 持久化写入用户配置文件
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved.get("ATOMCODE_DEFAULT_PROVIDER") == "MyProvider"
        assert saved.get("ATOMCODE_APPROVAL_MODE") == "plan"

    asyncio.run(run())


def test_settings_page_never_echoes_daemon_token(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path), daemon_token="super-secret-token"))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.get("/settings")

        assert response.status_code == 200
        assert "super-secret-token" not in response.text

    asyncio.run(run())


def test_settings_page_rejects_foreign_host(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            evil = await client.get("/settings", headers={"host": "evil.example.com:8765"})
            local = await client.get("/settings", headers={"host": "127.0.0.1:8765"})

        assert evil.status_code == 403
        assert local.status_code == 200

    asyncio.run(run())


def test_status_page_rejects_foreign_host(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            evil = await client.get("/", headers={"host": "evil.example.com:8765"})
            local = await client.get("/")

        assert evil.status_code == 403
        assert local.status_code == 200

    asyncio.run(run())


def test_models_and_health_endpoints(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = ModelsDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            models = await client.get("/v1/models")
            health = await client.get("/health")

        assert models.status_code == 200
        ids = [m["id"] for m in models.json()["data"]]
        assert ids == ["provider-a", "provider-b"]
        assert health.status_code == 200
        assert health.json()["ok"] is True

    asyncio.run(run())


def test_chat_completions_rejects_malformed_json(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bad = await client.post(
                "/v1/chat/completions",
                content="{not json",
                headers={"Content-Type": "application/json"},
            )
            array = await client.post(
                "/v1/chat/completions",
                json=["array", "not", "object"],
            )

        assert bad.status_code == 400
        assert array.status_code == 400

    asyncio.run(run())
