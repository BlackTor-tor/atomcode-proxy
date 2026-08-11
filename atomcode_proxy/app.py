"""FastAPI 应用装配：把 OpenAI/Anthropic 适配器挂到 /v1/* 路径。"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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

    return app