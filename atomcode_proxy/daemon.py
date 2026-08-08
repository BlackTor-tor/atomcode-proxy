"""AtomCode 本地 daemon 的 HTTP 客户端与 SSE 事件解析。

职责：
- 管理 AtomCode session 池（并发请求隔离，避免 409 session_busy）
- 把 /chat 的 SSE 字节流解析为结构化事件 dict
- 对外暴露 async 接口，供 OpenAI/Anthropic 适配器调用
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator, Any

import httpx

log = logging.getLogger("atomcode_proxy.daemon")

BASE_HEADERS_DEFAULT = {
    "Authorization": "Bearer atomcode_webui",
    "X-AtomCode-Client": "webui",
}

# 每个 working_dir 的 session 池上限，超出淘汰最旧的
MAX_POOL_SIZE = 8


class AtomCodeDaemonError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SessionBusyError(AtomCodeDaemonError):
    """daemon 返回 409：该 session 正在执行其他 chat 操作。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session busy: {session_id}", 409)
        self.session_id = session_id


class SessionPool:
    """按 working_dir 维护多个 session，并发请求各拿一个空闲 session。

    串行请求复用同一 session（保住上下文），并发请求自动分到不同 session
    （避免 daemon 侧 409 session_busy）。
    """

    def __init__(self, daemon: "AtomCodeDaemon") -> None:
        self._daemon = daemon
        self._lock = asyncio.Lock()
        self._pools: dict[str, list[str]] = {}  # working_dir -> [session_id...]
        self._busy: set[str] = set()            # 正在被使用的 session_id

    async def acquire(self, working_dir: str) -> str:
        """返回一个空闲 session；全忙或池空则新建。"""
        async with self._lock:
            pool = self._pools.setdefault(working_dir, [])
            for sid in pool:
                if sid not in self._busy:
                    self._busy.add(sid)
                    return sid
            sid = await self._daemon._create_session(working_dir)
            pool.append(sid)
            if len(pool) > MAX_POOL_SIZE:
                stale = pool.pop(0)
                self._busy.discard(stale)
            self._busy.add(sid)
            return sid

    def release(self, session_id: str) -> None:
        self._busy.discard(session_id)

    async def discard(self, working_dir: str, session_id: str) -> None:
        """把坏 session（busy 卡死等）移出池，下次请求不再复用。"""
        async with self._lock:
            pool = self._pools.get(working_dir, [])
            if session_id in pool:
                pool.remove(session_id)
            self._busy.discard(session_id)


class AtomCodeDaemon:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:13456",
        *,
        daemon_token: str = "atomcode_webui",
        default_provider: str = "AtomGit-deepseek-v4-flash",
        approval_mode: str = "bypass",
        default_working_dir: str = ".",
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_provider = default_provider
        self.approval_mode = approval_mode
        self.default_working_dir = default_working_dir
        self._headers = {
            "Authorization": f"Bearer {daemon_token}",
            "X-AtomCode-Client": "webui",
        }
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        self._pool = SessionPool(self)

    async def close(self) -> None:
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
        provider: str | None = None,
        approval_mode: str | None = None,
        max_retries: int = 3,
    ) -> AsyncIterator[dict[str, Any]]:
        """从池中取一个空闲 session 发消息，yield daemon 的 SSE 事件。

        并发请求自动分到不同 session；遇到 409 session_busy 时丢弃该
        session 并换新的重试（daemon 侧单 session 只允许一个 chat 操作）。
        """
        wd = working_dir or self.default_working_dir
        last_err: AtomCodeDaemonError | None = None
        for attempt in range(max_retries):
            session_id = await self._pool.acquire(wd)
            try:
                async for ev in self._chat_stream_raw(
                    message,
                    session_id=session_id,
                    working_dir=wd,
                    provider=provider,
                    approval_mode=approval_mode,
                ):
                    yield ev
                return
            except SessionBusyError as e:
                log.warning("session busy (%s), switching session, attempt=%d", session_id, attempt + 1)
                await self._pool.discard(wd, session_id)
                last_err = e
                continue
            finally:
                self._pool.release(session_id)
        if last_err is not None:
            raise last_err

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
        body = {
            "message": message,
            "session_id": session_id,
            "request_id": str(uuid.uuid4()),
            "working_dir": working_dir or self.default_working_dir,
            "provider": provider or self.default_provider,
            "approval_mode": approval_mode or self.approval_mode,
        }
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