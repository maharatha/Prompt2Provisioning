import json

import httpx
import pytest

from app.llm import PlanUsage, PlanUsageAuthError, ProviderError, complete_plan

SECRET = "sk-demo-secret-do-not-store"


def test_openai_call_keeps_the_key_out_of_the_prompt() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"region":"us-east-1"}'}}]},
        )

    text = complete_plan(
        "openai",
        "gpt-4.1-mini",
        SECRET,
        "one database in US East",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert text == '{"region":"us-east-1"}'
    assert seen["authorization"] == f"Bearer {SECRET}"
    body = json.loads(seen["body"])
    assert body["model"] == "gpt-4.1-mini"
    assert SECRET not in seen["body"]
    assert body["messages"][1]["content"] == "one database in US East"
    instructions = body["messages"][0]["content"]
    assert "easter us" in instructions
    assert "us-east-1" in instructions
    assert "db-large" in instructions
    assert "Do not change big or large into small." in instructions
    assert "US North" in instructions
    assert "container-small" in instructions
    assert "mysql-large" in instructions
    assert "Do not rewrite the request so those checks will pass." in instructions


def test_openai_call_asks_for_json_mode() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    complete_plan(
        "openai",
        "gpt-5",
        SECRET,
        "one database",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["response_format"] == {"type": "json_object"}


def _sent_body(provider: str, model: str) -> dict[str, object]:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        if provider == "openai":
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    complete_plan(
        provider,
        model,
        SECRET,
        "one database",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    body = seen["body"]
    assert isinstance(body, dict)
    return body


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5"])
def test_claude_models_that_support_effort_get_low_effort(model: str) -> None:
    output_config = _sent_body("claude", model)["output_config"]
    assert output_config["effort"] == "low"
    assert output_config["format"]["type"] == "json_schema"


def test_claude_haiku_gets_no_effort_setting() -> None:
    # Haiku 4.5 rejects the effort parameter.
    assert "effort" not in _sent_body("claude", "claude-haiku-4-5")["output_config"]


@pytest.mark.parametrize(
    ("model", "effort"),
    [("gpt-5", "low"), ("gpt-5-mini", "low"), ("gpt-4.1", None), ("gpt-4.1-mini", None)],
)
def test_openai_reasoning_effort_only_for_reasoning_models(model: str, effort: str | None) -> None:
    body = _sent_body("openai", model)
    assert body.get("reasoning_effort") == effort


def _objects(schema: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found.extend(_objects(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_objects(value))
    return found


def test_claude_call_constrains_output_to_a_json_schema() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"region":"us-east-1"}'}]})

    text = complete_plan(
        "claude",
        "claude-sonnet-5-5",
        SECRET,
        "one database",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert text == '{"region":"us-east-1"}'
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["max_tokens"] == 16000
    output_format = body["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    schema = output_format["schema"]

    objects = _objects(schema)
    assert objects
    assert all(item["additionalProperties"] is False for item in objects)
    unsupported = {"minimum", "maximum", "exclusiveMinimum", "minLength", "maxLength", "minItems"}
    assert not unsupported & set(json.dumps(schema).replace('"', " ").split())

    properties = schema["properties"]
    assert set(properties) == {"region", "environment", "tags", "resources", "interpretation_error"}
    assert schema["required"] == []
    assert properties["environment"]["enum"] == ["dev", "test", "prod"]
    resource = properties["resources"]["items"]
    assert resource["properties"]["type"]["enum"] == ["container", "postgres", "mysql", "object_storage"]
    assert "capacity_gb" not in resource["required"]
    assert properties["tags"]["required"] == []


def _call(provider: str, payload: dict[str, object]) -> str:
    model = "gpt-5" if provider == "openai" else "claude-sonnet-5-5"
    return complete_plan(
        provider,
        model,
        SECRET,
        "one database",
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))),
    )


@pytest.mark.parametrize(
    ("provider", "payload", "message"),
    [
        (
            "claude",
            {"stop_reason": "refusal", "content": [{"type": "text", "text": "{}"}]},
            "declined",
        ),
        (
            "claude",
            {"stop_reason": "max_tokens", "content": [{"type": "text", "text": '{"region":'}]},
            "cut off",
        ),
        (
            "openai",
            {"choices": [{"finish_reason": "content_filter", "message": {"content": "{}"}}]},
            "declined",
        ),
        (
            "openai",
            {"choices": [{"finish_reason": "length", "message": {"content": '{"region":'}}]},
            "cut off",
        ),
    ],
)
def test_refused_or_truncated_replies_are_provider_errors(
    provider: str, payload: dict[str, object], message: str
) -> None:
    with pytest.raises(ProviderError, match=message):
        _call(provider, payload)


@pytest.mark.parametrize("provider", ["openai", "claude"])
def test_oversized_reply_is_a_provider_error(provider: str) -> None:
    text = "x" * (32 * 1024 + 1)
    payload: dict[str, object]
    if provider == "openai":
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
    else:
        payload = {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}
    with pytest.raises(ProviderError, match="32 KB"):
        _call(provider, payload)


@pytest.mark.parametrize("provider", ["openai", "claude"])
def test_reply_at_the_size_limit_is_returned(provider: str) -> None:
    text = "x" * (32 * 1024)
    payload: dict[str, object]
    if provider == "openai":
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
    else:
        payload = {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}
    assert _call(provider, payload) == text


def test_claude_error_redacts_the_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid {SECRET}")

    with pytest.raises(ProviderError) as caught:
        complete_plan(
            "claude",
            "claude-haiku-4-5",
            SECRET,
            "one database",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert SECRET not in str(caught.value)
    assert "401" in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_blank_key_and_unknown_model_fail_before_a_call() -> None:
    with pytest.raises(ProviderError, match="not stored"):
        complete_plan("openai", "gpt-4.1-mini", "   ", "one database")
    with pytest.raises(ProviderError, match="listed models"):
        complete_plan("openai", "gpt-nope", SECRET, "one database")


TOKEN = "chatgpt-access-token-do-not-store"
PLAN = PlanUsage(token=TOKEN, models=("gpt-6.1-sol",))


def _sse(*events: dict[str, object]) -> str:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def _delta(text: str) -> dict[str, object]:
    return {"type": "response.output_text.delta", "delta": text}


_COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}


def _plan_call(stream: str, *, status: int = 200, seen: dict[str, object] | None = None) -> str:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["authorization"] = request.headers["authorization"]
            seen["body"] = json.loads(request.read().decode())
        return httpx.Response(status, text=stream, headers={"content-type": "text/event-stream"})

    return complete_plan(
        "openai",
        "gpt-6.1-sol",
        "",
        "one database",
        plan_usage=PLAN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_chatgpt_plan_call_streams_the_responses_api() -> None:
    seen: dict[str, object] = {}
    text = _plan_call(_sse(_delta('{"region":'), _delta('"us-east-1"}'), _COMPLETED), seen=seen)
    assert text == '{"region":"us-east-1"}'
    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert seen["authorization"] == f"Bearer {TOKEN}"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["stream"] is True
    assert body["store"] is False
    assert body["model"] == "gpt-6.1-sol"
    assert body["text"] == {"format": {"type": "json_object"}}
    assert "Do not rewrite the request so those checks will pass." in body["instructions"]
    # JSON mode is refused unless an input message (not instructions) says "json".
    assert "json" in body["input"][0]["content"]
    assert body["input"][-1] == {"role": "user", "content": "one database"}
    assert all(item["role"] == "user" for item in body["input"])
    forbidden = {"temperature", "top_p", "max_output_tokens", "metadata", "truncation", "user", "messages"}
    assert not forbidden & set(body)


def test_api_key_wins_over_a_chatgpt_sign_in() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    complete_plan(
        "openai",
        "gpt-5",
        SECRET,
        "one database",
        plan_usage=PLAN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"


def test_chatgpt_plan_allows_only_the_plan_models() -> None:
    with pytest.raises(ProviderError, match="listed models"):
        complete_plan("openai", "gpt-5", "", "one database", plan_usage=PLAN)


def test_claude_ignores_a_chatgpt_sign_in() -> None:
    with pytest.raises(ProviderError, match="Enter an API key. The key is not stored."):
        complete_plan("claude", "claude-sonnet-5-5", "", "one database", plan_usage=PLAN)


def test_openai_without_key_or_sign_in_names_both_options() -> None:
    with pytest.raises(ProviderError, match="sign in with ChatGPT"):
        complete_plan("openai", "gpt-5", "", "one database")


@pytest.mark.parametrize(
    ("stream", "message"),
    [
        (_sse(_delta("{}"), {"type": "response.failed", "response": {"error": {"message": "boom"}}}), "failed"),
        (_sse(_delta('{"region":'), {"type": "response.incomplete"}), "cut off"),
        (_sse({"type": "response.refusal.delta", "delta": "no"}, _COMPLETED), "declined"),
        (_sse(_delta('{"region":"us-east-1"}')), "ended before it finished"),
        (_sse(_COMPLETED), "no text"),
        ("data: {not json}\n\n", "unreadable"),
    ],
)
def test_bad_chatgpt_plan_streams_are_provider_errors(stream: str, message: str) -> None:
    with pytest.raises(ProviderError, match=message):
        _plan_call(stream)


def test_chatgpt_plan_stream_over_32_kb_is_refused() -> None:
    chunk = "x" * 1024
    stream = _sse(*[_delta(chunk) for _ in range(33)], _COMPLETED)
    with pytest.raises(ProviderError, match="32 KB"):
        _plan_call(stream)


def test_chatgpt_plan_stream_at_the_limit_is_returned() -> None:
    chunk = "x" * 1024
    assert _plan_call(_sse(*[_delta(chunk) for _ in range(32)], _COMPLETED)) == chunk * 32


@pytest.mark.parametrize(
    ("status", "body", "error", "message"),
    [
        (429, {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}, ProviderError, "usage limit"),
        (403, {"error": {"code": "subscription_sharing_user_not_eligible"}}, ProviderError, "Plus or Pro"),
        (401, {"error": {"code": "invalid_token"}}, PlanUsageAuthError, "Sign in with ChatGPT again"),
        (500, {"error": {"message": f"bad {TOKEN}"}}, ProviderError, "HTTP 500"),
    ],
)
def test_chatgpt_plan_http_errors_are_named(
    status: int, body: dict[str, object], error: type[ProviderError], message: str
) -> None:
    with pytest.raises(error, match=message) as caught:
        _plan_call(json.dumps(body), status=status)
    assert TOKEN not in str(caught.value)


def test_chatgpt_plan_stream_error_redacts_the_token() -> None:
    stream = _sse({"type": "error", "message": f"token {TOKEN} rejected"})
    with pytest.raises(ProviderError) as caught:
        _plan_call(stream)
    assert TOKEN not in str(caught.value)
    assert "[redacted]" in str(caught.value)
