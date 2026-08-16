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
        {"role": "tool", "content": "result", "tool_call_id": "call-1"},
    ]

    assert normalize_messages(messages) == [
        {"role": "user", "content": "hello"},
        {"role": "tool", "content": "result", "tool_call_id": "call-1"},
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
