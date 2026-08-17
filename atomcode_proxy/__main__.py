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
from .config import Config, DEFAULT_ENV_TEMPLATE, _default_env_path, _dotenv_values
from .tray import SystemTray
from .workdir import normalize_working_directory

log = logging.getLogger("atomcode_proxy.main")


# ---------------------------------------------------------------------------
# 端口预检
# ---------------------------------------------------------------------------

def _probe_host(host: str) -> str:
    """通配监听地址（0.0.0.0/::）下，就绪探测改用回环地址。

    Windows 上 socket.create_connection(("0.0.0.0", port)) 会抛
    WinError 10049（WSAEADDRNOTAVAIL），直接探测通配地址必失败。
    """
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def _check_port_available(host: str, port: int) -> bool:
    """端口占用预检：bind 成功后立即 close 释放。

    按 host 字面量选择地址族：IPv6 地址用 AF_INET6，否则 AF_INET，
    避免 IPv6 监听（如 ::）被 AF_INET bind 误判为端口占用。
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
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

def _is_daemon_running(daemon_url: str, timeout: float = 1.5) -> bool:
    """向 daemon 发一次真实 HTTP 请求判断是否在运行。

    仅 TCP 可连不足以证明端口上是 daemon（可能被其他程序占用）；
    任何 HTTP 响应（含 404/401 等错误状态）都证明端口上是 HTTP 服务。
    """
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    parsed = urlparse(daemon_url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 13456
    try:
        with urllib.request.urlopen(f"{scheme}://{host}:{port}/models", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        # 收到 HTTP 错误响应也说明是 HTTP 服务在监听
        return True
    except (OSError, ValueError):
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
    # 1. 环境变量（含 .env 解析值）
    env_path = os.environ.get("ATOMCODE_DAEMON_PATH") or _dotenv_values.get("ATOMCODE_DAEMON_PATH", "")
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
    daemon_proc_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """后台看门狗线程：每 60 秒检测 daemon 是否存活，失效则自动重启。

    daemon_proc_holder: 单元素列表，用于在闭包中可变引用 daemon 进程对象；
    与主线程的读写均需持有 daemon_proc_lock，避免退出时漏停被替换的进程。
    stop_event: 代理退出时 set，看门狗随之退出。
    """
    log.info("daemon 看门狗已启动（检测间隔 60 秒）")
    while not stop_event.is_set():
        # 等待 60 秒，期间若 stop_event 被 set 则立即退出
        if stop_event.wait(timeout=60):
            break
        # 代理服务自身存活检查：uvicorn 运行期意外崩溃时端口会关闭，
        # 仅记录告警日志（托盘已无法恢复，至少留下排查线索）
        if not _is_port_listening(_probe_host(cfg.host), cfg.port):
            log.error("代理服务端口 %s:%s 无响应：HTTP 服务可能已崩溃，请检查日志或重启", cfg.host, cfg.port)
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
            with daemon_proc_lock:
                old_proc = daemon_proc_holder[0]
                daemon_proc_holder[0] = new_proc
            # 锁外停止旧进程：避免长时间持锁阻塞主线程退出路径
            _stop_daemon(old_proc)
            # 等待新 daemon 就绪；stop_event 响应式等待，退出时立即返回
            stop_event.wait(2.0)
        else:
            log.warning("daemon 重启失败，将在下次检测时重试")
    log.info("daemon 看门狗已停止")


# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

def _get_log_directory() -> Path:
    """获取日志目录：冻结模式取 %APPDATA%\\atomcode-proxy\\logs（exe 旁不生成文件，
    且 exe 可能部署在 Program Files 等不可写目录），开发模式取项目根目录。"""
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "atomcode-proxy" / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def _setup_logging(cfg: Config) -> str:
    """配置日志：冻结模式只输出到文件，开发模式同时输出到控制台和文件。

    日志目录不可写时降级（仅告警不崩溃），避免无写权限目录下
    冻结 exe 静默退出。返回日志文件路径。
    """
    log_dir = _get_log_directory()
    log_path = log_dir / "atomcode-proxy.log"

    # 根日志配置
    root_logger = logging.getLogger("atomcode_proxy")
    root_logger.setLevel(logging.INFO)

    # 文件处理器：1MB x 3 备份
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=1024 * 1024,  # 1MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root_logger.addHandler(file_handler)
    except OSError as exc:
        log.warning("日志文件不可写，跳过文件日志: %s", exc)

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
    """生成默认 .env 配置文件供用户自定义。

    冻结 exe（console=False）下 stdout 重定向到 devnull，print 无可见输出：
    提示写入日志文件，并用资源管理器打开 .env 所在目录作为反馈。
    """
    env_path = _default_env_path()
    try:
        if env_path.exists():
            lines = [f"配置文件已存在: {env_path}", "如需重新生成，请先删除该文件。"]
        else:
            env_path.write_text(DEFAULT_ENV_TEMPLATE, encoding="utf-8")
            lines = [
                f"已生成配置文件模板: {env_path}",
                "所有配置项均有内置默认值，无需修改即可直接运行。",
                "如需自定义，取消注释对应行并修改值即可。",
            ]
    except OSError as exc:
        lines = [f"生成配置文件失败: {exc}（当前目录可能不可写）"]
    if getattr(sys, "frozen", False):
        try:
            log_dir = _get_log_directory()
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "atomcode-proxy.log", "a", encoding="utf-8") as f:
                f.write("\n".join(f"[init-config] {ln}" for ln in lines) + "\n")
            if sys.platform == "win32":
                os.startfile(str(env_path.parent))
        except OSError as exc:
            log.warning("写入 init-config 日志失败: %s", exc)
        return
    for ln in lines:
        print(ln)


def _ensure_working_directory(cfg: Config) -> None:
    """确认工作目录：已配置（含 atomcode-proxy-config.json 保存值）则校验后使用；
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
    # 用列表持有 daemon 进程引用，以便看门狗重启时更新；
    # 与看门狗线程的读写用锁同步，避免退出时漏停被替换的进程
    daemon_proc_lock = threading.Lock()
    daemon_proc_holder: list[subprocess.Popen | None] = [daemon_proc]
    # 记住 daemon 可执行文件路径，避免看门狗重复查找
    daemon_exe_cached = _find_daemon_executable() if daemon_proc is None else None
    watchdog_thread = threading.Thread(
        target=_daemon_watchdog,
        args=(cfg, daemon_exe_cached, daemon_proc_holder, daemon_proc_lock, watchdog_stop),
        daemon=True,
    )
    watchdog_thread.start()

    # --- 启动 FastAPI 服务（后台线程）---
    app = create_app(cfg)
    server_error: list[Exception | None] = [None]
    # 用 uvicorn.Server 而非 uvicorn.run：保留 server 引用供退出时
    # 设置 should_exit 优雅关闭，触发 lifespan 清理（停止 daemon 会话等）。
    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning")
    )

    def _run_server() -> None:
        try:
            server.run()
        except Exception as exc:
            server_error[0] = exc
            log.error("uvicorn 启动失败: %s", exc, exc_info=True)

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    # 等待服务就绪（最多 10 秒）
    server_ready = False
    probe_host = _probe_host(cfg.host)
    for _ in range(40):
        time.sleep(0.25)
        if server_error[0] is not None:
            break
        if _is_port_listening(probe_host, cfg.port):
            server_ready = True
            break

    def _cleanup_on_startup_failure() -> None:
        """启动失败路径统一清理：停止看门狗并终止已拉起的 daemon，避免孤儿进程。"""
        watchdog_stop.set()
        watchdog_thread.join(timeout=5)
        with daemon_proc_lock:
            _stop_daemon(daemon_proc_holder[0])
            daemon_proc_holder[0] = None

    if server_error[0] is not None:
        log.error("服务启动失败: %s", server_error[0])
        print(f"[错误] 服务启动失败: {server_error[0]}")
        print("请检查日志获取详细信息。")
        _cleanup_on_startup_failure()
        if not getattr(sys, "frozen", False):
            input("按回车键退出...")
        sys.exit(1)

    if not server_ready:
        log.error("服务启动超时（等待 10 秒后仍无法连接）")
        print(f"[错误] 服务启动超时，无法监听 {cfg.host}:{cfg.port}")
        _cleanup_on_startup_failure()
        if not getattr(sys, "frozen", False):
            input("按回车键退出...")
        sys.exit(1)

    log.info("服务已就绪: http://%s:%s", cfg.host, cfg.port)

    # --- 注册 daemon 配置热更新回调 ---
    # 设置页修改 ATOMCODE_DAEMON_URL 后，_apply_settings 会通过该回调同步
    # 主进程侧的进程生命周期状态：停掉我们拉起的旧 daemon、按新配置拉起，
    # 避免看门狗下一轮用新 URL 判定旧 daemon 失效后另起进程，导致旧进程
    # 被 holder 覆盖而泄漏为孤儿。
    def _on_daemon_config_changed(new_url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(new_url)
        host = (parsed.hostname or "127.0.0.1").lower()
        port = parsed.port or 13456
        with daemon_proc_lock:
            old_proc = daemon_proc_holder[0]
            daemon_proc_holder[0] = None
        _stop_daemon(old_proc)
        if host in ("127.0.0.1", "localhost", "::1"):
            exe = _find_daemon_executable()
            if exe is not None and not _is_daemon_running(new_url):
                new_proc = _start_daemon(exe, port)
                if new_proc is not None:
                    with daemon_proc_lock:
                        _stop_daemon(daemon_proc_holder[0])
                        daemon_proc_holder[0] = new_proc
                    log.info("daemon 已按新配置拉起 (PID: %d)", new_proc.pid)

    app.state.on_daemon_config_changed = _on_daemon_config_changed

    # --- 自动打开浏览器 ---
    status_url = f"http://{cfg.host}:{cfg.port}/"
    log.info("自动打开状态页面: %s", status_url)
    webbrowser.open(status_url)

    # --- 启动系统托盘（阻塞主线程）---
    def on_stop():
        """托盘退出回调。"""
        log.info("正在停止服务...")
        # 先优雅关闭 HTTP 服务：触发 lifespan 清理（停止 daemon 会话、关闭 httpx 连接）
        server.should_exit = True
        server_thread.join(timeout=10)
        watchdog_stop.set()
        watchdog_thread.join(timeout=5)
        if watchdog_thread.is_alive():
            # 看门狗可能正处于重启路径（spawn 新进程后写 holder 的窗口），
            # 再等一轮确保 holder 已写入最终值，避免漏停刚拉起的进程。
            watchdog_thread.join(timeout=5)
        with daemon_proc_lock:
            _stop_daemon(daemon_proc_holder[0])
            daemon_proc_holder[0] = None
        log.info("服务已停止")

    tray = SystemTray(cfg.host, cfg.port, log_path)
    tray.run(callback_on_stop=on_stop)


if __name__ == "__main__":
    main()
