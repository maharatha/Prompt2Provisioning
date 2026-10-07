import json

import httpx
import pytest

from app.llm import ProviderError, complete_plan

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
