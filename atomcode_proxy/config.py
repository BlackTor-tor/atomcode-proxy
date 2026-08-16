"""适配代理配置：环境变量驱动，全部带默认值。

加载优先级：已存在的环境变量 > 用户配置文件 atomcode-proxy-config.json > .env 文件 > 内置默认值。
程序永不会自动生成/写入 .env（仅 --init-config 手动生成模板）；
网页设置页保存的配置持久化到用户目录下的 atomcode-proxy-config.json。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .workdir import normalize_working_directory

log = logging.getLogger("atomcode_proxy.config")

# 项目根目录 = 本文件所在目录的上一级
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 内置 .env 模板：供 --init-config 手动生成时使用
DEFAULT_ENV_TEMPLATE = """\
# atomcode-proxy 配置文件（可选，所有项均有内置默认值）
# 仅在需要自定义时取消注释
#ATOMCODE_PROXY_HOST=127.0.0.1
#ATOMCODE_PROXY_PORT=8765
#ATOMCODE_DAEMON_URL=http://127.0.0.1:13456
#ATOMCODE_DAEMON_TOKEN=atomcode_webui
#ATOMCODE_DEFAULT_PROVIDER=AtomGit-deepseek-v4-flash
#ATOMCODE_APPROVAL_MODE=bypass
#ATOMCODE_PROXY_WORKDIR=
#ATOMCODE_MODEL_ALIAS=
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


# ---------------------------------------------------------------------------
# 用户配置文件（atomcode-proxy-config.json）：网页设置页保存的持久化存储
# ---------------------------------------------------------------------------

# 允许持久化的配置键（与设置页字段一致）
_USER_CONFIG_KEYS = (
    "ATOMCODE_PROXY_HOST",
    "ATOMCODE_PROXY_PORT",
    "ATOMCODE_DAEMON_URL",
    "ATOMCODE_DAEMON_TOKEN",
    "ATOMCODE_DEFAULT_PROVIDER",
    "ATOMCODE_APPROVAL_MODE",
    "ATOMCODE_PROXY_WORKDIR",
    "ATOMCODE_MODEL_ALIAS",
)


def user_config_path() -> Path:
    """用户配置文件路径：Windows 取 %APPDATA%\\atomcode-proxy\\atomcode-proxy-config.json，
    其他平台取 ~/.config/atomcode-proxy/atomcode-proxy-config.json。exe 旁不生成任何文件。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "atomcode-proxy" / "atomcode-proxy-config.json"


def read_user_config() -> dict[str, str]:
    """读取用户配置文件，返回 {key: value}；文件不存在或损坏时返回空字典。"""
    path = user_config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("用户配置文件读取失败，忽略: %s (%s)", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("用户配置文件格式异常（非对象），忽略: %s", path)
        return {}
    return {k: str(v) for k, v in data.items() if k in _USER_CONFIG_KEYS}


def write_user_config(updates: dict[str, str]) -> Path:
    """把 {key: value} 合并写入用户配置文件；空值表示删除该项。返回文件路径。"""
    merged = read_user_config()
    for key, val in updates.items():
        if key not in _USER_CONFIG_KEYS:
            continue
        if val:
            merged[key] = val
        else:
            merged.pop(key, None)
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# 启动时加载一次用户配置（网页保存值），供 Config 解析优先于 .env/默认值
_user_config = read_user_config()


def _cfg_value(key: str, default: str) -> str:
    """取值优先级：系统环境变量 > atomcode-proxy-config.json（网页保存）> 内置默认值。

    .env 的值在模块加载时已并入 os.environ（不覆盖已有变量），因此环境变量分支已涵盖 .env。
    """
    return os.environ.get(key) or _user_config.get(key) or default


def _resolve_working_dir() -> str:
    """读取显式工作目录（环境变量 > atomcode-proxy-config.json）；未配置时返回空值，
    由启动流程回退到用户主目录。"""
    raw = os.environ.get("ATOMCODE_PROXY_WORKDIR") or _user_config.get("ATOMCODE_PROXY_WORKDIR", "")
    return raw.strip()


def _resolve_workdir_roots() -> list[str]:
    """解析可选的允许根目录列表（ATOMCODE_WORKDIR_ROOTS，逗号分隔）。

    仅保留已存在的绝对目录；配置后请求级工作目录必须位于任一允许根内。
    """
    raw = os.environ.get("ATOMCODE_WORKDIR_ROOTS", "")
    roots: list[str] = []
    for item in raw.split(","):
        normalized = normalize_working_directory(item)
        if normalized:
            roots.append(normalized)
        elif item.strip():
            log.warning("ATOMCODE_WORKDIR_ROOTS 中的目录不存在，已忽略: %s", item.strip())
    return roots


@dataclass
class Config:
    """运行时可变的配置对象：网页保存的热更新直接改字段即可。"""
    host: str = field(default_factory=lambda: _cfg_value("ATOMCODE_PROXY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_cfg_value("ATOMCODE_PROXY_PORT", "8765")))

    daemon_url: str = field(default_factory=lambda: _cfg_value("ATOMCODE_DAEMON_URL", "http://127.0.0.1:13456"))
    daemon_token: str = field(default_factory=lambda: _cfg_value("ATOMCODE_DAEMON_TOKEN", "atomcode_webui"))
    default_provider: str = field(
        default_factory=lambda: _cfg_value("ATOMCODE_DEFAULT_PROVIDER", "AtomGit-deepseek-v4-flash")
    )
    approval_mode: str = field(default_factory=lambda: _cfg_value("ATOMCODE_APPROVAL_MODE", "bypass"))
    working_dir: str = field(default_factory=_resolve_working_dir)

    # 请求级工作目录允许根（安全围栏）：空列表表示不限制
    workdir_roots: list[str] = field(default_factory=_resolve_workdir_roots)

    # OpenAI 模型别名：逗号分隔的 k=v 列表，如 "claude-3-5-sonnet=AtomGit-deepseek-v4-flash"
    model_alias: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        aliases: dict[str, str] = {}
        raw = os.environ.get("ATOMCODE_MODEL_ALIAS") or _user_config.get("ATOMCODE_MODEL_ALIAS", "")
        for pair in raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k and v:
                    aliases[k.strip()] = v.strip()
        cfg = cls()
        cfg.model_alias = aliases
        return cfg

    def resolve_provider(self, model: str | None) -> str:
        """把上游请求的 model 名解析为 daemon 的 provider 名。"""
        if not model:
            return self.default_provider
        if model in self.model_alias:
            return self.model_alias[model]
        # 直接透传（daemon 侧认识自己的 provider 名）
        return model

    @property
    def daemon_port(self) -> int:
        """从 daemon_url 解析端口号。"""
        from urllib.parse import urlparse
        parsed = urlparse(self.daemon_url)
        return parsed.port or 13456
