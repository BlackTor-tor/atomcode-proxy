"""python -m atomcode_proxy"""
from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app
from .config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> None:
    cfg = Config.from_env()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()