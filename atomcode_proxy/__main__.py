"""python -m atomcode_proxy"""
from __future__ import annotations

import logging
import socket
import sys

import uvicorn

from . import __version__
from .app import create_app
from .config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _check_port_available(host: str, port: int) -> bool:
    """端口占用预检：bind 成功后立即 close 释放。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main() -> None:
    cfg = Config.from_env()

    print("=" * 52)
    print(f"atomcode-proxy v{__version__}")
    print(f"监听地址: http://{cfg.host}:{cfg.port}")
    print("=" * 52)

    if not _check_port_available(cfg.host, cfg.port):
        print(f"[错误] 端口 {cfg.port} 已被占用，无法启动。")
        print("可在 .env 中设置 ATOMCODE_PROXY_PORT 更换端口。")
        input("按回车键退出...")
        sys.exit(1)

    try:
        app = create_app(cfg)
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"[错误] 启动失败: {exc}")
        input("按回车键退出...")
        sys.exit(1)


if __name__ == "__main__":
    main()
