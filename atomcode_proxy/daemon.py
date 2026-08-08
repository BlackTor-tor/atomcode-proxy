"""AtomCode 本地 daemon 的 HTTP 客户端与 SSE 事件解析。

职责：
- 管理 AtomCode session（复用/新建）
- 把 /chat 的 SSE 字节流解析为结构化事件 dict
- 对外暴露 async 接口，供 OpenAI/Anthropic 适配器调用
"""
from __future__ import annotations

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


class AtomCodeDaemonError(RuntimeError):
    """daemon 返回非 2xx 或协议错误时抛出。"""


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
        # working_dir -> session_id 的会话缓存（session 在 daemon 侧记忆上下文）
        self._sessions: dict[str, str] = {}

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- 会话 ----------

    async def ensure_session(self, working_dir: str | None = None) -> str:
        """返回该 working_dir 对应的 session_id；无则新建。"""
        wd = working_dir or self.default_working_dir
        if wd in self._sessions and self._sessions[wd]:
            return self._sessions[wd]
        sid = await self._create_session(wd)
        self._sessions[wd] = sid
        return sid

    async def _create_session(self, working_dir: str) -> str:
        resp = await self._client.post("/sessions", json={"working_dir": working_dir})
        if resp.status_code < 200 or resp.status_code >= 300:
            raise AtomCodeDaemonError(f"create session failed: {resp.status_code} {resp.text[:300]}")
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

    async def chat_stream(
        self,
        message: str,
        *,
        session_id: str,
        working_dir: str | None = None,
        provider: str | None = None,
        approval_mode: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """发送单条用户消息，yield daemon 的 SSE 事件（已解析为 dict）。"""
        body = {
            "message": message,
            "session_id": session_id,
            "request_id": str(uuid.uuid4()),
            "working_dir": working_dir or self.default_working_dir,
            "provider": provider or self.default_provider,
            "approval_mode": approval_mode or self.approval_mode,
        }
        async with self._client.stream("POST", "/chat", json=body) as resp:
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                raise AtomCodeDaemonError(f"chat failed: {resp.status_code} {text[:300]}")
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