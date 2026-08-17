from atomcode_proxy.conversation import (
    ConversationKeyResolver,
    normalize_messages,
    request_working_directory,
)


def test_resolver_reuses_conversation_when_new_request_contains_previous_history():
    resolver = ConversationKeyResolver()
    scope = "client-a"

    first = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
    ]
    key = resolver.resolve(scope, first[:-1])
    resolver.remember(scope, key, first)

    second = first + [
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]

    assert resolver.resolve(scope, second[:-1]) == key


def test_resolver_does_not_share_new_conversations_with_same_client():
    resolver = ConversationKeyResolver()
    scope = "client-a"

    first_key = resolver.resolve(scope, [{"role": "system", "content": "system"}])
    resolver.remember(
        scope,
        first_key,
        [{"role": "system", "content": "system"}, {"role": "user", "content": "one"}],
    )

    second_key = resolver.resolve(scope, [{"role": "system", "content": "system"}])

    assert second_key != first_key


def test_normalize_messages_keeps_roles_and_text_content():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": "hi"},
    ]

    assert normalize_messages(messages) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_normalize_messages_maps_unsupported_roles_to_user():
    """daemon 导入接口仅接受 user/assistant，其余角色需降级为 user 并保留语义前缀。

    回归背景：AtomCode 5.0.5 起 /sessions/{id}/messages 对 system/tool/function
    角色返回 400，导致携带这些角色的客户端对话全部失败。
    """
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "function", "content": "x", "name": "run"},
        {"role": "tool", "content": "result", "tool_call_id": "call-1"},
        {"role": "user", "content": "hello"},
    ]

    assert normalize_messages(messages) == [
        {"role": "user", "content": "[system] You are helpful."},
        {"role": "user", "content": "[function] x"},
        {"role": "user", "content": "[tool] result"},
        {"role": "user", "content": "hello"},
    ]


def test_request_working_directory_accepts_generic_client_header(tmp_path):
    path, source = request_working_directory(
        {"x-working-directory": str(tmp_path)},
        {},
        "C:/fallback",
    )

    assert path == str(tmp_path.resolve())
    assert source == "header"


def test_request_working_directory_uses_default_when_client_has_no_workspace(tmp_path):
    path, source = request_working_directory({}, {}, str(tmp_path))

    assert path == str(tmp_path.resolve())
    assert source == "default"


def test_request_working_directory_accepts_base_url_query_override(tmp_path):
    path, source = request_working_directory(
        {},
        {},
        "C:/fallback",
        {"working_dir": str(tmp_path)},
    )

    assert path == str(tmp_path.resolve())
    assert source == "query"


def test_request_working_directory_allows_override_inside_roots(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    path, source = request_working_directory(
        {"x-working-directory": str(workspace)},
        {},
        str(tmp_path),
        allowed_roots=[str(tmp_path)],
    )

    assert path == str(workspace.resolve())
    assert source == "header"


def test_request_working_directory_rejects_override_outside_roots(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    default_dir = tmp_path / "default"
    default_dir.mkdir()

    path, source = request_working_directory(
        {"x-working-directory": str(outside)},
        {},
        str(default_dir),
        allowed_roots=[str(default_dir)],
    )

    assert path == str(default_dir.resolve())
    assert source == "default"


def test_request_working_directory_rejects_query_override_outside_roots(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    default_dir = tmp_path / "default"
    default_dir.mkdir()

    path, source = request_working_directory(
        {},
        {},
        str(default_dir),
        {"working_dir": str(outside)},
        allowed_roots=[str(default_dir)],
    )

    assert path == str(default_dir.resolve())
    assert source == "default"
