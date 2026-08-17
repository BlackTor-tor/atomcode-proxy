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
from .conversation import (
    client_scope,
    explicit_conversation_id,
    previous_response_id,
    request_working_directory,
    split_prompt_messages,
)
from .sse import HEARTBEAT, with_heartbeat

log = logging.getLogger("atomcode_proxy.openai")

router = APIRouter()

# text 事件是整段输出，切块模拟流式（按字符，兼容中文）
_TEXT_CHUNK_SIZE = 8


def _client_key(request: Request) -> str:
    """按客户端身份生成稳定隔离范围：auth + user-agent。

    不同客户端（Cursor / Codex / Claude Code）即使共用同一 working_dir
    也各自持有独立上下文，避免多客户端同时跑任务时串记忆。
    """
    return client_scope(request.headers, "openai")


def _gen_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _gen_resp_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def _now() -> int:
    return int(time.time())


def _extract_user_message(messages: list[dict[str, Any]]) -> str:
    """从 OpenAI messages 中取当前 prompt；历史由 session 恢复逻辑单独处理。"""
    return split_prompt_messages(messages)[0]


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
    conversation_key: str,
    history: list[dict[str, Any]],
) -> AsyncIterator[str]:
    """把 daemon SSE 流转为 OpenAI SSE 文本行。"""
    # 首块：声明 assistant 角色
    yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_reported, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"

    events = daemon.chat_with_session(
        message,
        working_dir=working_dir,
        provider=provider,
        client_key=client_key,
        conversation_key=conversation_key,
        history=history,
    )
    try:
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
            elif etype == "error":
                # daemon 侧错误（如 Provider not found）：转为代理错误流并记录日志
                raise AtomCodeDaemonError(f"daemon error: {ev.get('message', 'unknown error')}")
    except AtomCodeDaemonError as e:
        # 流式响应已开始无法返回状态码；记录错误并以文本形式告知客户端，避免静默中断
        log.error("openai stream aborted: %s", e)
        yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_reported, 'choices': [{'index': 0, 'delta': {'content': f'[proxy error] {e}'}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _chat_to_openai_object(
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
    """非流式：聚合 daemon 输出为完整 OpenAI 响应。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    stop_reason = "stopped"
    prompt_tokens = completion_tokens = 0
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
        elif etype == "reasoning":
            reasoning_parts.append(ev.get("content", ""))
        elif etype == "tokens":
            prompt_tokens = ev.get("prompt", 0)
            completion_tokens = ev.get("completion", 0)
        elif etype == "error":
            # daemon 侧错误（如 Provider not found）：浮出为 502，避免静默空响应
            raise AtomCodeDaemonError(f"daemon error: {ev.get('message', 'unknown error')}")
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


async def _parse_json_body(request: Request) -> dict[str, Any] | JSONResponse:
    """解析请求 JSON 体；非法 JSON 或非对象返回 400 而非落为 500。"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "invalid JSON body", "type": "invalid_request_error"}},
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "request body must be a JSON object", "type": "invalid_request_error"}},
        )
    return body


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> StreamingResponse | JSONResponse:
    parsed = await _parse_json_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    body: dict[str, Any] = parsed
    model_raw = body.get("model")
    daemon: AtomCodeDaemon = request.app.state.daemon
    cfg = request.app.state.config
    provider = await daemon.resolve_provider(model_raw, cfg.model_alias, cfg.default_provider)
    stream = bool(body.get("stream", False))
    messages = body.get("messages", [])
    message = _extract_user_message(messages)
    if not message:
        return JSONResponse(status_code=400, content={"error": {"message": "empty user message", "type": "invalid_request_error"}})

    client_key = _client_key(request)
    prompt, history = split_prompt_messages(messages)
    working_dir, working_dir_source = request_working_directory(
        request.headers,
        body,
        cfg.working_dir,
        request.query_params,
        allowed_roots=cfg.workdir_roots,
    )
    if not working_dir:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "working directory is not configured or does not exist", "type": "invalid_request_error"}},
        )
    resolver = request.app.state.conversation_resolver
    scope = client_scope(request.headers, provider)
    conversation_key = resolver.resolve(scope, history, explicit_conversation_id(request.headers, body))
    resolver.remember(scope, conversation_key, messages)
    log.info("openai request scope=%s conversation=%s working_dir=%s source=%s", scope, conversation_key, working_dir, working_dir_source)

    model_reported = model_raw or provider
    if stream:
        gen = _chat_to_openai_events(
            daemon, prompt, working_dir=working_dir,
            provider=provider, model_reported=model_reported,
            chat_id=_gen_id(), created=_now(), client_key=client_key,
            conversation_key=conversation_key, history=history,
        )
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        obj = await _chat_to_openai_object(
            daemon, prompt, working_dir=working_dir, provider=provider,
            model_reported=model_reported, client_key=client_key,
            conversation_key=conversation_key, history=history,
        )
        return JSONResponse(obj)
    except AtomCodeDaemonError as e:
        return JSONResponse(status_code=502, content={"error": {"message": str(e), "type": "upstream_error"}})


# ---------------------------------------------------------------------------
# OpenAI Responses API（/v1/responses）：Codex CLI 等客户端的默认接入协议
# ---------------------------------------------------------------------------


def _responses_content_text(content: Any) -> str:
    """把 Responses 的 content（字符串或类型化 block 数组）转为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "")
                if btype in ("input_text", "output_text", "text", "summary_text", "refusal"):
                    parts.append(str(block.get("text", "")))
                elif btype in ("input_image", "image", "input_file"):
                    parts.append(f"[{btype}]")
                else:
                    parts.append(f"[{btype}] {json.dumps(block, ensure_ascii=False)}")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _responses_input_to_messages(input_data: Any) -> list[dict[str, Any]]:
    """把 Responses 的 input（字符串或 Items 数组）转为消息列表。

    仅支持文本子集：message/function_call/function_call_output；
    reasoning 等模型内部 Item 跳过。返回值走统一的 normalize_messages 链路。
    """
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    messages: list[dict[str, Any]] = []
    for item in input_data or []:
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue
        itype = item.get("type", "message")
        if itype == "reasoning":
            continue
        if itype == "function_call":
            messages.append({
                "role": "assistant",
                "content": f"[function_call] {item.get('name', '')} {item.get('arguments', '')}".strip(),
            })
        elif itype == "function_call_output":
            messages.append({
                "role": "user",
                "content": f"[function_call_output] {_responses_content_text(item.get('output'))}",
            })
        else:
            role = item.get("role") or "user"
            messages.append({"role": role, "content": _responses_content_text(item.get("content"))})
    return messages


def _resp_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _responses_to_events(
    daemon: AtomCodeDaemon,
    message: str,
    *,
    working_dir: str,
    provider: str,
    model_reported: str,
    resp_id: str,
    msg_id: str,
    client_key: str,
    conversation_key: str,
    history: list[dict[str, Any]],
) -> AsyncIterator[str]:
    """把 daemon 流转为 Responses API SSE 事件序列（Codex 依赖的文本子集）。"""
    created = _now()
    base_response = {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "model": model_reported,
        "output": [],
    }
    yield _resp_sse("response.created", {"type": "response.created", "response": base_response})
    yield _resp_sse(
        "response.output_item_added",
        {
            "type": "response.output_item_added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": msg_id,
                "status": "in_progress",
                "role": "assistant",
                "model": model_reported,
                "content": [],
            },
        },
    )
    yield _resp_sse(
        "response.content_part_added",
        {
            "type": "response.content_part_added",
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    )

    text_parts: list[str] = []
    prompt_tokens = completion_tokens = 0
    events = daemon.chat_with_session(
        message,
        working_dir=working_dir,
        provider=provider,
        client_key=client_key,
        conversation_key=conversation_key,
        history=history,
    )
    try:
        async for ev in with_heartbeat(events):
            if ev is HEARTBEAT:
                # SSE 注释行保活，客户端不会渲染
                yield ": ping\n\n"
                continue
            etype = ev.get("type")
            if etype == "text":
                delta = ev.get("content", "")
                text_parts.append(delta)
                yield _resp_sse(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": msg_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": delta,
                    },
                )
            elif etype == "tokens":
                prompt_tokens = ev.get("prompt", 0)
                completion_tokens = ev.get("completion", 0)
            elif etype == "done":
                break
            elif etype == "error":
                raise AtomCodeDaemonError(f"daemon error: {ev.get('message', 'unknown error')}")
    except AtomCodeDaemonError as e:
        log.error("responses stream aborted: %s", e)
        yield _resp_sse(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    **base_response,
                    "status": "failed",
                    "error": {"code": "upstream_error", "message": str(e)},
                },
            },
        )
        return

    full_text = "".join(text_parts)
    yield _resp_sse(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "text": full_text,
        },
    )
    item = {
        "type": "message",
        "id": msg_id,
        "status": "completed",
        "role": "assistant",
        "model": model_reported,
        "content": [{"type": "output_text", "text": full_text, "annotations": []}],
    }
    yield _resp_sse("response.content_part_done", {"type": "response.content_part_done", "item_id": msg_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": full_text, "annotations": []}})
    yield _resp_sse("response.output_item_done", {"type": "response.output_item_done", "output_index": 0, "item": item})
    yield _resp_sse(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                **base_response,
                "status": "completed",
                "output": [item],
                "usage": {
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        },
    )


async def _responses_to_object(
    daemon: AtomCodeDaemon,
    message: str,
    *,
    working_dir: str,
    provider: str,
    model_reported: str,
    resp_id: str,
    msg_id: str,
    client_key: str,
    conversation_key: str,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """非流式：聚合 daemon 输出为完整 Responses 对象。"""
    text_parts: list[str] = []
    prompt_tokens = completion_tokens = 0
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
            prompt_tokens = ev.get("prompt", 0)
            completion_tokens = ev.get("completion", 0)
        elif etype == "error":
            raise AtomCodeDaemonError(f"daemon error: {ev.get('message', 'unknown error')}")
    item = {
        "type": "message",
        "id": msg_id,
        "status": "completed",
        "role": "assistant",
        "model": model_reported,
        "content": [{"type": "output_text", "text": "".join(text_parts), "annotations": []}],
    }
    return {
        "id": resp_id,
        "object": "response",
        "created_at": _now(),
        "status": "completed",
        "model": model_reported,
        "output": [item],
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "parallel_tool_calls": False,
    }


@router.post("/v1/responses", response_model=None)
async def create_response(request: Request) -> StreamingResponse | JSONResponse:
    parsed = await _parse_json_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    body: dict[str, Any] = parsed
    model_raw = body.get("model")
    daemon: AtomCodeDaemon = request.app.state.daemon
    cfg = request.app.state.config
    provider = await daemon.resolve_provider(model_raw, cfg.model_alias, cfg.default_provider)
    stream = bool(body.get("stream", False))

    messages = _responses_input_to_messages(body.get("input"))
    instructions = body.get("instructions")
    if instructions:
        messages.insert(0, {"role": "system", "content": _responses_content_text(instructions)})
    prompt, history = split_prompt_messages(messages)
    if not prompt:
        return JSONResponse(status_code=400, content={"error": {"message": "empty input", "type": "invalid_request_error"}})

    client_key = _client_key(request)
    working_dir, working_dir_source = request_working_directory(
        request.headers,
        body,
        cfg.working_dir,
        request.query_params,
        allowed_roots=cfg.workdir_roots,
    )
    if not working_dir:
        return JSONResponse(status_code=400, content={"error": {"message": "working directory is not configured or does not exist", "type": "invalid_request_error"}})
    resolver = request.app.state.conversation_resolver
    scope = client_scope(request.headers, provider)
    # previous_response_id 优先：Codex 每轮引用上一轮响应，需映射回同一逻辑会话
    # 以复用 daemon session（否则每轮新建 session 并全量导入历史）。
    prev_resp = previous_response_id(body)
    conversation_key = resolver.resolve(
        scope,
        history,
        prev_resp or explicit_conversation_id(request.headers, body),
    )
    # 提前生成本轮的 response_id 并登记映射，供客户端下轮 previous_response_id 解析
    resp_id = _gen_resp_id()
    resolver.remember_response(scope, resp_id, conversation_key)
    resolver.remember(scope, conversation_key, messages)
    log.info("responses request scope=%s conversation=%s working_dir=%s source=%s", scope, conversation_key, working_dir, working_dir_source)

    model_reported = model_raw or provider
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    if stream:
        gen = _responses_to_events(
            daemon, prompt, working_dir=working_dir,
            provider=provider, model_reported=model_reported,
            resp_id=resp_id, msg_id=msg_id,
            client_key=client_key, conversation_key=conversation_key, history=history,
        )
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        obj = await _responses_to_object(
            daemon, prompt, working_dir=working_dir, provider=provider,
            model_reported=model_reported, resp_id=resp_id, msg_id=msg_id,
            client_key=client_key, conversation_key=conversation_key, history=history,
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
