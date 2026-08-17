"""上游会话标识、消息规范化和请求上下文提取。"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .workdir import normalize_working_directory

log = logging.getLogger("atomcode_proxy.conversation")


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image_url":
                    parts.append(f"[image: {block.get('image_url', '')}]")
                elif block.get("type") == "image":
                    parts.append("[image]")
                elif block.get("type") in ("tool_use", "tool_result", "thinking"):
                    parts.append(f"[{block.get('type')}] {json.dumps(block, ensure_ascii=False)}")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """规范化为 daemon 导入接口可接受的消息列表。

    daemon 的 /sessions/{id}/messages 仅接受 user/assistant 角色（AtomCode 5.0.5 起
    对 system/tool/function 等角色返回 400），因此其余角色统一降级为 user 消息，
    并以 [role] 前缀保留原始语义。
    """
    normalized: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", ""))
        content = content_to_text(message.get("content"))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            suffix = f"[tool_calls] {json.dumps(tool_calls, ensure_ascii=False)}"
            content = f"{content}\n{suffix}".strip()
        if role not in ("user", "assistant"):
            content = f"[{role or 'unknown'}] {content}".strip()
            role = "user"
        normalized.append({"role": role, "content": content})
    return normalized


def split_prompt_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """取最后一条可执行 prompt，并返回其之前的规范化历史。"""
    for index in range(len(messages) - 1, -1, -1):
        role = messages[index].get("role")
        if role not in ("user", "tool"):
            continue
        content = content_to_text(messages[index].get("content"))
        if role == "tool":
            content = f"[工具结果] {content}"
        return content, normalize_messages(messages[:index])
    return "", normalize_messages(messages)


def _is_prefix(prefix: list[dict[str, str]], values: list[dict[str, str]]) -> bool:
    return len(prefix) <= len(values) and values[: len(prefix)] == prefix


@dataclass
class _ConversationFingerprint:
    key: str
    last_request: list[dict[str, str]]
    updated_at: float
    # 指纹是否因超过 max_messages 被截断：截断的指纹退化为纯前缀匹配
    truncated: bool = False


class ConversationKeyResolver:
    """按显式 ID 或完整历史前缀识别上游逻辑会话。"""

    # 每条指纹最多保留的消息数：只保留开头一段即可满足前缀匹配，
    # 避免超长会话（MB 级 system prompt）在内存中无限放大。
    # 代价是被截断的指纹只能退化为纯前缀匹配（极长会话才会触及）。
    DEFAULT_MAX_MESSAGES_PER_ENTRY = 200

    def __init__(self, max_entries: int = 256, max_messages: int = DEFAULT_MAX_MESSAGES_PER_ENTRY) -> None:
        self.max_entries = max_entries
        self.max_messages = max_messages
        self._entries: dict[str, list[_ConversationFingerprint]] = {}
        # response_id -> conversation key 映射（FIFO 淘汰，防止无限增长）：
        # /v1/responses 每轮生成新的 resp_<hex>，客户端下轮通过
        # previous_response_id 引用，需要映射回原会话以复用 daemon session。
        self._response_keys: dict[tuple[str, str], str] = {}
        self._response_order: list[tuple[str, str]] = []

    def _entry_matches(self, entry: _ConversationFingerprint, values: list[dict[str, str]]) -> bool:
        """判断已存指纹是否属于当前历史的同一逻辑会话。

        精确延续判定：请求时的指纹 = 此前全部消息，下一轮历史 = 该消息列
        再接模型应答，因此指纹之后的第一条新消息必须是 assistant 角色。
        以此区分"本会话的延续"与"发了相同问题的另一个会话"，避免串会话。
        """
        if not _is_prefix(entry.last_request, values):
            return False
        if entry.truncated:
            return True
        remainder = values[len(entry.last_request) :]
        return not remainder or remainder[0].get("role") == "assistant"

    def _lookup_by_prefix(self, scope: str, normalized_history: list[dict[str, str]]) -> str | None:
        entries = self._entries.setdefault(scope, [])
        matches = [entry for entry in entries if self._entry_matches(entry, normalized_history)]
        if matches:
            return max(matches, key=lambda entry: (len(entry.last_request), entry.updated_at)).key
        return None

    def resolve(
        self,
        scope: str,
        history: list[dict[str, Any]],
        explicit_id: str | None = None,
    ) -> str:
        if explicit_id:
            if explicit_id.startswith("resp_"):
                # previous_response_id：优先查映射复用同一会话；
                # 映射缺失（如代理重启）时回退前缀匹配或生成新会话
                mapped = self._response_keys.get((scope, explicit_id))
                if mapped:
                    return mapped
                key = self._lookup_by_prefix(scope, normalize_messages(history))
                return key or f"generated:{scope}:{uuid.uuid4().hex}"
            return f"explicit:{scope}:{explicit_id[:256]}"

        key = self._lookup_by_prefix(scope, normalize_messages(history))
        return key or f"generated:{scope}:{uuid.uuid4().hex}"

    def remember_response(self, scope: str, response_id: str, key: str) -> None:
        """记录 response_id 与逻辑会话的映射，供下轮 previous_response_id 解析。"""
        pair = (scope, response_id)
        self._response_keys[pair] = key
        self._response_order.append(pair)
        overflow = len(self._response_order) - self.max_entries
        if overflow > 0:
            for old in self._response_order[:overflow]:
                self._response_keys.pop(old, None)
            del self._response_order[:overflow]

    def _evict_scopes(self) -> None:
        """scope 总量上限淘汰：API Key 为任意非空值，host=0.0.0.0 监听下
        局域网主机可持续更换 Authorization/User-Agent 生成新 scope，
        必须有全局上限防 _entries 无界增长（按 scope 内最新 updated_at 做 LRU）。"""
        if len(self._entries) <= self.max_entries:
            return
        scoped = [
            (max((entry.updated_at for entry in entries), default=0.0), scope)
            for scope, entries in self._entries.items()
        ]
        scoped.sort()
        for _, scope in scoped[: len(self._entries) - self.max_entries]:
            del self._entries[scope]

    def remember(self, scope: str, key: str, messages: list[dict[str, Any]]) -> None:
        # 只保留开头 max_messages 条：前缀匹配只需开头一致，截断后仍是
        # 后续请求历史的合法前缀，同时限制单条指纹内存占用。
        full = normalize_messages(messages)
        normalized = full[: self.max_messages]
        entries = self._entries.setdefault(scope, [])
        for entry in entries:
            if entry.key == key:
                entry.last_request = normalized
                entry.truncated = len(full) > self.max_messages
                entry.updated_at = time.monotonic()
                break
        else:
            entries.append(
                _ConversationFingerprint(key, normalized, time.monotonic(), truncated=len(full) > self.max_messages)
            )
        self._evict_scopes()
        if len(entries) > self.max_entries:
            entries.sort(key=lambda entry: entry.updated_at, reverse=True)
            del entries[self.max_entries :]


def client_scope(headers: Mapping[str, str], provider: str) -> str:
    auth = headers.get("authorization", "") or headers.get("x-api-key", "") or headers.get("api-key", "")
    user_agent = headers.get("user-agent", "")
    raw = f"{auth}\x00{user_agent}\x00{provider}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def explicit_conversation_id(headers: Mapping[str, str], body: Mapping[str, Any]) -> str | None:
    for name in (
        "x-atomcode-conversation-id",
        "x-conversation-id",
        "x-cursor-composer-id",
        "x-cursor-conversation-id",
    ):
        value = headers.get(name)
        if value and value.strip():
            return value.strip()

    for name in ("conversation_id", "conversationId", "session_id", "sessionId", "conversation"):
        value = body.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()

    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for name in ("conversation_id", "conversationId", "composer_id", "composerId", "session_id"):
            value = metadata.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def previous_response_id(body: Mapping[str, Any]) -> str | None:
    """提取 Responses API 的 previous_response_id（Codex 多轮会话引用）。

    单独处理而非当作会话 ID：代理每轮生成新的 resp_<hex>，
    需经 ConversationKeyResolver.remember_response 映射回原会话。
    """
    value = body.get("previous_response_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _is_within_roots(path: str, roots: Sequence[str]) -> bool:
    """判断已规范化的绝对路径是否位于任一允许根目录内（含根本身）。"""
    resolved = Path(path)
    for root in roots:
        allowed = Path(root)
        if resolved == allowed or allowed in resolved.parents:
            return True
    return False


def request_working_directory(
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    default: str | None,
    query: Mapping[str, str] | None = None,
    allowed_roots: Sequence[str] | None = None,
) -> tuple[str | None, str]:
    """按请求覆盖、协议字段、URL 参数、默认值解析工作目录。

    优先级（与 README 声明一致）：header > 请求 JSON body/metadata >
    URL 查询参数 > 默认目录。

    安全围栏：配置 allowed_roots（ATOMCODE_WORKDIR_ROOTS）后，请求级目录
    必须位于任一允许根内，越界覆盖会被忽略并回退默认目录；未配置时不限制。
    """

    def _candidate(value: str | None, source: str) -> str | None:
        """规范化并按允许根过滤候选目录；越界时忽略并回退。"""
        if not value or not value.strip():
            return None
        normalized = normalize_working_directory(value)
        if normalized and allowed_roots and not _is_within_roots(normalized, allowed_roots):
            log.warning(
                "请求级工作目录越界已忽略（source=%s, path=%s），回退默认目录",
                source,
                normalized,
            )
            return None
        return normalized

    for name in (
        "x-atomcode-working-directory",
        "x-atomcode-working-dir",
        "x-working-directory",
        "x-workspace-directory",
        "x-workspace-path",
        "x-cursor-workspace",
        "x-cursor-workspace-folder",
        "x-cursor-workspace-path",
        "x-cursor-working-directory",
        "x-codex-working-directory",
        "x-claude-code-working-directory",
    ):
        selected = _candidate(headers.get(name), "header")
        if selected:
            return selected, "header"

    # 请求 JSON body / metadata 优先于 URL 查询参数（与 README 声明一致）
    for name in ("working_dir", "workingDir", "working_directory", "cwd", "workspace_path", "workspace"):
        value = body.get(name)
        if isinstance(value, str):
            selected = _candidate(value, "body")
            if selected:
                return selected, "body"

    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for name in ("working_dir", "workingDir", "working_directory", "cwd", "workspace_path", "workspace"):
            value = metadata.get(name)
            if isinstance(value, str):
                selected = _candidate(value, "metadata")
                if selected:
                    return selected, "metadata"

    for name in ("working_dir", "working_directory", "cwd", "workspace_path"):
        selected = _candidate((query or {}).get(name), "query")
        if selected:
            return selected, "query"

    return normalize_working_directory(default), "default"
