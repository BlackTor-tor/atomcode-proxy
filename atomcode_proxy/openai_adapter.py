"""OpenAI 兼容适配器：/v1/chat/completions + /v1/models。

把 OpenAI 请求翻译为 AtomCode daemon 的 /chat SSE，再转回 OpenAI SSE 或完整 JSON。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .daemon import AtomCodeDaemon, AtomCodeDaemonError
from .sse import HEARTBEAT, with_heartbeat

log = logging.getLogger("atomcode_proxy.openai")

router = APIRouter()

# text 事件是整段输出，切块模拟流式（按字符，兼容中文）
_TEXT_CHUNK_SIZE = 8


def _client_key(request: Request) -> str:
    """按客户端身份隔离 daemon session 池：auth + user-agent。

    不同客户端（Cursor / Codex / Claude Code）即使共用同一 working_dir
    也各自持有独立上下文，避免多客户端同时跑任务时串记忆。
    """
    auth = request.headers.get("authorization", "")
    ua = request.headers.get("user-agent", "")
    return f"{auth}|{ua}"


def _gen_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _now() -> int:
    return int(time.time())


def _extract_user_message(messages: list[dict[str, Any]]) -> str:
    """从 OpenAI messages 中取要发给 daemon 的消息。

    会话历史由 daemon 侧 session 记忆，这里只取最后一条 user 消息；
    tool 调用结果合并进 user 消息，避免丢失工具输出。
    """
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "user":
            content = msg.get("content")
            if isinstance(content, list):
                # 多段 content（text/image）：只取文本段
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                return "\n".join(parts) or ""
            return content or ""
        if role == "tool":
            return f"[工具结果] {msg.get('content', '')}"
    return ""


def _map_stop_reason(stop_reason: str | None) -> str:
    return {"stopped": "stop", "max_tokens": "length", "tool_use": "tool_calls"}.get(stop_reason or "", "stop")


async def _chat_to_openai_events(
    daemon: AtomCodeDaemon,
    message: str,
    *,
    working_dir: str,
    provider: str,
    model_reported: str,
    chat_id: str,
    created: int,
    client_key: str,
) -> AsyncIterator[str]:
    """把 daemon SSE 流转为 OpenAI SSE 文本行。"""
    # 首块：声明 assistant 角色
    yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_reported, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"

    events = daemon.chat_with_session(
        message, working_dir=working_dir, provider=provider, client_key=client_key
    )
    async for ev in with_heartbeat(events):
        if ev is HEARTBEAT:
            # 模型长时间无事件输出时的心跳：SSE 注释行，客户端视为连接存活但不会渲染
            yield ": ping\n\n"
            continue
        etype = ev.get("type")
        if etype == "reasoning":
            content = ev.get("content", "")
            if content:
                yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_reported, 'choices': [{'index': 0, 'delta': {'reasoning_content': content}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
        elif etype == "text":
            content = ev.get("content", "")
            for i in range(0, len(content), _TEXT_CHUNK_SIZE):
                yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_reported, 'choices': [{'index': 0, 'delta': {'content': content[i:i + _TEXT_CHUNK_SIZE]}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
        elif etype == "done":
            finish = _map_stop_reason(ev.get("stop_reason"))
            yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_reported, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}, ensure_ascii=False)}\n\n"
            break
    yield "data: [DONE]\n\n"


async def _chat_to_openai_object(
    daemon: AtomCodeDaemon,
    message: str,
    *,
    working_dir: str,
    provider: str,
    model_reported: str,
    client_key: str,
) -> dict[str, Any]:
    """非流式：聚合 daemon 输出为完整 OpenAI 响应。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    stop_reason = "stopped"
    prompt_tokens = completion_tokens = 0
    async for ev in daemon.chat_with_session(
        message, working_dir=working_dir, provider=provider, client_key=client_key
    ):
        etype = ev.get("type")
        if etype == "text":
            text_parts.append(ev.get("content", ""))
        elif etype == "reasoning":
            reasoning_parts.append(ev.get("content", ""))
        elif etype == "tokens":
            prompt_tokens = ev.get("prompt", 0)
            completion_tokens = ev.get("completion", 0)
        elif etype == "done":
            stop_reason = ev.get("stop_reason") or stop_reason
    content = "".join(text_parts)
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_parts:
        msg["reasoning_content"] = "".join(reasoning_parts)
    return {
        "id": _gen_id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model_reported,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": _map_stop_reason(stop_reason),
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> StreamingResponse | JSONResponse:
    body = await request.json()
    model_raw = body.get("model")
    provider = request.app.state.config.resolve_provider(model_raw)
    stream = bool(body.get("stream", False))
    messages = body.get("messages", [])
    message = _extract_user_message(messages)
    if not message:
        return JSONResponse(status_code=400, content={"error": {"message": "empty user message", "type": "invalid_request_error"}})

    daemon: AtomCodeDaemon = request.app.state.daemon
    cfg = request.app.state.config
    client_key = _client_key(request)

    model_reported = model_raw or provider
    if stream:
        gen = _chat_to_openai_events(
            daemon, message, working_dir=cfg.working_dir,
            provider=provider, model_reported=model_reported,
            chat_id=_gen_id(), created=_now(), client_key=client_key,
        )
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        obj = await _chat_to_openai_object(
            daemon, message, working_dir=cfg.working_dir, provider=provider,
            model_reported=model_reported, client_key=client_key,
        )
        return JSONResponse(obj)
    except AtomCodeDaemonError as e:
        return JSONResponse(status_code=502, content={"error": {"message": str(e), "type": "upstream_error"}})


@router.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    daemon: AtomCodeDaemon = request.app.state.daemon
    try:
        models = await daemon.list_models()
    except AtomCodeDaemonError as e:
        return JSONResponse(status_code=502, content={"error": {"message": str(e), "type": "upstream_error"}})
    now = _now()
    data = [
        {
            "id": m.get("provider"),
            "object": "model",
            "created": now,
            "owned_by": "atomcode",
            "root": m.get("provider"),
        }
        for m in models
    ]
    return JSONResponse({"object": "list", "data": data})