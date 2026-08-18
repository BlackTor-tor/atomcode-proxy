"""AtomCode 本地 daemon 的 HTTP 客户端与 SSE 事件解析。

职责：
- 按上游逻辑会话维护 AtomCode session，并在取消时停止 daemon 任务
- 把 /chat 的 SSE 字节流解析为结构化事件 dict
- 对外暴露 async 接口，供 OpenAI/Anthropic 适配器调用
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("atomcode_proxy.daemon")

BASE_HEADERS_DEFAULT = {
    "Authorization": "Bearer atomcode_webui",
    "X-AtomCode-Client": "webui",
}

# 代理只为每个逻辑会话保留一个 daemon session，避免并发请求被分配到无历史的新 session。
MAX_CONVERSATIONS = 256

# provider 名单缓存有效期（秒）
_PROVIDERS_CACHE_TTL = 300.0


def _read_dynamic_daemon_token(base_url: str) -> str | None:
    """读取 atomcode 5.0.6+ daemon 写入 ~/.atomcode/daemon-<port>.json 的随机 token。

    5.0.6 起 daemon 不再接受固定的 atomcode_webui token，每次启动生成
    随机 token 写入该文件。仅本地 daemon 适用；文件缺失/损坏（含 5.0.5
    等旧版本）返回 None，调用方回退到配置的静态 token。
    """
    try:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return None
        port = parsed.port or 13456
        data = json.loads((Path.home() / ".atomcode" / f"daemon-{port}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("读取 daemon 动态 token 文件失败: %s", exc)
        return None
    token = data.get("token")
    return token if isinstance(token, str) and token else None


def _wrap_httpx_errors(exc: Exception) -> AtomCodeDaemonError:
    """把 daemon 不可达等网络层异常包装为 502 语义的 AtomCodeDaemonError。

    适配器只捕获 AtomCodeDaemonError；若 httpx 连接异常直接穿透，
    非流式会变裸 500、流式会断流无 [DONE]，破坏 502 upstream_error 契约。
    """
    if isinstance(exc, AtomCodeDaemonError):
        return exc
    return AtomCodeDaemonError(f"daemon unreachable: {exc}", 502)


class AtomCodeDaemonError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SessionBusyError(AtomCodeDaemonError):
    """daemon 返回 409：该 session 正在执行其他 chat 操作。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session busy: {session_id}", 409)
        self.session_id = session_id


@dataclass
class ConversationState:
    """一个上游逻辑会话对应的 daemon session 和串行锁。"""

    conversation_key: str
    working_dir: str
    session_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 最近一次被 _state_for 命中的时间，用于超量时按 LRU 淘汰
    last_used: float = field(default_factory=time.monotonic)


class AtomCodeDaemon:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:13456",
        *,
        daemon_token: str = "atomcode_webui",
        default_provider: str = "AtomGit-deepseek-v4-flash",
        approval_mode: str = "bypass",
        default_working_dir: str = ".",
        timeout: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_provider = default_provider
        self.approval_mode = approval_mode
        self.default_working_dir = default_working_dir
        # 本地 daemon 优先用 5.0.6+ 的动态 token；文件不存在（旧版/远程）
        # 回退到配置的静态 token
        effective_token = _read_dynamic_daemon_token(self.base_url) or daemon_token
        if effective_token != daemon_token:
            log.info("检测到 daemon 动态 token 文件，优先使用其随机 token 鉴权")
        self._headers = {
            "Authorization": f"Bearer {effective_token}",
            "X-AtomCode-Client": "webui",
        }
        # read/write/pool 不设上限：长任务（模型长时间思考、agent 执行工具）
        # 可能数分钟无事件输出，固定 300s 超时会中断会话。connect 保留 10s。
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        self._states: dict[str, ConversationState] = {}
        self._states_lock = asyncio.Lock()
        # daemon provider 名单缓存（用于未知模型名回退判断）
        self._providers_cache: set[str] = set()
        self._providers_cached_at: float = 0.0

    async def _refresh_auth_token(self) -> bool:
        """收到 401 时重读 daemon token 文件刷新鉴权头；成功返回 True。

        daemon 被看门狗/用户重启后会生成新随机 token，客户端缓存的旧 token
        失效。token 文件未变化或不可读时返回 False，让 401 按原语义上抛。
        """
        token = _read_dynamic_daemon_token(self.base_url)
        new_header = f"Bearer {token}" if token else None
        if not new_header or self._client.headers.get("Authorization") == new_header:
            return False
        self._client.headers["Authorization"] = new_header
        log.info("daemon 鉴权 401，已从 token 文件刷新为动态 token")
        return True

    async def close(self) -> None:
        states = list(self._states.values())
        await asyncio.gather(
            *(self._stop_chat_shielded(state.session_id) for state in states if state.session_id),
            return_exceptions=True,
        )
        await self._client.aclose()

    async def _create_session(self, working_dir: str) -> str:
        try:
            resp = await self._client.post("/sessions", json={"working_dir": working_dir})
            if resp.status_code == 401 and await self._refresh_auth_token():
                resp = await self._client.post("/sessions", json={"working_dir": working_dir})
        except httpx.HTTPError as exc:
            raise _wrap_httpx_errors(exc) from exc
        if resp.status_code < 200 or resp.status_code >= 300:
            raise AtomCodeDaemonError(f"create session failed: {resp.status_code} {resp.text[:300]}", resp.status_code)
        try:
            data = resp.json()
        except ValueError as exc:
            # 端口可能被其他 HTTP 服务占用（返回 HTML 200），解析失败需转为
            # 502 语义而非穿透为裸 500
            raise AtomCodeDaemonError(
                f"daemon returned non-JSON response: {resp.text[:200]!r}", 502
            ) from exc
        sid = data.get("session_id") or data.get("id")
        if not sid:
            raise AtomCodeDaemonError(f"create session: no session_id in {data!r}")
        return sid

    async def _import_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        try:
            resp = await self._client.post(
                f"/sessions/{session_id}/messages",
                json={"messages": messages},
            )
            if resp.status_code == 401 and await self._refresh_auth_token():
                resp = await self._client.post(
                    f"/sessions/{session_id}/messages",
                    json={"messages": messages},
                )
        except httpx.HTTPError as exc:
            raise _wrap_httpx_errors(exc) from exc
        if resp.status_code < 200 or resp.status_code >= 300:
            raise AtomCodeDaemonError(
                f"import session messages failed: {resp.status_code} {resp.text[:300]}",
                resp.status_code,
            )

    async def stop_chat(self, session_id: str) -> None:
        """通知 AtomCode 停止指定 session 的 agent 任务；接口本身是幂等的。"""
        try:
            resp = await asyncio.wait_for(
                self._client.post("/chat/stop", json={"session_id": session_id}),
                timeout=5.0,
            )
            if resp.status_code == 401:
                if await self._refresh_auth_token():
                    resp = await asyncio.wait_for(
                        self._client.post("/chat/stop", json={"session_id": session_id}),
                        timeout=5.0,
                    )
        except Exception as exc:
            log.warning("stop daemon chat failed for session %s: %s", session_id, exc)
            return
        if resp.status_code not in (200, 201, 202, 204, 404, 409):
            log.warning("stop daemon chat returned %s for session %s", resp.status_code, session_id)

    async def _stop_chat_shielded(self, session_id: str) -> None:
        """取消路径中保护 stop 请求，避免上游取消传播把清理请求一并取消。"""
        task = asyncio.create_task(self.stop_chat(session_id))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 当前任务已被取消：继续等待独立 stop task 完成后必须重新抛出取消，
            # 否则取消被吞掉会破坏 asyncio 的结构化取消语义。
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _state_for(self, conversation_key: str, working_dir: str) -> ConversationState:
        old_session_ids: list[str] = []
        async with self._states_lock:
            state = self._states.get(conversation_key)
            if state is None or state.working_dir != working_dir:
                if state is not None and state.session_id:
                    old_session_ids.append(state.session_id)
                state = ConversationState(conversation_key, working_dir)
                self._states[conversation_key] = state
            else:
                state.last_used = time.monotonic()
            if len(self._states) > MAX_CONVERSATIONS:
                # 按 LRU 淘汰最久未用且未被占用的会话；被淘汰者若持有
                # daemon session 需记录并停止，避免 daemon 侧残留任务。
                candidates = [
                    (value.last_used, key, value)
                    for key, value in self._states.items()
                    if key != conversation_key and not value.lock.locked()
                ]
                if candidates:
                    _, _evict_key, evicted = min(candidates, key=lambda item: item[0])
                    self._states.pop(_evict_key, None)
                    if evicted.session_id:
                        old_session_ids.append(evicted.session_id)
        # 在锁外执行网络请求，避免长时间持有 _states_lock
        for session_id in old_session_ids:
            await self._stop_chat_shielded(session_id)
        return state

    async def _ensure_session(self, state: ConversationState, history: list[dict[str, Any]]) -> str:
        if state.session_id:
            return state.session_id
        session_id = await self._create_session(state.working_dir)
        try:
            await self._import_messages(session_id, history)
        except Exception:
            await self._stop_chat_shielded(session_id)
            raise
        state.session_id = session_id
        return session_id

    # ---------- 模型列表 ----------

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            resp = await self._client.get("/models")
            if resp.status_code == 401 and await self._refresh_auth_token():
                resp = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise _wrap_httpx_errors(exc) from exc
        if resp.status_code != 200:
            raise AtomCodeDaemonError(f"list models failed: {resp.status_code} {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AtomCodeDaemonError(
                f"daemon returned non-JSON response: {resp.text[:200]!r}", 502
            ) from exc

    async def known_providers(self) -> set[str]:
        """返回 daemon 已知 provider 名集合（带 TTL 缓存）。

        获取失败时返回空集，调用方应回退到直接透传，避免 daemon 不可用时段接全断。
        """
        now = time.monotonic()
        if self._providers_cache and now - self._providers_cached_at < _PROVIDERS_CACHE_TTL:
            return self._providers_cache
        try:
            models = await self.list_models()
        except (AtomCodeDaemonError, httpx.HTTPError) as exc:
            log.warning("获取 provider 名单失败，模型名解析回退透传: %s", exc)
            return set()
        self._providers_cache = {m.get("provider", "") for m in models if m.get("provider")}
        self._providers_cached_at = now
        return self._providers_cache

    async def resolve_provider(
        self,
        model: str | None,
        aliases: dict[str, str] | None = None,
        default_provider: str | None = None,
    ) -> str:
        """把上游请求的 model 名解析为 daemon 的 provider 名。

        优先级：别名映射 > 已知 provider 名透传 > 回退默认 provider。
        客户端默认模型名（claude-*/gpt-* 等）不在 daemon 名单内时回退默认值，
        避免daemon 返回 error 事件被吞掉后变成静默空响应。
        """
        default_provider = default_provider or self.default_provider
        if not model:
            return default_provider
        if aliases and model in aliases:
            return aliases[model]
        known = await self.known_providers()
        if not known or model in known:
            # 名单不可用时维持旧的透传行为（错误会由适配器的 error 事件处理浮出）
            return model
        log.warning("模型名 %r 不在 daemon provider 名单中，回退默认 provider %r", model, default_provider)
        return default_provider

    # ---------- 对话 ----------

    async def chat_with_session(
        self,
        message: str,
        *,
        working_dir: str | None = None,
        conversation_key: str | None = None,
        history: list[dict[str, Any]] | None = None,
        client_key: str | None = None,
        provider: str | None = None,
        approval_mode: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """在一个逻辑会话内串行执行，并在取消时立即通知 daemon 停止。"""
        wd = working_dir or self.default_working_dir
        if not wd:
            raise AtomCodeDaemonError("working directory is not configured")
        key = conversation_key or f"legacy:{client_key or 'default'}"
        state = await self._state_for(key, wd)

        # 新回合到达时先终止同一逻辑会话的旧回合，避免 daemon 端 409 busy。
        if state.lock.locked() and state.session_id:
            await self._stop_chat_shielded(state.session_id)

        async with state.lock:
            session_id = await self._ensure_session(state, history or [])
            retried = False
            try:
                while True:
                    terminal_seen = False
                    try:
                        async for ev in self._chat_stream_raw(
                            message,
                            session_id=session_id,
                            working_dir=wd,
                            provider=provider,
                            approval_mode=approval_mode,
                        ):
                            if ev.get("type") in {"done", "stopped", "error"}:
                                terminal_seen = True
                            yield ev
                        if not terminal_seen:
                            await self._stop_chat_shielded(session_id)
                        return
                    except SessionBusyError:
                        if retried:
                            await self._stop_chat_shielded(session_id)
                            state.session_id = None
                            raise
                        retried = True
                        await self._stop_chat_shielded(session_id)
                        state.session_id = None
                        session_id = await self._ensure_session(state, history or [])
                    except AtomCodeDaemonError as exc:
                        if exc.status_code not in (404, 410) or retried:
                            raise
                        retried = True
                        await self._stop_chat_shielded(session_id)
                        state.session_id = None
                        session_id = await self._ensure_session(state, history or [])
            except (asyncio.CancelledError, GeneratorExit):
                # 客户端断开时若正挂在 yield 点（事件已产出待消费），生成器被
                # 注入的是 GeneratorExit 而非 CancelledError，两类终止信号都
                # 必须通知 daemon 停止任务，否则 bypass 模式下 agent 会继续执行。
                try:
                    await self._stop_chat_shielded(session_id)
                finally:
                    raise

    async def _chat_stream_raw(
        self,
        message: str,
        *,
        session_id: str,
        working_dir: str,
        provider: str | None,
        approval_mode: str | None,
    ) -> AsyncIterator[dict[str, Any]]:
        """底层 /chat 调用：不做 session 管理，仅解析 SSE。

        httpx 网络层异常（daemon 宕机/重启窗口）统一包装为 502 语义，
        避免生成器内异常直接穿透到适配器造成断流。
        """
        try:
            async for ev in self._chat_stream_raw_inner(
                message,
                session_id=session_id,
                working_dir=working_dir,
                provider=provider,
                approval_mode=approval_mode,
            ):
                yield ev
        except httpx.HTTPError as exc:
            raise _wrap_httpx_errors(exc) from exc

    async def _chat_stream_raw_inner(
        self,
        message: str,
        *,
        session_id: str,
        working_dir: str,
        provider: str | None,
        approval_mode: str | None,
    ) -> AsyncIterator[dict[str, Any]]:
        """_chat_stream_raw 的未包装实现（网络异常直接抛出）。"""
        request_id = str(uuid.uuid4())
        body = {
            "message": message,
            "session_id": session_id,
            "request_id": request_id,
            "working_dir": working_dir or self.default_working_dir,
            "provider": provider or self.default_provider,
            "approval_mode": approval_mode or self.approval_mode,
        }
        log.info("daemon chat start session=%s request=%s working_dir=%s", session_id, request_id, working_dir)
        for attempt in range(2):
            async with self._client.stream("POST", "/chat", json=body) as resp:
                if resp.status_code == 409:
                    raise SessionBusyError(session_id)
                if resp.status_code == 401 and attempt == 0 and await self._refresh_auth_token():
                    # daemon 重启后随机 token 已换：刷新鉴权头后重试一次
                    continue
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise AtomCodeDaemonError(f"chat failed: {resp.status_code} {text[:300]}", resp.status_code)
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        # 忽略注释行（: bye）与空行
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        log.warning("skip non-json sse payload: %r", payload[:200])
        log.info("daemon chat stream ended session=%s request=%s", session_id, request_id)
