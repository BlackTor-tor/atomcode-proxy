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
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Any

import httpx

log = logging.getLogger("atomcode_proxy.daemon")

BASE_HEADERS_DEFAULT = {
    "Authorization": "Bearer atomcode_webui",
    "X-AtomCode-Client": "webui",
}

# 代理只为每个逻辑会话保留一个 daemon session，避免并发请求被分配到无历史的新 session。
MAX_CONVERSATIONS = 256


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
        self._headers = {
            "Authorization": f"Bearer {daemon_token}",
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

    async def close(self) -> None:
        states = list(self._states.values())
        await asyncio.gather(
            *(self._stop_chat_shielded(state.session_id) for state in states if state.session_id),
            return_exceptions=True,
        )
        await self._client.aclose()

    async def _create_session(self, working_dir: str) -> str:
        resp = await self._client.post("/sessions", json={"working_dir": working_dir})
        if resp.status_code < 200 or resp.status_code >= 300:
            raise AtomCodeDaemonError(f"create session failed: {resp.status_code} {resp.text[:300]}", resp.status_code)
        data = resp.json()
        sid = data.get("session_id") or data.get("id")
        if not sid:
            raise AtomCodeDaemonError(f"create session: no session_id in {data!r}")
        return sid

    async def _import_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        resp = await self._client.post(
            f"/sessions/{session_id}/messages",
            json={"messages": messages},
        )
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
            # 当前任务已被取消时，继续等待独立 stop task 完成。
            await task

    async def _state_for(self, conversation_key: str, working_dir: str) -> ConversationState:
        old_session_id: str | None = None
        async with self._states_lock:
            state = self._states.get(conversation_key)
            if state is None or state.working_dir != working_dir:
                if state is not None:
                    old_session_id = state.session_id
                state = ConversationState(conversation_key, working_dir)
                self._states[conversation_key] = state
                if len(self._states) > MAX_CONVERSATIONS:
                    candidates = [
                        (key, value)
                        for key, value in self._states.items()
                        if key != conversation_key and not value.lock.locked()
                    ]
                    if candidates:
                        self._states.pop(candidates[0][0], None)
        if old_session_id:
            await self._stop_chat_shielded(old_session_id)
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
        resp = await self._client.get("/models")
        if resp.status_code != 200:
            raise AtomCodeDaemonError(f"list models failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()

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
            except asyncio.CancelledError:
                await self._stop_chat_shielded(session_id)
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
        """底层 /chat 调用：不做 session 管理，仅解析 SSE。"""
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
        async with self._client.stream("POST", "/chat", json=body) as resp:
            if resp.status_code == 409:
                raise SessionBusyError(session_id)
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
