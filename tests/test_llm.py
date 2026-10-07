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
