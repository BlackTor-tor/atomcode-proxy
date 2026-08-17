"""FastAPI 应用装配：把 OpenAI/Anthropic 适配器挂到 /v1/* 路径。"""
from __future__ import annotations

import base64
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import httpx

from . import __version__
from .config import Config, environ_or_dotenv, read_user_config, write_user_config
from .conversation import ConversationKeyResolver
from .daemon import AtomCodeDaemon
from . import anthropic_adapter, openai_adapter
from .updater import (
    GITHUB_RELEASES_URL,
    check_for_update,
)
from .workdir import choose_working_directory, normalize_working_directory

log = logging.getLogger("atomcode_proxy")

# 目录选择器的等待上限：人工交互场景，过短会在用户暂时离开时误报失败
_CHOOSE_DIR_TIMEOUT = 300


def _get_logo_base64() -> str:
    """读取 Logo 文件并转为 base64。"""
    logo_paths: list[Path] = []
    if getattr(sys, "frozen", False):
        logo_paths.append(Path(sys.executable).resolve().parent / "assets" / "logo.png")
    logo_paths.append(Path(__file__).resolve().parent.parent / "assets" / "logo.png")
    for path in logo_paths:
        if path.exists():
            return base64.b64encode(path.read_bytes()).decode()
    return ""


def _is_local_source(request: Request, cfg: Config) -> bool:
    """校验请求来源是否为本服务页面（防恶意网页/局域网主机跨站调用写操作）。

    恶意网页发起的跨站请求会携带其自身的 Origin/Referer，
    与本服务地址不匹配即拒绝；无来源头的直连请求（如 curl、IDE 客户端）放行。
    另校验 Host 头白名单：DNS rebinding 攻击下浏览器请求的 Host 是攻击者
    域名而非本机地址，且同源请求可无 Origin/Referer，仅靠来源头拦不住。
    """
    allowed = {
        f"http://{cfg.host}:{cfg.port}",
        f"http://127.0.0.1:{cfg.port}",
        f"http://localhost:{cfg.port}",
        f"http://[::1]:{cfg.port}",
    }
    if cfg.port == 80:
        # HTTP 默认端口下，浏览器 Origin/Referer 与 Host 一样按规范省略 :80，
        # 需同时放行裸 origin 形式，否则设置页的写操作会被误拒为跨站请求
        allowed.update({f"http://{cfg.host}", "http://127.0.0.1", "http://localhost", "http://[::1]"})
    allowed_hosts = {
        f"{cfg.host}:{cfg.port}".lower(),
        f"127.0.0.1:{cfg.port}",
        f"localhost:{cfg.port}",
        f"[::1]:{cfg.port}",
    }
    if cfg.port == 80:
        # HTTP 默认端口下，客户端 Host 头按规范省略 :80，需同时放行裸主机名形式
        allowed_hosts.update({cfg.host.lower(), "127.0.0.1", "localhost", "[::1]"})
    host = (request.headers.get("host") or "").strip().lower()
    if host and host not in allowed_hosts:
        return False
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    for value in (origin, referer):
        if value and not any(value == p or value.startswith(p + "/") for p in allowed):
            return False
    return True


def _escape_attr(val: str) -> str:
    """转义 HTML 属性值：防止值中的引号截断属性或注入标签结构。

    统一用于所有 value="..." 拼接场景（工作目录、password、text 等）。
    """
    return val.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _sanitize_download_filename(name: str) -> str:
    """净化下载文件名：去除引号/反斜杠/换行等会破坏 HTTP 头的字符。"""
    cleaned = "".join(c if c.isprintable() and c not in '"\\\r\n' else "_" for c in name)
    return cleaned.strip() or "update.exe"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()

    # daemon 切换互斥：延迟切换后台任务与设置页立即切换串行化，避免
    # 并发重建客户端、并发触发主进程侧进程启停造成 cfg/客户端/进程错位
    daemon_switch_lock = asyncio.Lock()

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
        # 先取消延迟切换后台任务：避免关闭窗口内它再新建无人关闭的客户端
        # （httpx 泄漏），或其 handler 在退出路径上再次拉起/终止 daemon 进程
        switch_task = getattr(app.state, "daemon_switch_task", None)
        if switch_task is not None and not switch_task.done():
            switch_task.cancel()
            try:
                await switch_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.warning("等待 daemon 切换任务退出失败: %s", exc)
        # 关闭当前实际生效的 daemon 客户端：设置页热切换后 app.state.daemon
        # 已是 新 实例，仅关启动实例会漏掉对外部托管 daemon 的会话清理
        current = getattr(app.state, "daemon", daemon)
        if current is not daemon:
            try:
                await current.close()
            except Exception as exc:
                log.warning("关闭当前 daemon 客户端失败: %s", exc)
        await daemon.close()

    app = FastAPI(title="atomcode-proxy", version=__version__, lifespan=lifespan)
    app.include_router(openai_adapter.router)
    app.include_router(anthropic_adapter.router)
    app.state.config = cfg
    app.state.conversation_resolver = ConversationKeyResolver()

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "daemon": cfg.daemon_url,
            "provider": cfg.default_provider,
            "working_dir": cfg.working_dir,
        }

    @app.get("/version")
    async def version() -> dict:
        return {"name": "atomcode-proxy", "version": __version__, "frozen": getattr(sys, "frozen", False)}

    # ── 检查更新 API ─────────────────────────────────────────────

    @app.get("/api/update/check")
    async def api_check_update() -> JSONResponse:
        """查询 GitHub Releases 是否有新版本。"""
        try:
            result = await check_for_update(__version__)
            if result is None:
                return JSONResponse({"has_update": False, "current_version": __version__})
            return JSONResponse({"has_update": True, **result})
        except Exception as e:
            log.warning("检查更新失败: %s", e)
            return JSONResponse(status_code=502, content={"error": str(e)})

    @app.post("/api/update/download")
    async def api_download_update(request: Request) -> Response:
        """代理下载最新版本 exe：后端流式转发 GitHub 字节流，由浏览器
        保存到其默认下载路径（不写服务端本地文件）。

        仅接受来自本服务页面的 POST 请求，防止恶意网页/局域网主机
        通过跨站请求触发下载。
        """
        if not _is_local_source(request, cfg):
            log.warning("拒绝来自非本服务来源的下载请求")
            return JSONResponse(status_code=403, content={"error": "禁止的来源"})

        try:
            update_info = await check_for_update(__version__)
        except Exception as e:
            log.warning("检查更新失败: %s", e)
            return JSONResponse(status_code=502, content={"error": str(e)})
        if update_info is None:
            return JSONResponse(status_code=404, content={"error": "无可用更新"})

        # 优先使用 GitHub API 返回的权威 release 页链接，避免拼接 tag 格式漂移导致 404
        fallback_url = update_info.get("release_url") or (
            f"{GITHUB_RELEASES_URL}/tag/v{update_info.get('latest_version', '')}"
        )
        download_url = update_info.get("download_url", "")
        if not download_url:
            return JSONResponse(
                status_code=502,
                content={"error": "无可用下载链接", "fallback_url": fallback_url},
            )

        filename = (
            update_info.get("asset_name")
            or download_url.rsplit("/", 1)[-1]
            or f"atomcode-proxy-{update_info.get('latest_version', 'unknown')}-windows-x64.exe"
        )

        # 从 GitHub 流式拉取并转发给浏览器
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=30.0),
            follow_redirects=True,
        )
        try:
            resp = await client.send(
                client.build_request("GET", download_url, headers={"User-Agent": "atomcode-proxy-updater"}),
                stream=True,
            )
        except Exception as exc:
            await client.aclose()
            log.warning("连接 GitHub 下载源失败: %s", exc)
            return JSONResponse(
                status_code=502,
                content={"error": "下载失败", "fallback_url": fallback_url},
            )
        if resp.status_code >= 400:
            await resp.aclose()
            await client.aclose()
            log.warning("GitHub 下载源返回 %s", resp.status_code)
            return JSONResponse(
                status_code=502,
                content={"error": f"下载源返回 {resp.status_code}", "fallback_url": fallback_url},
            )

        async def _stream():
            """转发 GitHub 字节流；结束后关闭上游连接。"""
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        safe_filename = _sanitize_download_filename(filename)
        headers = {
            # attachment 提示浏览器保存为文件；X-Filename 供前端读取文件名。
            # filename* 保留原始非 ASCII 文件名，filename 为净化后的 ASCII 兼容回退。
            "Content-Disposition": (
                f'attachment; filename="{safe_filename.encode("ascii", "replace").decode("ascii")}"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
            # 响应头按 latin-1 编码，非 ASCII 文件名会导致构造响应时抛 UnicodeEncodeError
            "X-Filename": safe_filename.encode("ascii", "replace").decode("ascii"),
        }
        content_length = resp.headers.get("content-length", "")
        if content_length:
            headers["Content-Length"] = content_length
        log.info("开始代理下载: %s (%s bytes)", download_url, content_length or "unknown")
        return StreamingResponse(
            _stream(),
            media_type="application/octet-stream",
            headers=headers,
        )

    # ── 模型列表 API ─────────────────────────────────────────────

    @app.get("/api/models")
    async def api_list_models(request: Request) -> JSONResponse:
        """返回可用模型列表（从 daemon 获取）。

        同样校验请求来源：完整 provider/模型名单属于 daemon 连接信息，
        host=0.0.0.0 监听时不能让局域网任意主机枚举。
        """
        if not _is_local_source(request, cfg):
            return JSONResponse(status_code=403, content={"error": "禁止的来源"})
        daemon: AtomCodeDaemon = app.state.daemon
        try:
            models = await daemon.list_models()
            providers = sorted({m.get("provider", "") for m in models if m.get("provider")})
            return JSONResponse({"providers": providers, "models": models})
        except Exception as e:
            log.warning("获取模型列表失败: %s", e)
            return JSONResponse(status_code=502, content={"error": str(e)})

    @app.post("/api/choose-working-dir")
    async def api_choose_working_dir(request: Request) -> JSONResponse:
        """打开本机目录选择器，供设置页切换代理默认工作目录。

        请求体可选传 {"current": "..."} 作为选择器初始目录（输入框当前值）。
        """
        if not _is_local_source(request, cfg):
            return JSONResponse(status_code=403, content={"error": "禁止的来源"})
        initial = cfg.working_dir
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("current"):
                initial = str(body["current"])
        except Exception:
            pass  # 无请求体或非 JSON 时使用默认初始目录
        # 超时必须捕获：未捕获的 TimeoutError 会落为 500 纯文本，前端无法解析
        try:
            selected = await asyncio.wait_for(
                asyncio.to_thread(choose_working_directory, initial),
                timeout=_CHOOSE_DIR_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("目录选择超时（%d 秒）", _CHOOSE_DIR_TIMEOUT)
            return JSONResponse(
                status_code=504,
                content={"selected": False, "error": "选择目录超时；若对话框仍在显示，请先关闭后再重试"},
            )
        if not selected:
            return JSONResponse({"selected": False, "working_dir": cfg.working_dir})
        return JSONResponse({"selected": True, "working_dir": selected})

    @app.post("/api/validate-dir")
    async def api_validate_dir(request: Request) -> JSONResponse:
        """校验工作目录路径是否存在，供设置页输入框即时反馈。"""
        if not _is_local_source(request, cfg):
            return JSONResponse(status_code=403, content={"error": "禁止的来源"})
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON"})
        raw = str(body.get("path", "")) if isinstance(body, dict) else ""
        normalized = normalize_working_directory(raw)
        return JSONResponse({"valid": bool(normalized), "normalized": normalized or ""})

    # ── 配置页面辅助函数 ──────────────────────────────────────────

    _SETTING_FIELDS = [
        ("ATOMCODE_PROXY_HOST", "监听地址", "text", None),
        ("ATOMCODE_PROXY_PORT", "监听端口", "text", None),
        ("ATOMCODE_DAEMON_URL", "Daemon 地址", "text", None),
        ("ATOMCODE_DAEMON_TOKEN", "Daemon Token", "password", None),
        ("ATOMCODE_DEFAULT_PROVIDER", "默认 Provider", "dynamic-select", None),
        ("ATOMCODE_APPROVAL_MODE", "审批模式", "select", ["bypass", "build", "plan", "accept_edits"]),
        ("ATOMCODE_PROXY_WORKDIR", "工作目录", "text", None),
        ("ATOMCODE_MODEL_ALIAS", "模型别名 (k=v,k2=v2)", "text", None),
    ]

    # 内置默认值：系统环境变量、atomcode-proxy-config.json 和 .env 均未覆盖时，设置页回退显示这些值。
    # 取自 cfg（已合并环境变量 / atomcode-proxy-config.json / .env / 内置默认），避免默认值重复定义。
    # 注意：这是 create_app 时的启动快照，运行时热更新不会刷新，仅作设置页回退显示用。
    _BUILTIN_DEFAULTS: dict[str, str] = {
        "ATOMCODE_PROXY_HOST": cfg.host,
        "ATOMCODE_PROXY_PORT": str(cfg.port),
        "ATOMCODE_DAEMON_URL": cfg.daemon_url,
        "ATOMCODE_DAEMON_TOKEN": cfg.daemon_token,
        "ATOMCODE_DEFAULT_PROVIDER": cfg.default_provider,
        "ATOMCODE_APPROVAL_MODE": cfg.approval_mode,
        "ATOMCODE_PROXY_WORKDIR": cfg.working_dir,
        "ATOMCODE_MODEL_ALIAS": "",
    }

    def _read_env_file() -> dict[str, str]:
        """读取用户已保存的配置（atomcode-proxy-config.json），返回 {key: value} 字典。"""
        return read_user_config()

    def _runtime_values() -> dict[str, str]:
        """运行时配置快照（网页热更新后的值，优先于持久化文件显示）。"""
        c = app.state.config
        return {
            "ATOMCODE_PROXY_HOST": c.host,
            "ATOMCODE_PROXY_PORT": str(c.port),
            "ATOMCODE_DAEMON_URL": c.daemon_url,
            "ATOMCODE_DAEMON_TOKEN": c.daemon_token,
            "ATOMCODE_DEFAULT_PROVIDER": c.default_provider,
            "ATOMCODE_APPROVAL_MODE": c.approval_mode,
            "ATOMCODE_PROXY_WORKDIR": c.working_dir,
            "ATOMCODE_MODEL_ALIAS": ",".join(f"{k}={v}" for k, v in c.model_alias.items()),
        }

    def _current_value(field_name: str, env_vals: dict[str, str], runtime: dict[str, str]) -> str:
        """获取字段当前值：优先运行时配置（网页热更新值），其次用户配置文件
        （atomcode-proxy-config.json），再环境变量（含 .env 并入值），最后内置默认值。
        模型别名允许运行时为空（可清空）。"""
        if field_name in runtime and (runtime[field_name] or field_name == "ATOMCODE_MODEL_ALIAS"):
            return runtime[field_name]
        if field_name in env_vals:
            return env_vals[field_name]
        val = environ_or_dotenv(field_name)
        if val:
            return val
        return _BUILTIN_DEFAULTS.get(field_name, "")

    def _settings_html(env_vals: dict[str, str], message: str = "", runtime: dict[str, str] | None = None) -> str:
        """生成设置页面 HTML。"""
        runtime = runtime or {}
        logo_b64 = _get_logo_base64()
        logo_html = ""
        if logo_b64:
            logo_html = f'<div style="text-align: center; margin-bottom: 20px;"><img src="data:image/png;base64,{logo_b64}" alt="atomcode-proxy" style="width: 80px; height: 80px;"></div>'

        msg_html = ""
        if message:
            safe_msg = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            msg_html = f'<div style="background:#d4edda;color:#155724;padding:12px 16px;border-radius:8px;margin-bottom:20px;">{safe_msg}</div>'

        fields_html = ""
        # 字段回显：token 不回显明文（防局域网主机读取/浏览器保存历史泄露），
        # 已设置时显示空值 + 占位提示，仅在用户输入新值时覆盖。
        for field_name, label, input_type, options in _SETTING_FIELDS:
            val = _current_value(field_name, env_vals, runtime)
            if field_name == "ATOMCODE_PROXY_WORKDIR":
                escaped_val = _escape_attr(val)
                input_html = f'''
                <div style="display:flex;gap:8px;align-items:center;">
                    <input type="text" name="{field_name}" id="working-dir-input" value="{escaped_val}"
                           placeholder="粘贴本机已存在的绝对路径，或点击右侧按钮选择"
                           style="flex:1;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;">
                    <button type="button" class="btn btn-secondary" id="choose-working-dir-btn">选择目录…</button>
                </div>
                <div id="working-dir-status" style="font-size:12px;color:#666;margin-top:5px;">
                    目录必须存在，且会用于此代理实例创建的 daemon 会话
                </div>
                <script>
                (function() {{
                    var button = document.getElementById("choose-working-dir-btn");
                    var input = document.getElementById("working-dir-input");
                    var status = document.getElementById("working-dir-status");
                    var validateTimer = null;

                    function showValidation(path) {{
                        if (!path) {{
                            status.textContent = "目录不能为空";
                            status.style.color = "#dc3545";
                            return;
                        }}
                        fetch("/api/validate-dir", {{
                            method: "POST",
                            headers: {{"Content-Type": "application/json"}},
                            body: JSON.stringify({{ path: path }})
                        }})
                            .then(function(r) {{ return r.json(); }})
                            .then(function(data) {{
                                if (data.valid) {{
                                    status.textContent = "✓ 目录有效：" + data.normalized;
                                    status.style.color = "#28a745";
                                }} else {{
                                    status.textContent = "✗ 目录不存在或不是本机绝对路径";
                                    status.style.color = "#dc3545";
                                }}
                            }})
                            .catch(function() {{
                                status.textContent = "无法校验目录";
                                status.style.color = "#dc3545";
                            }});
                    }}

                    // 输入停顿 500ms 后即时校验
                    input.addEventListener("input", function() {{
                        if (validateTimer) {{ clearTimeout(validateTimer); }}
                        validateTimer = setTimeout(function() {{ showValidation(input.value.trim()); }}, 500);
                    }});
                    if (input.value.trim()) {{ showValidation(input.value.trim()); }}

                    button.addEventListener("click", function() {{
                        button.disabled = true;
                        status.textContent = "请在弹出的窗口中选择目录…";
                        status.style.color = "#666";
                        fetch("/api/choose-working-dir", {{
                            method: "POST",
                            headers: {{"Content-Type": "application/json"}},
                            body: JSON.stringify({{ current: input.value.trim() }})
                        }})
                            .then(function(r) {{ return r.json(); }})
                            .then(function(data) {{
                                if (data.selected) {{
                                    input.value = data.working_dir;
                                    showValidation(data.working_dir);
                                }} else {{
                                    status.textContent = "已取消选择，目录未更改";
                                    status.style.color = "#666";
                                }}
                            }})
                            .catch(function(err) {{
                                status.textContent = "选择目录失败: " + err.message;
                                status.style.color = "#dc3545";
                            }})
                            .finally(function() {{ button.disabled = false; }});
                    }});
                }})();
                </script>
                '''
            elif input_type == "select" and options:
                opts = "".join(
                    f'<option value="{o}" {"selected" if val == o else ""}>{o}</option>'
                    for o in options
                )
                input_html = f'<select name="{field_name}" style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;">{opts}</select>'
            elif input_type == "dynamic-select":
                # 完整转义：值含 </script> 或引号时不能破坏属性/脚本块
                escaped_val = _escape_attr(val)
                input_html = f'''
                <div id="ds_{field_name}_wrap">
                    <input type="hidden" name="{field_name}" id="ds_{field_name}_hidden" value="{escaped_val}">
                    <select id="ds_{field_name}_select"
                            style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;margin-bottom:6px;">
                        <option value="">加载中...</option>
                    </select>
                    <input type="text" id="ds_{field_name}_text" value="{escaped_val}"
                           placeholder="或输入自定义值"
                           style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;display:none;">
                    <div id="ds_{field_name}_status" style="font-size:12px;color:#999;margin-top:2px;"></div>
                </div>
                <script>
                (function() {{
                    var fieldName = "{field_name}";
                    // script 块是 raw text，HTML 实体不会解码，需用 JSON 序列化注入；
                    // 值中的闭合标签序列需转义斜杠，防止提前截断 script 块
                    // （注意：本注释内也不得出现该序列，否则会同样截断脚本块）
                    var currentVal = {json.dumps(val).replace("</", "<\\/")};
                    var hiddenEl = document.getElementById("ds_" + fieldName + "_hidden");
                    var selectEl = document.getElementById("ds_" + fieldName + "_select");
                    var textEl = document.getElementById("ds_" + fieldName + "_text");
                    var statusEl = document.getElementById("ds_" + fieldName + "_status");

                    fetch("/api/models")
                        .then(function(r) {{
                            if (!r.ok) throw new Error("HTTP " + r.status);
                            return r.json();
                        }})
                        .then(function(data) {{
                            var providers = data.providers || [];
                            if (providers.length === 0) throw new Error("无可用 Provider");
                            selectEl.innerHTML = "";
                            providers.forEach(function(p) {{
                                var opt = document.createElement("option");
                                opt.value = p;
                                opt.textContent = p;
                                if (p === currentVal) opt.selected = true;
                                selectEl.appendChild(opt);
                            }});
                            statusEl.textContent = "已加载 " + providers.length + " 个 Provider";
                            statusEl.style.color = "#28a745";
                        }})
                        .catch(function(err) {{
                            selectEl.style.display = "none";
                            textEl.style.display = "block";
                            statusEl.textContent = "无法加载模型列表: " + err.message + "（已切换到手动输入）";
                            statusEl.style.color = "#dc3545";
                        }});

                    selectEl.addEventListener("change", function() {{
                        hiddenEl.value = selectEl.value;
                    }});
                    textEl.addEventListener("input", function() {{
                        hiddenEl.value = textEl.value;
                    }});
                }})();
                </script>
                '''
            elif input_type == "password":
                if val:
                    input_html = (
                        f'<input type="password" name="{field_name}" value="" '
                        f'placeholder="已设置，留空保持不变" autocomplete="new-password" '
                        f'style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;">'
                    )
                else:
                    input_html = (
                        f'<input type="password" name="{field_name}" value="" '
                        f'placeholder="未设置，使用内置默认值" autocomplete="new-password" '
                        f'style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;">'
                    )
            else:
                # host/port/daemon 地址等可留空字段：占位符明确“留空保持不变”语义，
                # 避免用户误以为清空可恢复默认（清空会静默保持当前值）
                blank_keeps = field_name in (
                    "ATOMCODE_PROXY_HOST",
                    "ATOMCODE_PROXY_PORT",
                    "ATOMCODE_DAEMON_URL",
                )
                placeholder = ' placeholder="留空保持不变"' if blank_keeps else ""
                input_html = f'<input type="text" name="{field_name}" value="{_escape_attr(val)}"{placeholder} style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;">'
            fields_html += f"""
            <div style="margin-bottom:16px;">
                <label style="display:block;font-weight:600;margin-bottom:4px;color:#444;">{label}</label>
                <div style="font-size:12px;color:#999;margin-bottom:4px;"><code>{field_name}</code></div>
                {input_html}
            </div>"""

        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>设置 - atomcode-proxy</title>
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
        h2 {{ margin-top: 0; }}
        code {{
            background: #f8f9fa;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 13px;
        }}
        .btn {{
            display: inline-block;
            padding: 10px 24px;
            background: #2563eb;
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 15px;
            cursor: pointer;
        }}
        .btn:hover {{ background: #1d4ed8; }}
        .btn-secondary {{
            background: #6b7280;
        }}
        .btn-secondary:hover {{ background: #4b5563; }}
        .nav-link {{
            display: inline-block;
            margin-top: 16px;
            color: #2563eb;
            text-decoration: none;
            font-size: 14px;
        }}
        .nav-link:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    {logo_html}
    <h1 style="text-align: center;">设置</h1>
    {msg_html}
    <form method="POST" action="/settings">
        <div class="card">
            <h2>服务配置</h2>
            {fields_html}
        </div>
        <div style="text-align:center; margin: 20px 0; font-size:13px;color:#666;">
            保存后立即生效并写入用户配置文件（重启后仍保留）；监听地址/端口需重启服务才能生效
            <br>
            <button type="submit" class="btn">保存配置</button>
        </div>
    </form>
    <div style="text-align:center;">
        <a href="/" class="nav-link">&larr; 返回状态页面</a>
    </div>
</body>
</html>"""

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        """返回设置页面 HTML 表单。

        与写操作一样校验请求来源：GET 也回显 daemon 连接信息，
        host=0.0.0.0 监听时不能让局域网任意主机读取。
        """
        if not _is_local_source(request, cfg):
            return JSONResponse(status_code=403, content={"error": "禁止的来源"})
        env_vals = _read_env_file()
        return _settings_html(env_vals, runtime=_runtime_values())

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request) -> Response:
        """接收表单数据：热更新内存配置并持久化到用户配置文件 atomcode-proxy-config.json。"""
        if not _is_local_source(request, cfg):
            return JSONResponse(status_code=403, content={"error": "禁止的来源"})
        form = await request.form()
        updates: dict[str, str] = {}
        for field_name, _, input_type, _ in _SETTING_FIELDS:
            val = form.get(field_name, "")
            if val is not None:
                updates[field_name] = str(val)

        messages = await _apply_settings(app, updates)
        return _settings_html(
            _read_env_file(),
            message="<br>".join(messages),
            runtime=_runtime_values(),
        )

    def _schedule_daemon_switch(app: FastAPI) -> None:
        """后台任务：等旧 daemon 客户端所有会话空闲后执行延迟的配置切换。

        设置页保存 daemon 地址/Token 时若有进行中的任务，立即 close 旧客户端
        会终止所有客户端正在执行的模型生成与工具执行；推迟到空闲后再切换。
        """
        task = getattr(app.state, "daemon_switch_task", None)
        if task is not None and not task.done():
            return

        async def _switch_when_idle() -> None:
            while True:
                old = getattr(app.state, "daemon", None)
                pending = getattr(app.state, "pending_daemon_switch", None)
                if not pending or not isinstance(old, AtomCodeDaemon):
                    app.state.pending_daemon_switch = None
                    return
                if any(s.lock.locked() for s in old._states.values()):
                    await asyncio.sleep(2)
                    continue
                async with daemon_switch_lock:
                    # 锁内重读并复核：等待期间设置页可能已完成另一次切换，
                    # 或旧客户端又开始了新会话（此时释放锁回循环头继续等）
                    old = getattr(app.state, "daemon", None)
                    pending = getattr(app.state, "pending_daemon_switch", None)
                    if not pending or not isinstance(old, AtomCodeDaemon):
                        return
                    if any(s.lock.locked() for s in old._states.values()):
                        continue
                    new_url, new_token = pending
                    # 先原子换上新客户端再关闭旧客户端：切换后新请求立即走新连接，
                    # 避免"判定空闲→关闭完成"期间新请求落在即将关闭的旧客户端上被掐断
                    app.state.daemon = AtomCodeDaemon(
                        new_url,
                        daemon_token=new_token,
                        default_provider=cfg.default_provider,
                        approval_mode=cfg.approval_mode,
                        default_working_dir=cfg.working_dir,
                    )
                    app.state.pending_daemon_switch = None
                    cfg.daemon_url = new_url
                    cfg.daemon_token = new_token
                    log.info("Daemon 连接已在任务结束后切换: %s", new_url)
                # 锁外收尾：关闭旧客户端与同步进程生命周期，不阻塞下一次切换
                try:
                    await old.close()
                except Exception as exc:
                    log.warning("关闭旧 daemon 客户端失败: %s", exc)
                # 同步主进程侧生命周期：停旧进程并按新配置拉起
                handler = getattr(app.state, "on_daemon_config_changed", None)
                if handler is not None:
                    try:
                        await asyncio.to_thread(handler, new_url)
                    except Exception as exc:
                        log.warning("同步 daemon 进程生命周期失败: %s", exc)
                return

        app.state.daemon_switch_task = asyncio.create_task(_switch_when_idle())

    async def _apply_settings(app: FastAPI, updates: dict[str, str]) -> list[str]:
        """把表单修改热更新到运行时配置，并持久化到用户配置文件 atomcode-proxy-config.json。

        返回提示消息列表。
        """
        cfg = app.state.config
        messages: list[str] = []

        # 监听地址/端口：服务已绑定，运行时无法变更；保存后重启生效
        host = updates.get("ATOMCODE_PROXY_HOST", "").strip()
        port_raw = updates.get("ATOMCODE_PROXY_PORT", "").strip()
        if host and ("://" in host or "/" in host or "\\" in host or any(c.isspace() for c in host)):
            # 非法地址持久化后会导致重启 bind 失败退出，且 exe 无控制台难以排查
            messages.append("监听地址无效（应为 IP 或主机名，不能含协议前缀、路径或空格），本次未修改监听地址")
            updates.pop("ATOMCODE_PROXY_HOST", None)
            host = ""
        if not host:
            # 留空 = 保持不变：空值持久化会从用户配置文件删除该项，导致重启后丢失。
            # 若用户曾自定义过该项，明确告知恢复默认需手动填写默认值
            updates.pop("ATOMCODE_PROXY_HOST", None)
            if "ATOMCODE_PROXY_HOST" in read_user_config():
                messages.append("监听地址留空：保持当前值不变（如需恢复默认请手动填写 127.0.0.1）")
        host_changed = bool(host) and host != cfg.host
        port_changed = False
        if port_raw:
            try:
                if not 1 <= int(port_raw) <= 65535:
                    messages.append("端口无效（1-65535），本次未修改端口")
                    updates.pop("ATOMCODE_PROXY_PORT", None)
                else:
                    port_changed = int(port_raw) != cfg.port
            except ValueError:
                messages.append("端口无效（1-65535），本次未修改端口")
                updates.pop("ATOMCODE_PROXY_PORT", None)
        else:
            updates.pop("ATOMCODE_PROXY_PORT", None)
            if "ATOMCODE_PROXY_PORT" in read_user_config():
                messages.append("监听端口留空：保持当前值不变（如需恢复默认请手动填写 8765）")
        if host_changed or port_changed:
            messages.append("监听地址/端口已保存，重启服务后生效")

        # 默认 Provider / 审批模式 / 工作目录：热更新，立即生效
        new_provider = updates.get("ATOMCODE_DEFAULT_PROVIDER", "").strip()
        new_mode = updates.get("ATOMCODE_APPROVAL_MODE", "").strip()
        new_workdir = updates.get("ATOMCODE_PROXY_WORKDIR", "").strip()
        persist_updates = dict(updates)
        # Daemon Token 留空 = 保持不变（与 UI 占位符承诺一致）：空值持久化会从
        # 用户配置文件删除该项，重启后回退默认值导致 daemon 认证失败
        if not updates.get("ATOMCODE_DAEMON_TOKEN", "").strip():
            persist_updates.pop("ATOMCODE_DAEMON_TOKEN", None)
        # 默认 Provider / Daemon 地址留空 = 保持不变（与 host/port/token 同语义）：
        # 空值持久化会从用户配置文件删除该项，重启后静默回退默认值
        if not updates.get("ATOMCODE_DEFAULT_PROVIDER", "").strip():
            persist_updates.pop("ATOMCODE_DEFAULT_PROVIDER", None)
        if not updates.get("ATOMCODE_DAEMON_URL", "").strip():
            persist_updates.pop("ATOMCODE_DAEMON_URL", None)
        if new_provider:
            cfg.default_provider = new_provider
        if new_mode:
            cfg.approval_mode = new_mode
        if new_workdir:
            normalized_workdir = normalize_working_directory(new_workdir)
            if normalized_workdir:
                cfg.working_dir = normalized_workdir
                persist_updates["ATOMCODE_PROXY_WORKDIR"] = normalized_workdir
            else:
                messages.append("工作目录无效：目录不存在或不是目录，本次未修改")
                persist_updates.pop("ATOMCODE_PROXY_WORKDIR", None)
        elif "ATOMCODE_PROXY_WORKDIR" in updates:
            messages.append("工作目录不能为空；请保留现有目录或选择一个新目录")
            persist_updates.pop("ATOMCODE_PROXY_WORKDIR", None)

        # 模型别名：解析 k=v 列表（允许清空）；非法片段不静默丢弃，向用户反馈
        aliases: dict[str, str] = {}
        invalid_alias_parts: list[str] = []
        raw = updates.get("ATOMCODE_MODEL_ALIAS", "")
        for pair in raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip() and v.strip():
                    aliases[k.strip()] = v.strip()
                elif pair.strip():
                    invalid_alias_parts.append(pair.strip())
            elif pair.strip():
                invalid_alias_parts.append(pair.strip())
        cfg.model_alias = aliases
        if invalid_alias_parts:
            messages.append("以下模型别名格式无效已忽略（应为 k=v）：" + "、".join(invalid_alias_parts))

        # Daemon 地址/Token：变化则重建 daemon 客户端（用新 cfg），立即生效
        new_daemon_url = updates.get("ATOMCODE_DAEMON_URL", "").strip() or cfg.daemon_url
        new_daemon_token = updates.get("ATOMCODE_DAEMON_TOKEN", "").strip()
        daemon_url_changed = new_daemon_url != cfg.daemon_url
        daemon_token_changed = bool(new_daemon_token) and new_daemon_token != cfg.daemon_token
        if daemon_url_changed or daemon_token_changed:
            # 整个切换序列与延迟切换后台任务互斥：并发重建客户端/并发触发
            # 主进程侧启停会交错覆盖，造成 cfg、客户端与 daemon 进程三方错位
            async with daemon_switch_lock:
                old = app.state.daemon
                busy = isinstance(old, AtomCodeDaemon) and any(s.lock.locked() for s in old._states.values())
                try:
                    if busy:
                        # 有进行中的任务：立即 close 会终止所有客户端正在执行的会话，
                        # 推迟到任务结束后再切换（期间新请求仍走旧连接）。
                        # 此时不能提前改写 cfg：看门狗按 cfg.daemon_url 检测存活，
                        # 提前指向新地址会在延迟窗口内误杀承载在途会话的旧 daemon 进程。
                        app.state.pending_daemon_switch = (new_daemon_url, new_daemon_token or cfg.daemon_token)
                        messages.append("检测到进行中的任务：Daemon 连接将在任务结束后自动切换（期间新请求仍走旧连接）")
                        _schedule_daemon_switch(app)
                    else:
                        cfg.daemon_url = new_daemon_url
                        if new_daemon_token:
                            cfg.daemon_token = new_daemon_token
                        app.state.pending_daemon_switch = None
                        # 先原子换上新客户端再关闭旧客户端：close 期间（含逐会话
                        # 发 stop 的网络等待）新请求立即走新连接，不会踩到已关闭的
                        # httpx 客户端上抛 RuntimeError
                        app.state.daemon = AtomCodeDaemon(
                            cfg.daemon_url,
                            daemon_token=cfg.daemon_token,
                            default_provider=cfg.default_provider,
                            approval_mode=cfg.approval_mode,
                            default_working_dir=cfg.working_dir,
                        )
                        messages.append("Daemon 连接已更新并立即生效（原有会话上下文已重置）")
                        if old is not None:
                            await old.close()
                        # 同步主进程侧 daemon 进程生命周期（停旧进程、按新配置拉起），
                        # 避免看门狗下一轮另起进程后旧进程泄漏为孤儿
                        handler = getattr(app.state, "on_daemon_config_changed", None)
                        if handler is not None:
                            try:
                                await asyncio.to_thread(handler, cfg.daemon_url)
                            except Exception as exc:
                                log.warning("同步 daemon 进程生命周期失败: %s", exc)
                except Exception as exc:
                    log.warning("重建 daemon 客户端失败: %s", exc)
                    messages.append(f"Daemon 配置已保存，但重建连接失败: {exc}")
        else:
            # daemon_url/token 未变：同步 provider/mode/workdir 到现有客户端
            daemon = getattr(app.state, "daemon", None)
            if isinstance(daemon, AtomCodeDaemon):
                daemon.default_provider = cfg.default_provider
                daemon.approval_mode = cfg.approval_mode
                daemon.default_working_dir = cfg.working_dir

        # 环境变量优先级高于用户配置文件：被覆盖的项重启后会恢复环境变量值，需明确提示
        env_overridden = sorted(
            key for key, val in persist_updates.items() if environ_or_dotenv(key) and environ_or_dotenv(key) != val
        )
        if env_overridden:
            messages.append(
                "注意：以下配置项已被环境变量（含 .env）覆盖，重启后将恢复为环境变量的值："
                + "、".join(env_overridden)
            )

        messages.append("配置已保存并立即生效")

        # 持久化到用户配置文件（exe 旁不生成任何文件）
        try:
            cfg_path = write_user_config(persist_updates)
            messages.append(f"已写入用户配置文件: {cfg_path}")
        except OSError as exc:
            log.warning("写入用户配置文件失败: %s", exc)
            messages.append(f"写入用户配置文件失败（修改仅本次运行生效）: {exc}")

        return messages

    @app.get("/", response_class=HTMLResponse)
    async def status_page(request: Request) -> Response:
        """返回 HTML 状态页面。

        同样校验请求来源：页面含 daemon 连接信息（地址/provider），
        host=0.0.0.0 监听时不能让局域网任意主机读取。
        """
        if not _is_local_source(request, cfg):
            return JSONResponse(status_code=403, content={"error": "禁止的来源"})
        logo_b64 = _get_logo_base64()
        logo_html = ""
        if logo_b64:
            logo_html = f'<div style="text-align: center; margin-bottom: 20px;"><img src="data:image/png;base64,{logo_b64}" alt="atomcode-proxy" style="width: 80px; height: 80px;"></div>'
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
        .nav-link {{
            display: inline-block;
            color: #2563eb;
            text-decoration: none;
            font-size: 15px;
            padding: 8px 20px;
            border: 1px solid #2563eb;
            border-radius: 6px;
        }}
        .nav-link:hover {{
            background: #2563eb;
            color: white;
        }}
        .model-tag {{
            display: inline-block;
            background: #e8f0fe;
            color: #1a73e8;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 12px;
            margin: 2px;
        }}
        .update-banner {{
            background: #fff3cd;
            border: 1px solid #ffe08a;
            border-radius: 8px;
            padding: 16px 20px;
            margin: 20px 0;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 12px;
        }}
        .btn {{
            display: inline-block;
            padding: 8px 20px;
            background: #2563eb;
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            cursor: pointer;
        }}
        .btn:hover {{ background: #1d4ed8; }}
        .btn:disabled {{ background: #9ca3af; cursor: not-allowed; }}
        .btn-small {{
            padding: 4px 14px;
            font-size: 13px;
            margin-left: 8px;
        }}
    </style>
</head>
<body>
    {logo_html}
    <h1 style="text-align: center;">atomcode-proxy</h1>
    <div id="update-banner" class="update-banner" style="display:none;">
        <div id="update-banner-content" style="flex:1; min-width:200px;"></div>
    </div>
    <div class="card">
        <h2>服务状态</h2>
        <p>状态: <span class="status running">运行中</span></p>
        <p>版本: {__version__} <button id="check-update-btn" class="btn btn-small" onclick="checkForUpdate()">检查更新</button></p>
        <p>监听地址: <code>http://{cfg.host}:{cfg.port}</code></p>
    </div>
    <div class="card">
        <h2>Daemon 连接</h2>
        <p>地址: <code>{cfg.daemon_url}</code></p>
        <p>Provider: <code>{cfg.default_provider}</code></p>
        <div id="models-info">
            <p class="info">加载模型信息中...</p>
        </div>
    </div>
    <div class="card">
        <h2>客户端接入</h2>
        <p>OpenAI 兼容: <code>http://{cfg.host}:{cfg.port}/v1</code></p>
        <p>Anthropic 兼容: <code>http://{cfg.host}:{cfg.port}</code></p>
        <p>API Key: 任意非空值</p>
    </div>
    <div style="text-align: center;">
        <a href="/settings" class="nav-link">&#9881; 设置</a>
    </div>
    <script>
    (function() {{
        var el = document.getElementById("models-info");
        fetch("/api/models")
            .then(function(r) {{
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }})
            .then(function(data) {{
                var providers = data.providers || [];
                var models = data.models || [];
                var html = "<p>可用 Provider: <strong>" + providers.length + "</strong> 个，模型: <strong>" + models.length + "</strong> 个</p>";
                el.innerHTML = html;
                if (providers.length > 0) {{
                    // provider 名来自 daemon，用 textContent 渲染防 HTML 注入
                    var wrap = document.createElement("div");
                    providers.forEach(function(p) {{
                        var tag = document.createElement("span");
                        tag.className = "model-tag";
                        tag.textContent = p;
                        wrap.appendChild(tag);
                    }});
                    el.appendChild(wrap);
                }}
            }})
            .catch(function(err) {{
                el.innerHTML = '<p class="info" style="color:#dc3545;">无法获取模型列表: ' + err.message + '</p>';
            }});
    }})();
    </script>
    <script>
    // 服务端注入的 GitHub Releases 下载页链接（下载失败时引导用户前往）
    var RELEASES_URL = "{GITHUB_RELEASES_URL}";

    // 通过 ?check_update=1 进入（托盘「检查更新」入口）时自动触发一次检查
    if (location.search.indexOf("check_update") !== -1) {{
        checkForUpdate();
    }}

    function checkForUpdate() {{
        var btn = document.getElementById("check-update-btn");
        if (btn) {{ btn.disabled = true; btn.textContent = "检查中..."; }}
        fetch("/api/update/check")
            .then(function(r) {{
                // 502 等非 200 表示检查失败（如 GitHub 限流），不能当作"已是最新"
                if (!r.ok) {{ throw new Error("HTTP " + r.status); }}
                return r.json();
            }})
            .then(function(data) {{
                var banner = document.getElementById("update-banner");
                var content = document.getElementById("update-banner-content");
                if (data.has_update) {{
                    if (btn) {{ btn.textContent = "有新版本"; btn.disabled = false; }}
                    banner.style.display = "flex";
                    var html = '<strong>🎉 发现新版本！</strong> ';
                    html += data.current_version + ' &rarr; <strong>v' + data.latest_version + '</strong>';
                    if (data.download_url) {{
                        html += ' <button id="download-btn" class="btn" onclick="downloadUpdate()">下载更新</button>';
                        html += ' <span id="download-status"></span>';
                    }} else {{
                        html += ' <a href="' + data.release_url + '" target="_blank" class="btn">前往下载</a>';
                    }}
                    content.innerHTML = html;
                }} else {{
                    banner.style.display = "none";
                    if (btn) {{
                        btn.textContent = "已是最新";
                        setTimeout(function() {{ btn.textContent = "检查更新"; btn.disabled = false; }}, 2000);
                    }}
                }}
            }})
            .catch(function() {{
                if (btn) {{ btn.textContent = "检查失败，点击重试"; btn.disabled = false; }}
            }});
    }}

    function downloadUpdate() {{
        var btn = document.getElementById("download-btn");
        var status = document.getElementById("download-status");
        if (btn) {{ btn.disabled = true; btn.textContent = "正在下载..."; }}
        if (status) {{ status.textContent = ""; }}
        fetch("/api/update/download", {{ method: "POST" }})
            .then(function(r) {{
                if (!r.ok) {{
                    // 后端返回 JSON 错误（含 fallback_url 兜底链接）
                    return r.json().catch(function() {{ return {{ error: "HTTP " + r.status }}; }})
                        .then(function(d) {{ throw d; }});
                }}
                var filename = r.headers.get("X-Filename") || "update.exe";
                return r.blob().then(function(blob) {{ return {{ blob: blob, filename: filename }}; }});
            }})
            .then(function(data) {{
                // 交给浏览器保存到其默认下载路径
                var url = URL.createObjectURL(data.blob);
                var a = document.createElement("a");
                a.href = url;
                a.download = data.filename;
                document.body.appendChild(a);
                a.click();
                a.remove();
                setTimeout(function() {{ URL.revokeObjectURL(url); }}, 10000);
                if (btn) {{ btn.style.display = "none"; }}
                if (status) {{
                    status.innerHTML = '<span style="color:#155724;">✅ 下载成功</span>';
                }}
            }})
            .catch(function(err) {{
                if (btn) {{ btn.disabled = false; btn.textContent = "重试下载"; }}
                var reason = (err && (err.error || err.message)) || "未知错误";
                var fallback = (err && err.fallback_url) || RELEASES_URL;
                if (status) {{
                    status.innerHTML = '';
                    var span = document.createElement("span");
                    span.style.color = "#dc3545";
                    span.textContent = "❌ 下载失败，请到 ";
                    var link = document.createElement("a");
                    link.href = fallback;
                    link.target = "_blank";
                    link.style.color = "#dc3545";
                    link.style.textDecoration = "underline";
                    link.textContent = fallback;
                    span.appendChild(link);
                    span.appendChild(document.createTextNode(" 下载。原因：" + reason));
                    status.appendChild(span);
                }}
            }});
    }}
    </script>
</body>
</html>"""

    return app
