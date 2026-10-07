"""Optional "Sign in with ChatGPT" for the OpenAI planner.

A ChatGPT Plus or Pro user can let this app bill model calls to their plan
instead of an API key. OpenAI's open-source flow is OAuth 2.0 with PKCE, a
loopback callback on ``127.0.0.1``, and no client secret.

The tokens live only in this API process. They are never logged, never written
to disk, and never put on a plan record. A restart means signing in again.
The ID token is not used to identify anyone, so its signature is not checked;
``state`` and the PKCE verifier protect the code exchange.

This module returns credentials only. ``app.llm`` makes the model call.
"""

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode
from uuid import uuid4

import httpx

from app.llm import PlanUsage, ProviderError

AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
MODELS_URL = "https://api.openai.com/v1/models"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "Prompt2Provisioning"
_PENDING_SECONDS = 600
_REFRESH_MARGIN_SECONDS = 60
_TIMEOUT_SECONDS = 30.0
_SIGN_IN_AGAIN = "Sign in with ChatGPT again."


class SignInError(Exception):
    """The browser sign-in did not finish. No token is part of this error."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass
class _Pending:
    state: str
    verifier: str
    redirect_uri: str
    client_id: str
    expires_at: float


@dataclass
class _Session:
    client_id: str
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float
    models: tuple[str, ...] | None = None


class ChatGPTAuth:
    """One signed-in ChatGPT account for this single-user prototype."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._host_id = f"urn:uuid:{uuid4()}"
        # The issued client id is not a secret. Reusing it on a later sign-in
        # avoids registering this app again.
        self._client_id: str | None = None
        self._pending: _Pending | None = None
        self._session: _Session | None = None

    @property
    def signed_in(self) -> bool:
        return self._session is not None

    def start(self, port: int) -> str:
        """Begin a sign-in and return the URL to open in the browser."""
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        client_id = self._client_id or DYNAMIC_CLIENT_ID
        self._pending = _Pending(
            state=state,
            verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            expires_at=self._clock() + _PENDING_SECONDS,
        )
        params = {
            "client_id": client_id,
            "ext_agent_host_id": self._host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "resource": RESOURCE,
            "state": state,
            "nonce": secrets.token_urlsafe(32),
            "code_challenge_method": "S256",
            "code_challenge": pkce_challenge(verifier),
        }
        if client_id == DYNAMIC_CLIENT_ID:
            params["agent_name_hint"] = AGENT_NAME
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def finish(
        self,
        code: str | None,
        state: str | None,
        client_id: str | None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """Exchange the callback code for tokens and keep them in memory."""
        pending = self._pending
        if pending is None or not isinstance(state, str):
            raise SignInError("No sign-in is waiting. Start again from the planner.")
        if not hmac.compare_digest(state.encode(), pending.state.encode()):
            raise SignInError("The sign-in response did not match this attempt. Start again.")
        self._pending = None
        if self._clock() > pending.expires_at:
            raise SignInError("The sign-in took longer than 10 minutes. Start again.")
        if not isinstance(code, str) or code == "":
            raise SignInError("OpenAI did not return a sign-in code. Start again.")
        issued = client_id if isinstance(client_id, str) and client_id else pending.client_id
        if issued == DYNAMIC_CLIENT_ID:
            raise SignInError("OpenAI did not return a client id. Start again.")

        try:
            payload = self._post_token(
                {
                    "grant_type": "authorization_code",
                    "client_id": issued,
                    "code": code,
                    "code_verifier": pending.verifier,
                    "redirect_uri": pending.redirect_uri,
                    "resource": RESOURCE,
                },
                client,
            )
        except ProviderError as exc:
            raise SignInError(exc.message) from exc
        self._client_id = issued
        self._session = self._session_from(issued, payload, previous_refresh=None)

    def plan_usage(self, *, client: httpx.Client | None = None) -> PlanUsage:
        """Return a fresh access token and the models this account may use."""
        session = self._session
        if session is None:
            raise ProviderError("Sign in with ChatGPT or enter an OpenAI API key.")
        if self._clock() > session.expires_at - _REFRESH_MARGIN_SECONDS:
            session = self._refresh(session, client)
        if session.models is None:
            session.models = self._list_models(session.access_token, client)
        return PlanUsage(token=session.access_token, models=session.models)

    def status(self) -> dict[str, object]:
        """What the UI may show. Never includes a token."""
        session = self._session
        if session is None:
            return {"signed_in": False, "models": []}
        return {"signed_in": True, "models": list(session.models or ())}

    def sign_out(self) -> None:
        self._session = None
        self._pending = None

    def _refresh(self, session: _Session, client: httpx.Client | None) -> _Session:
        if session.refresh_token is None:
            self.sign_out()
            raise ProviderError(f"The ChatGPT sign-in expired. {_SIGN_IN_AGAIN}")
        try:
            payload = self._post_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": session.refresh_token,
                    "client_id": session.client_id,
                    "resource": RESOURCE,
                },
                client,
            )
            refreshed = self._session_from(
                session.client_id, payload, previous_refresh=session.refresh_token
            )
        except ProviderError as exc:
            self.sign_out()
            raise ProviderError(f"The ChatGPT sign-in could not be renewed. {_SIGN_IN_AGAIN}") from exc
        refreshed.models = session.models
        self._session = refreshed
        return refreshed

    def _session_from(
        self,
        client_id: str,
        payload: Mapping[str, object],
        *,
        previous_refresh: str | None,
    ) -> _Session:
        access = payload.get("access_token")
        expires_in = payload.get("expires_in")
        refresh = payload.get("refresh_token", previous_refresh)
        if not isinstance(access, str) or access == "":
            raise ProviderError("OpenAI returned no access token.")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
            raise ProviderError("OpenAI returned no token lifetime.")
        if refresh is not None and (not isinstance(refresh, str) or refresh == ""):
            raise ProviderError("OpenAI returned an unreadable refresh token.")
        return _Session(
            client_id=client_id,
            access_token=access,
            refresh_token=refresh,
            expires_at=self._clock() + expires_in,
        )

    def _post_token(self, form: dict[str, str], client: httpx.Client | None) -> Mapping[str, object]:
        response = _send(client, "POST", TOKEN_URL, data=form)
        if response.status_code >= 400:
            raise ProviderError(f"OpenAI refused the sign-in with HTTP {response.status_code}.")
        return _json_object(response)

    def _list_models(self, token: str, client: httpx.Client | None) -> tuple[str, ...]:
        response = _send(client, "GET", MODELS_URL, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            self.sign_out()
            raise ProviderError(f"The ChatGPT sign-in was not accepted. {_SIGN_IN_AGAIN}")
        if response.status_code >= 400:
            raise ProviderError(f"OpenAI could not list your plan's models (HTTP {response.status_code}).")
        payload = _json_object(response)
        slugs = _model_slugs(payload)
        if not slugs:
            raise ProviderError("Your ChatGPT plan lists no models this app can use.")
        return slugs


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _model_slugs(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Read ``models[].slug`` (plan route) or ``data[].id`` (standard list)."""
    slugs: list[str] = []
    for key, name in (("models", "slug"), ("data", "id")):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get(name), str) and item[name]:
                slugs.append(item[name])
    return tuple(dict.fromkeys(slugs))


def _send(client: httpx.Client | None, method: str, url: str, **kwargs: object) -> httpx.Response:
    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        return http.request(method, url, **kwargs)  # type: ignore[arg-type]
    except httpx.TimeoutException as exc:
        raise ProviderError("OpenAI did not respond in time.") from exc
    except httpx.HTTPError as exc:
        raise ProviderError("OpenAI could not be reached.") from exc
    finally:
        if owns_client:
            http.close()


def _json_object(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError("OpenAI returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise ProviderError("OpenAI returned an unexpected response.")
    return payload
