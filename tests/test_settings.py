"""设置页与基础端点的回归测试。

覆盖：
- /settings 保存：热更新 + 持久化到用户配置文件
- GET /settings 与 GET / 的来源校验（防局域网读取）
- 设置页不回显 daemon Token 明文
- Token 留空保存不得删除已持久化的值
- 非法监听地址被拒绝且不持久化
- 监听 80 端口时 Origin 裸形式放行
- 目录选择器超时返回结构化 504
- /v1/models 与 /health 端点冒烟
- 非法 JSON 请求返回 400 而非 500
"""
import asyncio
import json
import time

import httpx

import atomcode_proxy.app as app_module
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


def test_settings_save_keeps_persisted_token_when_left_blank(tmp_path, monkeypatch):
    """UI 承诺 Token 留空 = 保持不变：任意一次保存不得从配置文件删除已存 token。"""

    async def run():
        cfg_path = tmp_path / "user-config" / "atomcode-proxy-config.json"
        monkeypatch.setattr(config_module, "user_config_path", lambda: cfg_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps({"ATOMCODE_DAEMON_TOKEN": "my-secret-token"}), encoding="utf-8"
        )

        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            # 仅修改 Provider 保存，token 按占位符提示留空
            response = await client.post(
                "/settings",
                data={
                    "ATOMCODE_DEFAULT_PROVIDER": "MyProvider",
                    "ATOMCODE_DAEMON_TOKEN": "",
                },
            )

        assert response.status_code == 200
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved.get("ATOMCODE_DAEMON_TOKEN") == "my-secret-token"
        assert saved.get("ATOMCODE_DEFAULT_PROVIDER") == "MyProvider"

    asyncio.run(run())


def test_settings_save_rejects_invalid_host(tmp_path, monkeypatch):
    """带协议前缀等非法监听地址：提示无效且不持久化（否则重启 bind 失败退出）。"""

    async def run():
        cfg_path = tmp_path / "user-config" / "atomcode-proxy-config.json"
        monkeypatch.setattr(config_module, "user_config_path", lambda: cfg_path)

        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        original_host = app.state.config.host
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.post(
                "/settings",
                data={"ATOMCODE_PROXY_HOST": "http://0.0.0.0"},
            )

        assert response.status_code == 200
        assert "监听地址无效" in response.text
        assert app.state.config.host == original_host
        if cfg_path.exists():
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert "ATOMCODE_PROXY_HOST" not in saved

    asyncio.run(run())


def test_settings_api_allows_origin_without_port_on_default_http_port(tmp_path):
    """监听 80 时浏览器 Origin 省略 :80，本机写操作不得被误拒为跨站请求。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path), port=80))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            local = await client.post(
                "/api/validate-dir",
                headers={"origin": "http://127.0.0.1"},
                json={"path": str(tmp_path)},
            )
            evil = await client.post(
                "/api/validate-dir",
                headers={"origin": "http://evil.example.com"},
                json={"path": str(tmp_path)},
            )

        assert local.status_code == 200
        assert evil.status_code == 403

    asyncio.run(run())


def test_choose_working_dir_timeout_returns_structured_504(tmp_path, monkeypatch):
    """目录选择器超时：返回结构化 504 JSON，而非未捕获的 500 纯文本。"""

    async def run():
        def blocking_choose(initial):
            time.sleep(1)
            return None

        monkeypatch.setattr(app_module, "choose_working_directory", blocking_choose)
        monkeypatch.setattr(app_module, "_CHOOSE_DIR_TIMEOUT", 0.05)
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.post("/api/choose-working-dir", json={})

        assert response.status_code == 504
        assert response.json()["selected"] is False
        assert "超时" in response.json()["error"]

    asyncio.run(run())


def test_models_and_health_endpoints(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = ModelsDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            models = await client.get("/v1/models")
            api_models = await client.get("/api/models")
            health = await client.get("/health")

        assert models.status_code == 200
        ids = [m["id"] for m in models.json()["data"]]
        assert ids == ["provider-a", "provider-b"]
        # /api/models 回显 daemon 连接信息，纳入与设置页相同的来源校验
        assert api_models.status_code == 200
        assert api_models.json()["providers"] == ["provider-a", "provider-b"]
        assert health.status_code == 200
        assert health.json()["ok"] is True

    asyncio.run(run())


def test_api_models_rejects_foreign_host(tmp_path):
    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = ModelsDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            evil = await client.get("/api/models", headers={"host": "evil.example.com:8765"})
            local = await client.get("/api/models")

        assert evil.status_code == 403
        assert local.status_code == 200

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
