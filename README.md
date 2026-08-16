# atomcode-proxy

<p align="center">
  <img src="assets/logo.png" alt="atomcode-proxy" width="120" />
</p>

把 OpenAI / Anthropic 兼容协议翻译为 AtomCode 本地 daemon 协议的适配代理，
使 Codex CLI、Cursor 等工具可以直接使用 AtomCode 的云端模型。

## 架构

```mermaid
flowchart LR
    A[Codex CLI / Cursor] -->|OpenAI /v1/chat/completions| P[atomcode-proxy<br/>127.0.0.1:8765]
    B[Claude Code / Cursor] -->|Anthropic /v1/messages| P
    P -->|POST /chat SSE| D[AtomCode daemon<br/>127.0.0.1:13456]
    D -->|HKDF 签名请求| C[llm-api.atomgit.com]
```

数据流：上游客户端 -> 本代理（协议翻译）-> AtomCode 本地 daemon（`/sessions` + `/chat`）-> 云端。

## 便携版下载（Windows）

无需 Python 环境，**双击即用**。发布页：

👉 <https://github.com/BlackTor-tor/atomcode-proxy/releases>

使用步骤：

1. 从最新 Release 下载 `atomcode-proxy-<版本>-windows-x64.exe`。
2. **双击运行**即可——程序会自动启动 AtomCode daemon（如果未运行）并启动代理服务。
3. 首次启动会弹出目录选择器；选择的目录就是 daemon 的工作目录，并会保存到 exe 旁的 `.env`。默认监听 `127.0.0.1:8765`。
4. 程序自动打开浏览器显示状态页面，系统托盘图标提供以下功能：
   - **打开状态页面**：查看服务状态、daemon 连接信息、客户端接入指南
   - **打开设置页面**：在浏览器中修改端口、provider 等配置并保存到 `.env`，重启 exe 生效
   - **显示日志**：用默认程序打开运行日志文件
   - **退出**：停止所有服务并关闭程序
5. 点击托盘“退出”即完全关闭（exe 退出时会自动关闭它启动的 daemon）。

> 💡 高级用户可在 exe 旁创建 `.env` 文件自定义配置（端口、provider 等），运行 `atomcode-proxy.exe --init-config` 可生成配置模板。
>
> 📝 日志文件保存在 `logs/atomcode-proxy.log`（exe 同级目录），可通过托盘菜单直接查看。

主要配置项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ATOMCODE_PROXY_HOST` / `ATOMCODE_PROXY_PORT` | `127.0.0.1` / `8765` | 代理监听地址与端口 |
| `ATOMCODE_DAEMON_URL` | `http://127.0.0.1:13456` | AtomCode daemon 地址 |
| `ATOMCODE_DAEMON_TOKEN` | `atomcode_webui` | daemon 认证 token |
| `ATOMCODE_DEFAULT_PROVIDER` | `AtomGit-deepseek-v4-flash` | 默认模型 provider |
| `ATOMCODE_APPROVAL_MODE` | `bypass` | 审批模式 |
| `ATOMCODE_PROXY_WORKDIR` | 启动时选择 | daemon 工作目录；也可由请求按客户端覆盖 |
| `ATOMCODE_MODEL_ALIAS` | 空 | 模型别名 |

> ⚠️ Windows Defender / 杀毒软件可能对 PyInstaller 打包的单文件程序误报（未经数字签名的可执行文件常见现象）。如遇拦截，请添加信任或允许运行，也可按下文自行从源码构建。

## 前置条件

- **便携版（exe）**：无需任何前置条件，双击即可运行。程序会自动检测并启动 AtomCode daemon。
- **源码运行**：需要 Python 3.10+ 和 AtomCode daemon（程序同样会自动启动 daemon）。

安装依赖（源码模式）：

```bash
pip install -r requirements.txt
```

## 启动

```bash
python run.py
# 自动启动 daemon（如未运行）并启动代理，默认监听 127.0.0.1:8765
```

生成可选配置文件模板：

```bash
python run.py --init-config
```

## 从源码构建

开发模式直接运行：

```powershell
python run.py
```

构建便携版单文件 exe（PyInstaller onefile）：

```powershell
.\scripts\build.ps1 -Version 0.1.0
# 产物在 release\ 目录
```

自动发布：推送 `v*` 格式的 tag（如 `git tag v0.1.0; git push origin v0.1.0`）会触发 GitHub Actions（`.github/workflows/release.yml`）在 Windows 上自动构建 exe 并发布 Release，版本号取自 tag 名。

## 配置

除工作目录需要在首次启动时确认外，其余配置均有内置默认值，**无需 `.env` 文件即可运行**。

如需自定义，可在 exe 旁（开发模式为项目根目录）创建 `.env` 文件，运行 `--init-config` 可生成模板。
优先级：**已存在的系统环境变量 > `.env` 文件 > 内置默认值**。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ATOMCODE_PROXY_HOST` | `127.0.0.1` | 代理监听地址 |
| `ATOMCODE_PROXY_PORT` | `8765` | 代理监听端口 |
| `ATOMCODE_DAEMON_URL` | `http://127.0.0.1:13456` | daemon 地址 |
| `ATOMCODE_DAEMON_TOKEN` | `atomcode_webui` | daemon 认证 token |
| `ATOMCODE_DEFAULT_PROVIDER` | `AtomGit-deepseek-v4-flash` | 默认模型 provider |
| `ATOMCODE_APPROVAL_MODE` | `bypass` | 审批模式：`bypass`/`build`/`plan`/`accept_edits` |
| `ATOMCODE_PROXY_WORKDIR` | 启动时选择 | daemon 默认工作目录；请求可用 header、body 或 URL 参数覆盖 |
| `ATOMCODE_MODEL_ALIAS` | 空 | 模型别名，逗号分隔 `k=v`，如 `gpt-4o=AtomGit-deepseek-v4-flash` |

`.env` 加载在 `config.py` 模块导入时完成，无需额外依赖（自带轻量解析，仅支持 `KEY=VALUE`、`#` 注释、引号包裹值）。

## 客户端接入

通用要点：

- **Base URL（OpenAI 兼容）**：`http://127.0.0.1:8765/v1`
- **Base URL（Anthropic 兼容）**：`http://127.0.0.1:8765`
- **API Key**：任意非空值即可（代理不校验）
- **可用模型**：`AtomGit-deepseek-v4-flash`（默认）、`AtomGit-Qwen-Qwen3-VL-8B-Instruct`

### Codex CLI（OpenAI 兼容）

```toml
# ~/.codex/config.toml
model_provider = "atomcode"
model = "AtomGit-deepseek-v4-flash"

[model_providers.atomcode]
name = "atomcode"
base_url = "http://127.0.0.1:8765/v1"
env_key = "ATOMCODE_API_KEY"   # 任意非空值即可，代理不校验
```

```powershell
$env:ATOMCODE_API_KEY = "dummy"
codex exec "你好"
```

### Cursor（OpenAI 或 Anthropic 兼容）

Settings -> Models -> 添加 provider：

- **方式一**（OpenAI 兼容）：Type 选 "OpenAI"，Base URL 填 `http://127.0.0.1:8765/v1`，API Key 任意值
- **方式二**（Anthropic 兼容）：Type 选 "Anthropic"，Base URL 填 `http://127.0.0.1:8765`，API Key 任意值

添加模型后勾选 `AtomGit-deepseek-v4-flash` 即可使用。

### Claude Code（Anthropic 兼容）

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8765"
$env:ANTHROPIC_AUTH_TOKEN = "dummy"
claude
```

### DeepSeek Harness（dsh）

1. 本地部署 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 后，运行 `dsh web`。
2. 打开 Web UI（默认 `http://127.0.0.1:3080/`），点击 **设置 → 模型 → 自定义设置**：

   - API 地址：`http://127.0.0.1:8765/v1`
   - 密钥：任意非空值即可（代理不校验）
   - 自定义模型名称：`AtomGit-deepseek-v4-flash`、`AtomGit-Qwen-Qwen3-VL-8B-Instruct`
   - 实际模型：`DeepSeek-V4-Flash`、`Qwen-Qwen3-VL-8B-Instruct`

   配置效果见下图：

   ![dsh Web UI 自定义模型配置](assets/dsh-webui-config.png)

### Cline / Roo Code（VS Code 插件，OpenAI 兼容）

API Provider 选 **OpenAI Compatible**：

- Base URL: `http://127.0.0.1:8765/v1`
- API Key: 任意值
- Model ID: `AtomGit-deepseek-v4-flash`

### Cherry Studio / Chatbox（桌面客户端，OpenAI 兼容）

添加自定义 Provider：

- API 地址: `http://127.0.0.1:8765/v1`
- API Key: 任意值
- 模型名: `AtomGit-deepseek-v4-flash`

### OpenAI SDK（编程调用）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="dummy",
)
resp = client.chat.completions.create(
    model="AtomGit-deepseek-v4-flash",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### curl 快速验证

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy" \
  -d '{"model":"AtomGit-deepseek-v4-flash","messages":[{"role":"user","content":"说你好"}]}'
```

## 多客户端工作目录与会话隔离

代理不会假设 Cursor、Codex CLI 或 Claude Code 的进程目录就是工作区目录。标准 OpenAI/Anthropic 协议没有统一的 cwd 字段，因此目录来源按以下优先级处理：

1. 请求显式目录：`X-AtomCode-Working-Directory`（也接受 `X-Working-Directory`、`X-Workspace-Directory`、`X-Cursor-Workspace-Path`）。
2. 请求 JSON 的 `working_dir` / `cwd` / `workspace_path`，或 `metadata` 中的同名字段。
3. Base URL 查询参数 `?working_dir=F:/Projects/example`。
4. 代理启动时的目录选择；该选择会保存到 exe 旁的 `.env`，作为没有请求级目录的客户端默认值。

请求级目录必须是本机已存在的绝对目录。每个客户端身份、工作目录和会话 ID 都会绑定独立 daemon session；如果客户端没有提供稳定会话 ID，代理会根据完整消息历史前缀识别连续回合，不会把两个新对话共用一个 session。

注意：Cursor、Codex CLI、Claude Code 是否发送工作区路径取决于客户端版本和协议适配器；标准 OpenAI/Anthropic 请求不保证包含 cwd。客户端未发送路径时，代理不能从网络请求反推出 IDE 当前目录，会使用启动时选择的默认目录。

Cursor、Codex CLI、Claude Code、Cline 等无法发送自定义 header 时，使用每个工作区一个代理实例（不同端口）最可靠；也可以在其 Base URL 中加入 `?working_dir=...`。支持自定义 header 的客户端直接发送 `X-AtomCode-Working-Directory` 即可。

## 协议映射说明

- OpenAI/Anthropic `messages` 的当前 prompt 发给 daemon；首次建立或恢复 session 时，之前的完整消息历史会写回 daemon。
- `reasoning` 事件映射为 OpenAI 的 `reasoning_content`（DeepSeek 风格）；Anthropic 端忽略 reasoning 只传正文。
- `text` 事件按 8 字符切块模拟流式输出。
- 工具调用：daemon 处于 `bypass` 模式自行执行工具，代理不做透传。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 对话（stream 与非 stream） |
| GET | `/v1/models` | 模型列表（OpenAI 格式） |
| POST | `/v1/messages` | Anthropic 对话（stream 与非 stream） |
| GET | `/health` | 健康检查 |

## 已知限制

- daemon 无 OpenAI/Anthropic 原生端点，必须经本代理转发。
- 云端直连需要 HKDF 请求签名，本代理只走本地 daemon，不直连云端。
- 多客户端或多工作区会按客户端身份、工作目录和逻辑会话分别绑定 daemon session，各客户端上下文互不串扰。
- 工具调用由 daemon 在 `bypass` 模式下自行执行（如文件读写、命令执行），**不映射为 OpenAI/Anthropic 的 tool_calls 协议**；因此依赖标准 tool_calls 的客户端（如强制 function calling 的编排框架）可能无法获得工具结果透传，但对话与代码生成不受影响。
- 模型的 `reasoning_content`（思维链）仅在 OpenAI 兼容端点返回；Anthropic 端点忽略 reasoning，只输出正文，避免客户端因缺 signature 报错。
