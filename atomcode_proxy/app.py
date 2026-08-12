"""FastAPI 应用装配：把 OpenAI/Anthropic 适配器挂到 /v1/* 路径。"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .config import Config
from .daemon import AtomCodeDaemon
from . import anthropic_adapter, openai_adapter

log = logging.getLogger("atomcode_proxy")


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        daemon = AtomCodeDaemon(
            cfg.daemon_url,
            daemon_token=cfg.daemon_token,
            default_provider=cfg.default_provider,
            approval_mode=cfg.approval_mode,
            default_working_dir=cfg.working_dir,
        )
        app.state.daemon = daemon
        app.state.config = cfg
        log.info("daemon client ready: %s", cfg.daemon_url)
        yield
        await daemon.close()

    app = FastAPI(title="atomcode-proxy", version=__version__, lifespan=lifespan)
    app.include_router(openai_adapter.router)
    app.include_router(anthropic_adapter.router)
    app.state.config = cfg

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "daemon": cfg.daemon_url, "provider": cfg.default_provider}

    @app.get("/version")
    async def version() -> dict:
        return {"name": "atomcode-proxy", "version": __version__, "frozen": getattr(sys, "frozen", False)}

    @app.get("/", response_class=HTMLResponse)
    async def status_page() -> str:
        """返回 HTML 状态页面。"""
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>atomcode-proxy</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            max-width: 800px;
            margin: 50px auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .card {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{ color: #333; }}
        .status {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 14px;
        }}
        .status.running {{
            background: #d4edda;
            color: #155724;
        }}
        .info {{ color: #666; }}
        code {{
            background: #f8f9fa;
            padding: 2px 6px;
            border-radius: 3px;
        }}
    </style>
</head>
<body>
    <h1>atomcode-proxy</h1>
    <div class="card">
        <h2>服务状态</h2>
        <p>状态: <span class="status running">运行中</span></p>
        <p>版本: {__version__}</p>
        <p>监听地址: <code>http://{cfg.host}:{cfg.port}</code></p>
    </div>
    <div class="card">
        <h2>Daemon 连接</h2>
        <p>地址: <code>{cfg.daemon_url}</code></p>
        <p>Provider: <code>{cfg.default_provider}</code></p>
    </div>
    <div class="card">
        <h2>客户端接入</h2>
        <p>OpenAI 兼容: <code>http://{cfg.host}:{cfg.port}/v1</code></p>
        <p>Anthropic 兼容: <code>http://{cfg.host}:{cfg.port}</code></p>
        <p>API Key: 任意非空值</p>
    </div>
</body>
</html>"""

    return app