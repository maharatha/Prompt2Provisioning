from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.chatgpt_auth import (
    AUTHORIZE_URL,
    MODELS_URL,
    RESOURCE,
    SCOPE,
    TOKEN_URL,
    ChatGPTAuth,
    SignInError,
    pkce_challenge,
)
from app.llm import ProviderError

ACCESS = "chatgpt-access-token-do-not-show"
REFRESH = "chatgpt-refresh-token-do-not-show"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _params(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


class _OpenAI:
    """A fake auth server and models list. It records every request it sees."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.tokens = {"access_token": ACCESS, "refresh_token": REFRESH, "id_token": "x", "expires_in": 3600}
        self.models = {"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT"}]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(self.token_status, json=self.tokens)
        if str(request.url) == MODELS_URL:
            return httpx.Response(200, json=self.models)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def form(self, index: int) -> dict[str, str]:
        return {key: values[0] for key, values in parse_qs(self.requests[index].read().decode()).items()}


def _signed_in(server: _OpenAI, clock: _Clock | None = None) -> ChatGPTAuth:
    auth = ChatGPTAuth(clock=clock or _Clock())
    params = _params(auth.start(8000))
    auth.finish("code-1", params["state"], "issued-client", client=server.client())
    return auth


def test_authorize_url_carries_the_documented_parameters() -> None:
    auth = ChatGPTAuth()
    url = auth.start(8123)
    assert url.startswith(AUTHORIZE_URL + "?")
    params = _params(url)
    assert params["client_id"] == "dynamic_agent_client"
    assert params["agent_name_hint"] == "Prompt2Provisioning"
    assert params["ext_agent_host_id"].startswith("urn:uuid:")
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == "http://127.0.0.1:8123/callback"
    assert params["scope"] == SCOPE
    assert params["resource"] == RESOURCE
    assert params["code_challenge_method"] == "S256"
    assert params["state"] and params["nonce"]
    assert params["state"] != _params(auth.start(8123))["state"]


def test_code_exchange_sends_the_verifier_that_matches_the_challenge() -> None:
    server = _OpenAI()
    auth = ChatGPTAuth()
    params = _params(auth.start(8000))
    auth.finish("code-1", params["state"], "issued-client", client=server.client())

    form = server.form(0)
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == "issued-client"
    assert form["code"] == "code-1"
    assert form["redirect_uri"] == "http://127.0.0.1:8000/callback"
    assert form["resource"] == RESOURCE
    assert pkce_challenge(form["code_verifier"]) == params["code_challenge"]
    assert auth.signed_in


def test_status_never_contains_a_token() -> None:
    server = _OpenAI()
    auth = _signed_in(server)
    auth.plan_usage(client=server.client())
    status = auth.status()
    assert status == {"signed_in": True, "models": ["gpt-6.1-sol"]}
    assert ACCESS not in repr(status) and REFRESH not in repr(auth.__dict__)


@pytest.mark.parametrize("state", [None, "not-the-state"])
def test_callback_with_a_wrong_state_is_refused(state: str | None) -> None:
    server = _OpenAI()
    auth = ChatGPTAuth()
    auth.start(8000)
    with pytest.raises(SignInError):
        auth.finish("code-1", state, "issued-client", client=server.client())
    assert server.requests == []
    assert not auth.signed_in


def test_callback_without_a_started_sign_in_is_refused() -> None:
    with pytest.raises(SignInError, match="No sign-in is waiting"):
        ChatGPTAuth().finish("code-1", "state", "issued-client")


def test_expired_sign_in_attempt_is_refused() -> None:
    clock = _Clock()
    auth = ChatGPTAuth(clock=clock)
    state = _params(auth.start(8000))["state"]
    clock.now += 601
    with pytest.raises(SignInError, match="10 minutes"):
        auth.finish("code-1", state, "issued-client", client=_OpenAI().client())


def test_a_state_works_only_once() -> None:
    server = _OpenAI()
    auth = ChatGPTAuth()
    state = _params(auth.start(8000))["state"]
    auth.finish("code-1", state, "issued-client", client=server.client())
    with pytest.raises(SignInError):
        auth.finish("code-1", state, "issued-client", client=server.client())


def test_refused_code_exchange_is_a_sign_in_error() -> None:
    server = _OpenAI()
    server.token_status = 400
    auth = ChatGPTAuth()
    state = _params(auth.start(8000))["state"]
    with pytest.raises(SignInError, match="HTTP 400"):
        auth.finish("code-1", state, "issued-client", client=server.client())
    assert not auth.signed_in


def test_missing_issued_client_id_is_refused() -> None:
    auth = ChatGPTAuth()
    state = _params(auth.start(8000))["state"]
    with pytest.raises(SignInError, match="client id"):
        auth.finish("code-1", state, None, client=_OpenAI().client())


def test_later_sign_in_reuses_the_issued_client_id() -> None:
    auth = _signed_in(_OpenAI())
    auth.sign_out()
    params = _params(auth.start(8000))
    assert params["client_id"] == "issued-client"
    assert "agent_name_hint" not in params


def test_plan_usage_returns_the_token_and_the_plan_models() -> None:
    server = _OpenAI()
    auth = _signed_in(server)
    usage = auth.plan_usage(client=server.client())
    assert usage.token == ACCESS
    assert usage.models == ("gpt-6.1-sol",)
    assert ACCESS not in repr(usage)
    assert server.requests[-1].headers["authorization"] == f"Bearer {ACCESS}"


def test_token_near_expiry_is_refreshed_and_rotated() -> None:
    server = _OpenAI()
    clock = _Clock()
    auth = _signed_in(server, clock)
    auth.plan_usage(client=server.client())
    clock.now += 3600 - 30
    server.tokens = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    assert auth.plan_usage(client=server.client()).token == "new-access"
    refresh = server.form(len(server.requests) - 1)
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["refresh_token"] == REFRESH
    assert refresh["client_id"] == "issued-client"

    clock.now += 3600 - 30
    server.tokens = {"access_token": "third-access", "expires_in": 3600}
    auth.plan_usage(client=server.client())
    assert server.form(len(server.requests) - 1)["refresh_token"] == "new-refresh"


def test_failed_refresh_signs_out() -> None:
    server = _OpenAI()
    clock = _Clock()
    auth = _signed_in(server, clock)
    clock.now += 3600
    server.token_status = 400
    with pytest.raises(ProviderError, match="Sign in with ChatGPT again"):
        auth.plan_usage(client=server.client())
    assert not auth.signed_in


def test_an_account_with_no_models_is_a_provider_error() -> None:
    server = _OpenAI()
    server.models = {"models": []}
    auth = _signed_in(server)
    with pytest.raises(ProviderError, match="no models"):
        auth.plan_usage(client=server.client())


def test_sign_out_forgets_the_session() -> None:
    auth = _signed_in(_OpenAI())
    auth.sign_out()
    assert auth.status() == {"signed_in": False, "models": []}
    with pytest.raises(ProviderError):
        auth.plan_usage()
