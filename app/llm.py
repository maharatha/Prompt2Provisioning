"""Optional demo calls to OpenAI or Claude.

The result is one raw string, the same untrusted output the mock planner
returns. This module does not validate, price, approve, or store the API key.

OpenAI has two credentials. An API key calls Chat Completions. A "Sign in with
ChatGPT" token (``app.chatgpt_auth``) calls the Responses API against the
person's ChatGPT plan; that route must stream and must not store the reply.
A typed API key wins when both are present.

Every call asks the provider for JSON only: OpenAI JSON mode, and a Claude
structured-output schema. That limits what the model may write. It does not
change what it wrote, and ``app.validation`` still checks every field. The
Claude schema is a looser shape than ``ProposedPlan``: structured outputs allow
only closed objects and no numeric or length limits, so tags are limited to the
three policy keys, each optional, and quantity has no range.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.models import Environment, ResourceType

OPENAI_MODELS = ("gpt-4.1-mini", "gpt-4.1", "gpt-5-mini", "gpt-5")
CLAUDE_MODELS = ("claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5")
MODEL_CHOICES = {"openai": OPENAI_MODELS, "claude": CLAUDE_MODELS}
_DATA_DIR = Path(__file__).resolve().parent / "data"
_CLAUDE_MAX_TOKENS = 16000
# Turning a sentence into a small JSON plan is simple extraction, so ask for low
# reasoning effort: much faster, and every plan is still checked afterwards.
# Haiku 4.5 rejects "effort", and gpt-4.1 models reject "reasoning_effort".
_CLAUDE_EFFORT_MODELS = frozenset({"claude-sonnet-5-5", "claude-opus-5-5"})
_LOW_EFFORT = "low"
_TIMEOUT_SECONDS = 120.0
MAX_REPLY_BYTES = 32 * 1024
_RESPONSES_URL = "https://api.openai.com/v1/responses"
_PLAN_LIMIT_CODE = "subscription_sharing_usage_limit_exceeded"
_PLAN_NOT_ELIGIBLE_CODE = "subscription_sharing_user_not_eligible"
_JSON_REPLY_REQUEST = "Reply with one json object, as the instructions describe. The next message is the request."

_RESOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [item.value for item in ResourceType]},
        "name": {"type": "string"},
        "sku": {"type": "string"},
        "quantity": {"type": "integer"},
        "capacity_gb": {"type": "integer"},
        "public_access": {"type": "boolean"},
    },
    "required": ["type", "name", "sku", "quantity", "public_access"],
    "additionalProperties": False,
}
PLAN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string"},
        "environment": {"type": "string", "enum": [item.value for item in Environment]},
        "tags": {
            "type": "object",
            "properties": {
                "environment": {"type": "string"},
                "owner": {"type": "string"},
                "cost-center": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
        "resources": {"type": "array", "items": _RESOURCE_SCHEMA},
        "interpretation_error": {"type": "string"},
    },
    "required": [],
    "additionalProperties": False,
}


class ProviderError(Exception):
    """The model call did not return text. The API key is not part of this error."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class PlanUsageAuthError(ProviderError):
    """OpenAI no longer accepts the ChatGPT sign-in; the caller should forget it."""


@dataclass(frozen=True)
class PlanUsage:
    """A ChatGPT plan access token and the models that account may call."""

    token: str = field(repr=False)
    models: tuple[str, ...]


def complete_plan(
    provider: str,
    model: str | None,
    api_key: str | None,
    prompt: str,
    *,
    plan_usage: PlanUsage | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Ask one allowlisted model for a raw JSON string."""
    choices = MODEL_CHOICES.get(provider)
    if choices is None:
        raise ProviderError("Choose the built-in planner, OpenAI, or Claude.")
    secret = api_key.strip() if isinstance(api_key, str) else ""
    # A typed key wins. The plan's own model list is its allowlist.
    plan = plan_usage if provider == "openai" and secret == "" else None
    if plan is not None:
        choices = plan.models
    if not isinstance(model, str) or model not in choices:
        raise ProviderError("Choose one of the listed models.")
    if secret == "" and plan is None:
        if provider == "openai":
            raise ProviderError("Enter an API key or sign in with ChatGPT. The key is not stored.")
        raise ProviderError("Enter an API key. The key is not stored.")

    try:
        brief = interpretation_brief()
    except ValueError as exc:
        raise ProviderError(str(exc)) from exc

    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        if plan is not None:
            text = _openai_plan_usage(http, model, plan.token, prompt, brief)
        elif provider == "openai":
            text = _openai(http, model, secret, prompt, brief)
        else:
            text = _claude(http, model, secret, prompt, brief)
    except httpx.TimeoutException as exc:
        raise ProviderError("The model provider did not respond in time.") from exc
    except httpx.HTTPError as exc:
        raise ProviderError("The model provider could not be reached.") from exc
    finally:
        if owns_client:
            http.close()
    if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
        raise ProviderError("The model reply was larger than 32 KB and was not stored.")
    return text


def interpretation_brief() -> str:
    """Build the model prompt from the live region list and price catalog.

    The brief asks for a best guess. It does not ask the model to satisfy policy.
    """
    regions = _string_list(_read_json(_DATA_DIR / "policy.json"), "allowed_regions")
    prices = _read_json(_DATA_DIR / "prices.json")
    if not isinstance(prices, dict) or not prices:
        raise ValueError("Price catalog could not be loaded for the model brief.")
    sku_lines: list[str] = []
    for type_name, rates in prices.items():
        if not isinstance(type_name, str) or not isinstance(rates, dict) or not rates:
            raise ValueError("Price catalog could not be loaded for the model brief.")
        names = [sku for sku in rates if isinstance(sku, str) and sku]
        if not names:
            raise ValueError("Price catalog could not be loaded for the model brief.")
        sku_lines.append(f"{type_name}: {', '.join(names)}")
    return _brief_text(regions, sku_lines)


def _brief_text(regions: tuple[str, ...], sku_lines: list[str]) -> str:
    region_list = ", ".join(regions)
    sku_block = "\n".join(sku_lines)
    return f"""
You interpret a plain-language infrastructure request. Return one JSON object and no other text.
This is a best guess. Later checks decide whether the guess is allowed. Do not rewrite the request so those checks will pass.

Legal region ids: {region_list}.
Map a misspelling or synonym onto one of those ids. "easter us", "eastern us", "east coast", and "virginia" are us-east-1. "western us" and "oregon" are us-west-2. "east us 2" and "azure east" are eastus2.
When the request names no place, use us-east-1.
Return {{"interpretation_error":"Unrecognized region '<the words they used>'."}} only when the place is a different region, such as US North, Europe, or Tokyo. Do not map US North onto an allowed region.

environment is dev, test, or prod. When none is named, use dev.
tags must include non-blank environment, owner, and cost-center. When the sentence names no owner, use dev-team. When it names no cost-center, use engineering.

Resource types and the SKU names you may emit:
{sku_block}
mysql and mysql database are type mysql. postgres and database are type postgres. A couple of websites are two containers.
small maps to a small SKU, medium to a medium SKU, and big or large to a large SKU. Keep an explicit size. A big database stays db-large.
"low cost" means small only when that resource has no size word. Do not change big or large into small.
Public storage stays public_access true.
object_storage requires capacity_gb. Other types omit capacity_gb.
When no size is named, use the small SKU.
Required keys are region, environment, tags, and resources. Each resource has type, name, sku, quantity, and public_access. quantity is an integer from 1 to 100.
Do not add status, cost, policy, hash, or approval fields.
""".strip()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("The model brief could not be loaded.") from exc


def _string_list(payload: object, key: str) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        raise ValueError("The model brief could not be loaded.")
    value = payload.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError("The model brief could not be loaded.")
    return tuple(value)


def _openai(http: httpx.Client, model: str, api_key: str, prompt: str, brief: str) -> str:
    response = http.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": brief},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            **({"reasoning_effort": _LOW_EFFORT} if model.startswith("gpt-5") else {}),
        },
    )
    payload = _json_body(response, api_key)
    return _text_from_openai(payload, api_key)


def _openai_plan_usage(http: httpx.Client, model: str, token: str, prompt: str, brief: str) -> str:
    """Call the Responses API on the person's ChatGPT plan.

    The plan route requires ``stream`` and ``store: false``, takes the brief as
    ``instructions``, and rejects sampling and output-length settings. JSON mode
    also needs the word "json" in an input message; ``instructions`` does not
    count, so a fixed message asks for it and the request stays its own message.
    """
    with http.stream(
        "POST",
        _RESPONSES_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": model,
            "instructions": brief,
            "input": [
                {"role": "user", "content": _JSON_REPLY_REQUEST},
                {"role": "user", "content": prompt},
            ],
            "text": {"format": {"type": "json_object"}},
            "stream": True,
            "store": False,
        },
    ) as response:
        if response.status_code >= 400:
            response.read()
            raise _plan_usage_error(response, token)
        return _text_from_stream(response.iter_lines(), token)


def _plan_usage_error(response: httpx.Response, token: str) -> ProviderError:
    code = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else payload.get("code")
    if response.status_code == 401:
        return PlanUsageAuthError("OpenAI no longer accepts the ChatGPT sign-in. Sign in with ChatGPT again.")
    if code == _PLAN_LIMIT_CODE or response.status_code == 429:
        return ProviderError("Your ChatGPT plan's usage limit for this app was reached. Nothing was stored.")
    if code == _PLAN_NOT_ELIGIBLE_CODE:
        return ProviderError(
            "This ChatGPT account cannot use its plan here. Plus or Pro is required. "
            "Use an OpenAI API key instead."
        )
    detail = _redact(response.text[:180], token)
    return ProviderError(f"The model provider returned HTTP {response.status_code}. {detail}")


def _text_from_stream(lines: Iterable[str], token: str) -> str:
    """Join streamed output text. Only a ``response.completed`` event ends it well."""
    parts: list[str] = []
    size = 0
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderError("The model provider sent an unreadable stream event.") from exc
        if not isinstance(event, dict):
            raise ProviderError("The model provider sent an unreadable stream event.")
        kind = event.get("type")
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ProviderError("The model provider sent an unreadable stream event.")
            size += len(delta.encode("utf-8"))
            if size > MAX_REPLY_BYTES:
                raise ProviderError("The model reply was larger than 32 KB and was not stored.")
            parts.append(delta)
        elif isinstance(kind, str) and kind.startswith("response.refusal."):
            raise ProviderError("The model declined this request. Nothing was stored.")
        elif kind == "response.incomplete":
            raise ProviderError("The model reply was cut off before it finished. Nothing was stored.")
        elif kind in ("response.failed", "error"):
            raise ProviderError(f"The model provider reported a failed response. {_stream_error(event, token)}")
        elif kind == "response.completed":
            text = "".join(parts)
            if text.strip() == "":
                raise ProviderError("The model provider returned no text.")
            return _redact(text, token)
    raise ProviderError("The model reply ended before it finished. Nothing was stored.")


def _stream_error(event: Mapping[str, object], token: str) -> str:
    error = event.get("error")
    response = event.get("response")
    if not isinstance(error, dict) and isinstance(response, dict):
        error = response.get("error")
    message = error.get("message") if isinstance(error, dict) else event.get("message")
    return _redact(message[:180], token) if isinstance(message, str) else ""


def _claude(http: httpx.Client, model: str, api_key: str, prompt: str, brief: str) -> str:
    response = http.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "max_tokens": _CLAUDE_MAX_TOKENS,
            "system": brief,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {
                "format": {"type": "json_schema", "schema": PLAN_OUTPUT_SCHEMA},
                **({"effort": _LOW_EFFORT} if model in _CLAUDE_EFFORT_MODELS else {}),
            },
        },
    )
    payload = _json_body(response, api_key)
    return _text_from_claude(payload, api_key)


def _json_body(response: httpx.Response, api_key: str) -> Mapping[str, object]:
    if response.status_code >= 400:
        detail = _redact(response.text[:180], api_key)
        raise ProviderError(f"The model provider returned HTTP {response.status_code}. {detail}")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ProviderError("The model provider returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise ProviderError("The model provider returned an unexpected response.")
    return payload


def _text_from_openai(payload: Mapping[str, object], api_key: str) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("The model provider returned no text.")
    _require_complete(choices[0].get("finish_reason"), declined="content_filter", cut_off="length")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderError("The model provider returned no text.")
    return _as_text(message.get("content"), api_key)


def _require_complete(reason: object, *, declined: str, cut_off: str) -> None:
    """Refuse a reply the provider marked as declined or truncated."""
    if reason == declined:
        raise ProviderError("The model declined this request. Nothing was stored.")
    if reason == cut_off:
        raise ProviderError("The model reply was cut off before it finished. Nothing was stored.")


def _text_from_claude(payload: Mapping[str, object], api_key: str) -> str:
    _require_complete(payload.get("stop_reason"), declined="refusal", cut_off="max_tokens")
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise ProviderError("The model provider returned no text.")
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    if not parts:
        raise ProviderError("The model provider returned no text.")
    return _redact("".join(parts), api_key)


def _as_text(content: object, api_key: str) -> str:
    if isinstance(content, str) and content.strip() != "":
        return _redact(content, api_key)
    if isinstance(content, list):
        parts = [
            item.get("text")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        text = "".join(part for part in parts if isinstance(part, str))
        if text.strip() != "":
            return _redact(text, api_key)
    raise ProviderError("The model provider returned no text.")


def _redact(text: str, api_key: str) -> str:
    if api_key == "":
        return text
    return text.replace(api_key, "[redacted]")
