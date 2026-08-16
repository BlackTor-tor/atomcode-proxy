"""Anthropic 兼容适配器：/v1/messages (+ /v1/models)。

Anthropic 流式事件序列（message_start -> content_block_start -> content_block_delta -> ... -> message_stop）
在此被构造为对 Cursor/Claude Code 等客户端兼容的格式。
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
from .conversation import (
    client_scope,
    explicit_conversation_id,
    request_working_directory,
    split_prompt_messages,
)
from .sse import HEARTBEAT, with_heartbeat

log = logging.getLogger("atomcode_proxy.anthropic")

router = APIRouter()

_TEXT_CHUNK_SIZE = 8


def _client_key(request: Request) -> str:
    """按客户端身份生成稳定隔离范围：auth + user-agent。

    不同客户端（Cursor / Codex / Claude Code）即使共用同一 working_dir
    也各自持有独立上下文，避免多客户端同时跑任务时串记忆。
    """
    return client_scope(request.headers, "anthropic")


def _gen_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _extract_user_text(messages: list[dict[str, Any]]) -> str:
    """Anthropic content 可能是字符串或 block 数组，取最后一条 user 消息的文本。"""
    return split_prompt_messages(messages)[0]


def _messages_with_system(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic 将 system 放在顶层，恢复 session 时转换为标准 system 消息。"""
    messages = list(body.get("messages", []))
    system = body.get("system")
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return messages


def _map_stop_reason(stop_reason: str | None) -> str:
    return {"stopped": "end_turn", "max_tokens": "max_tokens", "tool_use": "tool_use"}.get(
        stop_reason or "", "end_turn"
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _messages_to_anthropic_events(
    daemon: AtomCodeDaemon,
    message: str,
    *,
    working_dir: str,
    provider: str,
    model_reported: str,
    msg_id: str,
    client_key: str,
    conversation_key: str,
    history: list[dict[str, Any]],
) -> AsyncIterator[str]:
    message_obj = {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model_reported,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    yield _sse("message_start", {"type": "message_start", "message": message_obj})
    yield _sse(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )

    output_tokens = 0
    events = daemon.chat_with_session(
        message,
        working_dir=working_dir,
        provider=provider,
        client_key=client_key,
        conversation_key=conversation_key,
        history=history,
    )
    async for ev in with_heartbeat(events):
        if ev is HEARTBEAT:
            # 模型长时间无事件输出时的心跳：Anthropic 官方 ping 事件，客户端视为连接存活
            yield _sse("ping", {"type": "ping"})
            continue
        etype = ev.get("type")
        if etype == "text":
            content = ev.get("content", "")
            for i in range(0, len(content), _TEXT_CHUNK_SIZE):
                chunk = content[i:i + _TEXT_CHUNK_SIZE]
                output_tokens += 1
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                )
        elif etype == "done":
            stop_reason = _map_stop_reason(ev.get("stop_reason"))
            output_tokens = max(output_tokens, ev.get("tokens", 0) or output_tokens)
            yield _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": output_tokens},
                },
            )
            break

    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _sse("message_stop", {"type": "message_stop"})


async def _messages_to_anthropic_object(
    daemon: AtomCodeDaemon,
    message: str,
    *,
    working_dir: str,
    provider: str,
    model_reported: str,
    client_key: str,
    conversation_key: str,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    text_parts: list[str] = []
    stop_reason = "stopped"
    input_tokens = output_tokens = 0
    async for ev in daemon.chat_with_session(
        message,
        working_dir=working_dir,
        provider=provider,
        client_key=client_key,
        conversation_key=conversation_key,
        history=history,
    ):
        etype = ev.get("type")
        if etype == "text":
            text_parts.append(ev.get("content", ""))
        elif etype == "tokens":
            input_tokens = ev.get("prompt", 0)
            output_tokens = ev.get("completion", 0)
        elif etype == "done":
            stop_reason = ev.get("stop_reason") or stop_reason
    return {
        "id": _gen_msg_id(),
        "type": "message",
        "role": "assistant",
        "model": model_reported,
        "content": [{"type": "text", "text": "".join(text_parts)}],
        "stop_reason": _map_stop_reason(stop_reason),
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


@router.post("/v1/messages", response_model=None)
async def messages(request: Request) -> StreamingResponse | JSONResponse:
    body = await request.json()
    model_raw = body.get("model")
    provider = request.app.state.config.resolve_provider(model_raw)
    stream = bool(body.get("stream", False))
    msgs = _messages_with_system(body)
    message = _extract_user_text(msgs)
    if not message:
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request_error", "message": "empty user message"}},
        )

    daemon: AtomCodeDaemon = request.app.state.daemon
    cfg = request.app.state.config
    client_key = _client_key(request)

    prompt, history = split_prompt_messages(msgs)
    working_dir, working_dir_source = request_working_directory(
        request.headers,
        body,
        cfg.working_dir,
        request.query_params,
    )
    if not working_dir:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "working directory is not configured or does not exist",
                },
            },
        )
    resolver = request.app.state.conversation_resolver
    scope = client_scope(request.headers, provider)
    conversation_key = resolver.resolve(scope, history, explicit_conversation_id(request.headers, body))
    resolver.remember(scope, conversation_key, msgs)
    log.info("anthropic request scope=%s conversation=%s working_dir=%s source=%s", scope, conversation_key, working_dir, working_dir_source)

    model_reported = model_raw or provider
    if stream:
        gen = _messages_to_anthropic_events(
            daemon, prompt, working_dir=working_dir,
            provider=provider, model_reported=model_reported, msg_id=_gen_msg_id(),
            client_key=client_key, conversation_key=conversation_key, history=history,
        )
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        obj = await _messages_to_anthropic_object(
            daemon, prompt, working_dir=working_dir,
            provider=provider, model_reported=model_reported, client_key=client_key,
            conversation_key=conversation_key, history=history,
        )
        return JSONResponse(obj)
    except AtomCodeDaemonError as e:
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {"type": "api_error", "message": str(e)}},
        )


@router.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    daemon: AtomCodeDaemon = request.app.state.daemon
    try:
        models = await daemon.list_models()
    except AtomCodeDaemonError as e:
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {"type": "api_error", "message": str(e)}},
        )
    data = [
        {"id": m.get("provider"), "display_name": m.get("provider"), "created_at": int(time.time())}
        for m in models
    ]
    return JSONResponse({"data": data, "has_more": False, "first_id": data[0]["id"] if data else None, "last_id": data[-1]["id"] if data else None})
