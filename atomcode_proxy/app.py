"""FastAPI 应用装配：把 OpenAI/Anthropic 适配器挂到 /v1/* 路径。"""
from __future__ import annotations

import base64
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__
from .config import Config, _default_env_path
from .daemon import AtomCodeDaemon
from . import anthropic_adapter, openai_adapter
from .updater import (
    GITHUB_RELEASES_URL,
    check_for_update,
    download_latest_release,
)

log = logging.getLogger("atomcode_proxy")


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


def _is_local_download_source(request: Request, cfg: Config) -> bool:
    """校验下载请求来源是否为本服务页面（防跨站写盘）。

    恶意网页/局域网主机发起的请求会携带其自身的 Origin/Referer，
    与本服务地址不匹配即拒绝；无来源头的直连请求（如 curl）放行。
    """
    allowed = {
        f"http://{cfg.host}:{cfg.port}",
        f"http://127.0.0.1:{cfg.port}",
        f"http://localhost:{cfg.port}",
        f"http://[::1]:{cfg.port}",
    }
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    for value in (origin, referer):
        if value and not any(value == p or value.startswith(p + "/") for p in allowed):
            return False
    return True


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
    async def api_download_update(request: Request) -> JSONResponse:
        """下载最新版本 exe 到本地下载目录。

        仅接受来自本服务页面的 POST 请求，防止恶意网页/局域网主机
        通过跨站请求触发写盘。
        """
        if not _is_local_download_source(request, cfg):
            log.warning("拒绝来自非本服务来源的下载请求")
            return JSONResponse(status_code=403, content={"error": "禁止的来源"})
        try:
            update_info = await check_for_update(__version__)
            if update_info is None:
                return JSONResponse(status_code=404, content={"error": "无可用更新"})
            path = await download_latest_release(update_info)
            if path is None:
                # 优先使用 GitHub API 返回的权威 release 页链接，避免拼接 tag 格式漂移导致 404
                fallback_url = update_info.get("release_url") or (
                    f"{GITHUB_RELEASES_URL}/tag/v{update_info.get('latest_version', '')}"
                )
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": "下载失败",
                        "fallback_url": fallback_url,
                    },
                )
            return JSONResponse({
                "success": True,
                "path": str(path),
                "version": update_info.get("latest_version", ""),
            })
        except Exception as e:
            log.warning("下载更新失败: %s", e)
            return JSONResponse(status_code=502, content={"error": str(e)})

    # ── 模型列表 API ─────────────────────────────────────────────

    @app.get("/api/models")
    async def api_list_models() -> JSONResponse:
        """返回可用模型列表（从 daemon 获取）。"""
        daemon: AtomCodeDaemon = app.state.daemon
        try:
            models = await daemon.list_models()
            providers = sorted({m.get("provider", "") for m in models if m.get("provider")})
            return JSONResponse({"providers": providers, "models": models})
        except Exception as e:
            log.warning("获取模型列表失败: %s", e)
            return JSONResponse(status_code=502, content={"error": str(e)})

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

    # 内置默认值：.env 文件和系统环境变量均未覆盖时，设置页回退显示这些值。
    # 取自 cfg（已合并 .env / 环境变量 / 内置默认），避免默认值重复定义。
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
        """读取 .env 文件，返回 {key: value} 字典（忽略注释行）。"""
        env_path = _default_env_path()
        result: dict[str, str] = {}
        if not env_path.is_file():
            return result
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
        return result

    def _write_env_file(updates: dict[str, str]) -> Path:
        """更新 .env 文件中对应的 KEY=VALUE 行；不存在则创建。"""
        env_path = _default_env_path()
        if env_path.is_file():
            content = env_path.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            new_lines: list[str] = []
            handled: set[str] = set()
            for line in lines:
                stripped = line.strip()
                # 匹配注释掉的 #KEY=VALUE 或正常 KEY=VALUE
                m = re.match(r"^#?\s*([A-Z_][A-Z0-9_]*)=(.*)$", stripped)
                if m and m.group(1) in updates:
                    key = m.group(1)
                    val = updates[key]
                    new_lines.append(f"{key}={val}\n")
                    handled.add(key)
                else:
                    new_lines.append(line if line.endswith("\n") else line + "\n")
            # 追加未处理的字段
            for key, val in updates.items():
                if key not in handled:
                    new_lines.append(f"{key}={val}\n")
            env_path.write_text("".join(new_lines), encoding="utf-8")
        else:
            parts = [f"{k}={v}\n" for k, v in updates.items()]
            env_path.write_text("".join(parts), encoding="utf-8")
        return env_path

    def _current_value(field_name: str, env_vals: dict[str, str]) -> str:
        """获取字段当前值：优先 .env 文件，其次环境变量，最后内置默认值。"""
        if field_name in env_vals:
            return env_vals[field_name]
        val = os.environ.get(field_name, "")
        if val:
            return val
        return _BUILTIN_DEFAULTS.get(field_name, "")

    def _settings_html(env_vals: dict[str, str], message: str = "") -> str:
        """生成设置页面 HTML。"""
        logo_b64 = _get_logo_base64()
        logo_html = ""
        if logo_b64:
            logo_html = f'<div style="text-align: center; margin-bottom: 20px;"><img src="data:image/png;base64,{logo_b64}" alt="atomcode-proxy" style="width: 80px; height: 80px;"></div>'

        msg_html = ""
        if message:
            msg_html = f'<div style="background:#d4edda;color:#155724;padding:12px 16px;border-radius:8px;margin-bottom:20px;">{message}</div>'

        fields_html = ""
        for field_name, label, input_type, options in _SETTING_FIELDS:
            val = _current_value(field_name, env_vals)
            if input_type == "select" and options:
                opts = "".join(
                    f'<option value="{o}" {"selected" if val == o else ""}>{o}</option>'
                    for o in options
                )
                input_html = f'<select name="{field_name}" style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;">{opts}</select>'
            elif input_type == "dynamic-select":
                escaped_val = val.replace('"', '&quot;')
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
                    var currentVal = "{escaped_val}";
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
                input_html = f'<input type="password" name="{field_name}" value="{val}" style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;">'
            else:
                input_html = f'<input type="text" name="{field_name}" value="{val}" style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:14px;">'
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
        <div style="text-align:center; margin: 20px 0;">
            <button type="submit" class="btn">保存配置</button>
        </div>
    </form>
    <div style="text-align:center;">
        <a href="/" class="nav-link">&larr; 返回状态页面</a>
    </div>
</body>
</html>"""

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page() -> str:
        """返回设置页面 HTML 表单。"""
        env_vals = _read_env_file()
        return _settings_html(env_vals)

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request) -> str:
        """接收表单数据并保存到 .env 文件。"""
        form = await request.form()
        updates: dict[str, str] = {}
        for field_name, _, input_type, _ in _SETTING_FIELDS:
            val = form.get(field_name, "")
            if val is not None:
                updates[field_name] = str(val)
        env_path = _write_env_file(updates)
        log.info("配置已保存到: %s", env_path)
        return _settings_html(
            _read_env_file(),
            message=f"配置已保存到 {env_path}。请重启服务以使新配置生效。",
        )

    @app.get("/", response_class=HTMLResponse)
    async def status_page() -> str:
        """返回 HTML 状态页面。"""
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
                if (providers.length > 0) {{
                    html += "<div>" + providers.map(function(p) {{ return '<span class=\\"model-tag\\">' + p + '</span>'; }}).join("") + "</div>";
                }}
                el.innerHTML = html;
            }})
            .catch(function(err) {{
                el.innerHTML = '<p class="info" style="color:#dc3545;">无法获取模型列表: ' + err.message + '</p>';
            }});
    }})();
    </script>
    <script>
    // 服务端注入的 GitHub Releases 下载页链接（失败兜底提示用）
    var RELEASES_URL = "{GITHUB_RELEASES_URL}";

    // 通过 ?check_update=1 进入（托盘「检查更新」入口）时自动触发一次检查
    if (location.search.indexOf("check_update") !== -1) {{
        checkForUpdate();
    }}

    function checkForUpdate() {{
        var btn = document.getElementById("check-update-btn");
        if (btn) {{ btn.disabled = true; btn.textContent = "检查中..."; }}
        fetch("/api/update/check")
            .then(function(r) {{ return r.json(); }})
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
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                if (data.success) {{
                    if (btn) {{ btn.style.display = "none"; }}
                    if (status) {{
                        status.innerHTML = '<span style="color:#155724;">✅ 下载完成！文件已保存到：<code>' + data.path + '</code></span>';
                    }}
                }} else if (data.fallback_url) {{
                    if (btn) {{ btn.style.display = "none"; }}
                    if (status) {{
                        status.innerHTML = '<span style="color:#dc3545;">❌ 下载失败，请到下载页：<a href="' + data.fallback_url + '" target="_blank" style="color:#dc3545;text-decoration:underline;">' + data.fallback_url + '</a> 进行下载</span>';
                    }}
                }} else {{
                    if (btn) {{ btn.disabled = false; btn.textContent = "重试下载"; }}
                    if (status) {{
                        status.innerHTML = '<span style="color:#dc3545;">❌ 下载失败：' + (data.error || "未知错误") + '，请到下载页：<a href="' + RELEASES_URL + '" target="_blank" style="color:#dc3545;text-decoration:underline;">' + RELEASES_URL + '</a> 进行下载</span>';
                    }}
                }}
            }})
            .catch(function() {{
                if (btn) {{ btn.disabled = false; btn.textContent = "重试下载"; }}
                if (status) {{
                    status.innerHTML = '<span style="color:#dc3545;">❌ 下载失败，请到下载页：<a href="' + RELEASES_URL + '" target="_blank" style="color:#dc3545;text-decoration:underline;">' + RELEASES_URL + '</a> 进行下载</span>';
                }}
            }});
    }}
    </script>
</body>
</html>"""

    return app
