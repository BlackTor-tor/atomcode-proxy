"""python -m atomcode_proxy"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .config import Config, DEFAULT_ENV_TEMPLATE, _default_env_path
from .tray import SystemTray
from .workdir import normalize_working_directory

log = logging.getLogger("atomcode_proxy.main")


# ---------------------------------------------------------------------------
# 端口预检
# ---------------------------------------------------------------------------

def _check_port_available(host: str, port: int) -> bool:
    """端口占用预检：bind 成功后立即 close 释放。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _is_port_listening(host: str, port: int) -> bool:
    """检测端口是否已在监听（可连接即返回 True）。"""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (OSError, ConnectionRefusedError):
        return False


# ---------------------------------------------------------------------------
# daemon 健康检查
# ---------------------------------------------------------------------------

def _is_daemon_running(daemon_url: str, timeout: float = 1.0) -> bool:
    """尝试连接 daemon，判断是否已在运行。"""
    try:
        # daemon 没有专门的 health 端点，用极短超时尝试连接端口
        from urllib.parse import urlparse
        parsed = urlparse(daemon_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 13456
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


# ---------------------------------------------------------------------------
# daemon 路径查找
# ---------------------------------------------------------------------------

def _find_daemon_executable() -> Path | None:
    """按优先级查找 atomcode.exe。

    查找顺序：
    1. 环境变量 ATOMCODE_DAEMON_PATH
    2. %LOCALAPPDATA%\\AtomCode\\atomcode.exe（标准安装位置）
    3. C:\\Program Files\\AtomCode\\atomcode.exe
    """
    # 1. 环境变量
    env_path = os.environ.get("ATOMCODE_DAEMON_PATH")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        log.warning("ATOMCODE_DAEMON_PATH 指向的文件不存在: %s", p)

    # 2. 标准安装位置：%LOCALAPPDATA%\AtomCode\atomcode.exe
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        p = Path(local_app_data) / "AtomCode" / "atomcode.exe"
        if p.is_file():
            return p

    # 3. Program Files
    p = Path(r"C:\Program Files\AtomCode\atomcode.exe")
    if p.is_file():
        return p

    return None


# ---------------------------------------------------------------------------
# daemon 进程管理
# ---------------------------------------------------------------------------

def _start_daemon(daemon_exe: Path, port: int) -> subprocess.Popen | None:
    """启动 daemon 进程，返回 Popen 对象；失败返回 None。"""
    try:
        proc = subprocess.Popen(
            [str(daemon_exe), "daemon", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        # 等待一小段时间确认进程没有立即退出
        time.sleep(0.5)
        if proc.poll() is not None:
            log.warning("daemon 进程启动后立即退出 (exit code: %s)", proc.returncode)
            return None
        return proc
    except Exception as exc:
        log.warning("启动 daemon 失败: %s", exc)
        return None


def _stop_daemon(proc: subprocess.Popen | None) -> None:
    """终止由我们启动的 daemon 进程。"""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            log.info("正在关闭 daemon 进程 (PID: %d)...", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            log.info("daemon 进程已关闭")
    except Exception as exc:
        log.warning("关闭 daemon 时出错: %s", exc)


# ---------------------------------------------------------------------------
# daemon 看门狗
# ---------------------------------------------------------------------------

def _daemon_watchdog(
    cfg: Config,
    daemon_exe: Path | None,
    daemon_proc_holder: list[subprocess.Popen | None],
    stop_event: threading.Event,
) -> None:
    """后台看门狗线程：每 60 秒检测 daemon 是否存活，失效则自动重启。

    daemon_proc_holder: 单元素列表，用于在闭包中可变引用 daemon 进程对象。
    stop_event: 代理退出时 set，看门狗随之退出。
    """
    log.info("daemon 看门狗已启动（检测间隔 60 秒）")
    while not stop_event.is_set():
        # 等待 60 秒，期间若 stop_event 被 set 则立即退出
        if stop_event.wait(timeout=60):
            break
        # 检测 daemon 是否仍在运行
        if _is_daemon_running(cfg.daemon_url):
            continue
        # daemon 失效，尝试重启
        log.warning("检测到 daemon 失效，正在重启...")
        exe = daemon_exe or _find_daemon_executable()
        if exe is None:
            log.warning("未找到 atomcode.exe，无法重启 daemon")
            continue
        new_proc = _start_daemon(exe, cfg.daemon_port)
        if new_proc is not None:
            log.info("daemon 已重启 (PID: %d)", new_proc.pid)
            daemon_proc_holder[0] = new_proc
            # 等待新 daemon 就绪
            time.sleep(2.0)
        else:
            log.warning("daemon 重启失败，将在下次检测时重试")
    log.info("daemon 看门狗已停止")


# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

def _get_log_directory() -> Path:
    """获取日志目录：冻结模式取 exe 同级目录，开发模式取项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def _setup_logging(cfg: Config) -> str:
    """配置日志：冻结模式只输出到文件，开发模式同时输出到控制台和文件。

    返回日志文件路径。
    """
    log_dir = _get_log_directory()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "atomcode-proxy.log"

    # 根日志配置
    root_logger = logging.getLogger("atomcode_proxy")
    root_logger.setLevel(logging.INFO)

    # 文件处理器：1MB x 3 备份
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=1024 * 1024,  # 1MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger.addHandler(file_handler)

    # 开发模式：同时输出到控制台
    if not getattr(sys, "frozen", False):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root_logger.addHandler(console_handler)

    return str(log_path)


# ---------------------------------------------------------------------------
# --init-config 命令
# ---------------------------------------------------------------------------

def _init_config() -> None:
    """生成默认 .env 配置文件供用户自定义。"""
    env_path = _default_env_path()
    if env_path.exists():
        print(f"配置文件已存在: {env_path}")
        print("如需重新生成，请先删除该文件。")
        return
    env_path.write_text(DEFAULT_ENV_TEMPLATE, encoding="utf-8")
    print(f"已生成配置文件模板: {env_path}")
    print("所有配置项均有内置默认值，无需修改即可直接运行。")
    print("如需自定义，取消注释对应行并修改值即可。")


def _ensure_working_directory(cfg: Config) -> None:
    """确认工作目录：已配置（含 config.json 保存值）则校验后使用；
    未配置时回退到用户主目录，不弹窗阻塞启动，可在设置页修改。"""
    selected = normalize_working_directory(cfg.working_dir)
    if selected:
        cfg.working_dir = selected
        return
    cfg.working_dir = str(Path.home())
    log.info("未配置工作目录，默认使用用户主目录: %s（可在设置页修改）", cfg.working_dir)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    # --init-config: 仅生成配置文件后退出
    if len(sys.argv) > 1 and sys.argv[1] == "--init-config":
        _init_config()
        return

    cfg = Config.from_env()

    _ensure_working_directory(cfg)

    # --- 配置日志 ---
    log_path = _setup_logging(cfg)
    log.info("atomcode-proxy v%s 启动中...", __version__)

    # --- 端口预检 ---
    if not _check_port_available(cfg.host, cfg.port):
        log.error("端口 %s 已被占用，无法启动", cfg.port)
        print(f"[错误] 端口 {cfg.port} 已被占用，无法启动。")
        print("可在设置页面（或系统环境变量）更换监听端口后重启。")
        if not getattr(sys, "frozen", False):
            input("按回车键退出...")
        sys.exit(1)

    # --- 自动启动 daemon ---
    daemon_proc: subprocess.Popen | None = None
    daemon_status_msg = ""

    if _is_daemon_running(cfg.daemon_url):
        daemon_status_msg = "检测到 AtomCode daemon 已在运行"
        log.info(daemon_status_msg)
    else:
        daemon_exe = _find_daemon_executable()
        if daemon_exe is not None:
            log.info("正在启动 AtomCode daemon: %s", daemon_exe)
            daemon_proc = _start_daemon(daemon_exe, cfg.daemon_port)
            if daemon_proc is not None:
                daemon_status_msg = f"已自动启动 AtomCode daemon (PID: {daemon_proc.pid})"
                log.info(daemon_status_msg)
                # 等待 daemon 就绪
                time.sleep(1.0)
            else:
                daemon_status_msg = "AtomCode daemon 启动失败，代理将继续运行（请确认 daemon 已手动启动）"
                log.warning(daemon_status_msg)
        else:
            daemon_status_msg = "未找到 AtomCode daemon，代理将继续运行（请确认 daemon 已手动启动）"
            log.warning("未找到 atomcode.exe，可通过 ATOMCODE_DAEMON_PATH 环境变量指定路径")

    # --- 启动 daemon 看门狗（后台线程）---
    watchdog_stop = threading.Event()
    # 用列表持有 daemon 进程引用，以便看门狗重启时更新
    daemon_proc_holder: list[subprocess.Popen | None] = [daemon_proc]
    # 记住 daemon 可执行文件路径，避免看门狗重复查找
    daemon_exe_cached = _find_daemon_executable() if daemon_proc is None else None
    watchdog_thread = threading.Thread(
        target=_daemon_watchdog,
        args=(cfg, daemon_exe_cached, daemon_proc_holder, watchdog_stop),
        daemon=True,
    )
    watchdog_thread.start()

    # --- 启动 FastAPI 服务（后台线程）---
    app = create_app(cfg)
    server_error: list[Exception | None] = [None]

    def _run_server() -> None:
        try:
            uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")
        except Exception as exc:
            server_error[0] = exc
            log.error("uvicorn 启动失败: %s", exc, exc_info=True)

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    # 等待服务就绪（最多 10 秒）
    server_ready = False
    for _ in range(40):
        time.sleep(0.25)
        if server_error[0] is not None:
            break
        if _is_port_listening(cfg.host, cfg.port):
            server_ready = True
            break

    if server_error[0] is not None:
        log.error("服务启动失败: %s", server_error[0])
        print(f"[错误] 服务启动失败: {server_error[0]}")
        print("请检查日志获取详细信息。")
        if not getattr(sys, "frozen", False):
            input("按回车键退出...")
        sys.exit(1)

    if not server_ready:
        log.error("服务启动超时（等待 10 秒后仍无法连接）")
        print(f"[错误] 服务启动超时，无法监听 {cfg.host}:{cfg.port}")
        if not getattr(sys, "frozen", False):
            input("按回车键退出...")
        sys.exit(1)

    log.info("服务已就绪: http://%s:%s", cfg.host, cfg.port)

    # --- 自动打开浏览器 ---
    status_url = f"http://{cfg.host}:{cfg.port}/"
    log.info("自动打开状态页面: %s", status_url)
    webbrowser.open(status_url)

    # --- 启动系统托盘（阻塞主线程）---
    def on_stop():
        """托盘退出回调。"""
        log.info("正在停止服务...")
        watchdog_stop.set()
        watchdog_thread.join(timeout=5)
        _stop_daemon(daemon_proc_holder[0])
        log.info("服务已停止")

    tray = SystemTray(cfg.host, cfg.port, log_path)
    tray.run(callback_on_stop=on_stop)


if __name__ == "__main__":
    main()
