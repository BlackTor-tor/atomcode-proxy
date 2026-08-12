"""适配代理配置：环境变量驱动，全部带默认值。

加载优先级：已存在的环境变量 > 项目根目录 .env 文件 > 内置默认值。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("atomcode_proxy.config")

# 项目根目录 = 本文件所在目录的上一级
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 内置 .env 模板：首次运行且 .env 不存在时自动写入
DEFAULT_ENV_TEMPLATE = """\
# 复制为 .env 后按需修改
ATOMCODE_PROXY_HOST=127.0.0.1
ATOMCODE_PROXY_PORT=8765
ATOMCODE_DAEMON_URL=http://127.0.0.1:13456
ATOMCODE_DAEMON_TOKEN=atomcode_webui
ATOMCODE_DEFAULT_PROVIDER=AtomGit-deepseek-v4-flash
ATOMCODE_APPROVAL_MODE=bypass
ATOMCODE_PROXY_WORKDIR=
ATOMCODE_MODEL_ALIAS=
"""


def _default_env_path() -> Path:
    """默认 .env 路径：冻结态(PyInstaller)取 exe 旁；否则取项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return _PROJECT_ROOT / ".env"


def _load_dotenv(path: Path | None = None) -> None:
    """轻量 .env 加载：不覆盖已存在的环境变量。

    仅支持 KEY=VALUE 行、# 注释、双引号包裹的值；不处理变量展开。
    路径优先级：环境变量 ATOMCODE_PROXY_ENV > _default_env_path()。
    """
    override = os.environ.get("ATOMCODE_PROXY_ENV")
    if path is None:
        path = Path(override) if override else _default_env_path()
    if not path.is_file():
        log.info("未找到 .env，使用默认配置")
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    log.info(".env 加载完成: %s", path)


_load_dotenv()


def _resolve_working_dir() -> str:
    """默认工作目录：优先取用户主目录，避免 daemon 落在奇怪 cwd。"""
    return os.environ.get("ATOMCODE_PROXY_WORKDIR") or os.path.expanduser("~")


@dataclass(frozen=True)
class Config:
    host: str = field(default_factory=lambda: os.environ.get("ATOMCODE_PROXY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("ATOMCODE_PROXY_PORT", "8765")))

    daemon_url: str = field(default_factory=lambda: os.environ.get("ATOMCODE_DAEMON_URL", "http://127.0.0.1:13456"))
    daemon_token: str = field(default_factory=lambda: os.environ.get("ATOMCODE_DAEMON_TOKEN", "atomcode_webui"))
    default_provider: str = field(
        default_factory=lambda: os.environ.get("ATOMCODE_DEFAULT_PROVIDER", "AtomGit-deepseek-v4-flash")
    )
    approval_mode: str = field(default_factory=lambda: os.environ.get("ATOMCODE_APPROVAL_MODE", "bypass"))
    working_dir: str = field(default_factory=_resolve_working_dir)

    # OpenAI 模型别名：逗号分隔的 k=v 列表，如 "claude-3-5-sonnet=AtomGit-deepseek-v4-flash"
    model_alias: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        aliases: dict[str, str] = {}
        raw = os.environ.get("ATOMCODE_MODEL_ALIAS", "")
        for pair in raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k and v:
                    aliases[k.strip()] = v.strip()
        cfg = cls()
        object.__setattr__(cfg, "model_alias", aliases)
        return cfg

    def resolve_provider(self, model: str | None) -> str:
        """把上游请求的 model 名解析为 daemon 的 provider 名。"""
        if not model:
            return self.default_provider
        if model in self.model_alias:
            return self.model_alias[model]
        # 直接透传（daemon 侧认识自己的 provider 名）
        return model