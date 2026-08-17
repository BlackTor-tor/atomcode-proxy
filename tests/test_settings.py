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
import re
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


def test_settings_save_keeps_persisted_provider_and_daemon_url_when_left_blank(tmp_path, monkeypatch):
    """Provider / Daemon 地址留空 = 保持不变：不得从配置文件删除已存值。"""

    async def run():
        cfg_path = tmp_path / "user-config" / "atomcode-proxy-config.json"
        monkeypatch.setattr(config_module, "user_config_path", lambda: cfg_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps(
                {
                    "ATOMCODE_DEFAULT_PROVIDER": "MyProvider",
                    "ATOMCODE_DAEMON_URL": "http://192.168.1.5:13456",
                }
            ),
            encoding="utf-8",
        )

        app = create_app(
            Config(
                working_dir=str(tmp_path),
                default_provider="MyProvider",
                daemon_url="http://192.168.1.5:13456",
            )
        )
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.post(
                "/settings",
                data={
                    "ATOMCODE_DEFAULT_PROVIDER": "",
                    "ATOMCODE_DAEMON_URL": "",
                },
            )

        assert response.status_code == 200
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved.get("ATOMCODE_DEFAULT_PROVIDER") == "MyProvider"
        assert saved.get("ATOMCODE_DAEMON_URL") == "http://192.168.1.5:13456"
        # 运行时值也保持不变
        assert app.state.config.default_provider == "MyProvider"
        assert app.state.config.daemon_url == "http://192.168.1.5:13456"

    asyncio.run(run())


def test_settings_save_reports_invalid_model_alias_parts(tmp_path, monkeypatch):
    """非法别名片段不得静默丢弃：保存后应向用户反馈被忽略的片段。"""

    async def run():
        cfg_path = tmp_path / "user-config" / "atomcode-proxy-config.json"
        monkeypatch.setattr(config_module, "user_config_path", lambda: cfg_path)

        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.post(
                "/settings",
                data={"ATOMCODE_MODEL_ALIAS": "gpt-4o AtomGit-x,ok=AtomGit-y"},
            )

        assert response.status_code == 200
        assert "格式无效已忽略" in response.text
        assert "gpt-4o AtomGit-x" in response.text
        assert app.state.config.model_alias == {"ok": "AtomGit-y"}

    asyncio.run(run())


def test_settings_save_immediate_daemon_switch_swaps_client_before_close(tmp_path, monkeypatch):
    """立即切换路径：新客户端先生效再关旧客户端，新请求不落在已关闭客户端上。"""

    async def run():
        cfg_path = tmp_path / "user-config" / "atomcode-proxy-config.json"
        monkeypatch.setattr(config_module, "user_config_path", lambda: cfg_path)

        app = create_app(Config(working_dir=str(tmp_path)))
        from atomcode_proxy.daemon import AtomCodeDaemon

        old_client = AtomCodeDaemon("http://127.0.0.1:13456")
        app.state.daemon = old_client
        closed = []

        async def record_close():
            # 记录关闭时刻 app.state.daemon 是否已换新：若仍指向旧客户端，
            # close 窗口内的新请求会踩到已关闭的 httpx 客户端
            closed.append(app.state.daemon is not old_client)
            await asyncio.sleep(0)

        old_client.close = record_close
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.post(
                "/settings",
                data={"ATOMCODE_DAEMON_URL": "http://127.0.0.1:23456"},
            )

        assert response.status_code == 200
        assert closed == [True]
        assert app.state.daemon.base_url == "http://127.0.0.1:23456"
        await app.state.daemon.close()

    asyncio.run(run())


def test_chat_endpoints_reject_non_list_messages(tmp_path):
    """messages 非消息对象列表 / input 非法类型：返回 400 而非 500。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = FakeDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            openai_str = await client.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": "hello"},
            )
            openai_mixed = await client.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "hi"}, "bare"]},
            )
            anthropic_dict = await client.post(
                "/v1/messages",
                json={"model": "x", "messages": {"a": 1}},
            )
            responses_int = await client.post(
                "/v1/responses",
                json={"model": "x", "input": 123},
            )

        assert openai_str.status_code == 400
        assert openai_mixed.status_code == 400
        assert anthropic_dict.status_code == 400
        assert responses_int.status_code == 400

    asyncio.run(run())


def test_settings_page_script_blocks_are_balanced(tmp_path):
    """script 块内不得出现可截断/双重转义脚本块的序列（含 JS 注释与注入值）。

    v0.1.17 回归：S4 修复的注释里写了字面闭合标签序列，HTML 解析器在 raw text
    模式下遇到即闭合脚本块，导致 Provider 下拉加载逻辑失效。
    v0.1.18 后加固：注入值内的小于号全部转义为反斜杠u003c，彻底消除闭合标签、
    注释起始、开标签等一切风险序列。"""

    async def run():
        # 恶意/异常 provider 名模拟最坏情况注入
        evil = 'AtomGit-x"</script><script>alert(1)//'
        app = create_app(Config(working_dir=str(tmp_path), default_provider=evil))
        app.state.daemon = ModelsDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.get("/settings")

        assert response.status_code == 200
        text = response.text
        # 脚本块开闭标签数量必须一致：不一致说明块内泄露了闭合序列
        assert text.count("<script>") == text.count("</script>")
        # 注入值内不得残留任何尖括号（彻底防截断/双重转义）
        assert 'var currentVal = "AtomGit-x\\"\\u003c/script>\\u003cscript>alert(1)//";' in text

    asyncio.run(run())


def test_status_page_script_blocks_are_balanced(tmp_path):
    """状态页同样校验：script/style 块内无截断序列，开闭计数一致。"""

    async def run():
        app = create_app(Config(working_dir=str(tmp_path)))
        app.state.daemon = ModelsDaemon()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            response = await client.get("/")

        assert response.status_code == 200
        text = response.text
        assert text.count("<script>") == text.count("</script>")
        assert text.count("<style>") == text.count("</style>")
        for block in re.finditer(r"<script>(.*?)</script>", text, re.S):
            assert "</script" not in block.group(1)

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
