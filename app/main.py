"""HTTP API for the prompt-to-provisioning planner.

One process-wide store and plan service back every request. Run a single API
worker. These handlers are async and call the service synchronously, with no
await during a create, approval, rejection, or artifact transition. This
prototype does not lock the in-memory store.
"""

import html
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from app.chatgpt_auth import ChatGPTAuth, SignInError
from app.llm import PlanUsageAuthError, ProviderError, complete_plan
from app.models import PlanRecord, ProposedPlan
from app.planner import UnknownScenarioError
from app.services import PlanNotFound, PlanService, ServiceError
from app.store import InMemoryStore

_LOG = logging.getLogger("app")


def configure_logging() -> None:
    """Write plan events to the API process stderr.

    The handler is attached once. Log lines name the plan and the outcome.
    They do not include the request body, so an API key in that body is not logged.
    """
    _LOG.setLevel(logging.INFO)
    if _LOG.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _LOG.addHandler(handler)


configure_logging()

app = FastAPI(
    title="Prompt-to-Provisioning Planner",
    version="0.1.0",
)

_store = InMemoryStore()
_plan_service = PlanService(_store)


def get_plan_service() -> PlanService:
    """Return the plan service created with this process.

    Tests override this dependency with an isolated store. The application
    does not construct a new store per request.
    """
    return _plan_service


_chatgpt_auth = ChatGPTAuth()


def get_chatgpt_auth() -> ChatGPTAuth:
    """Return this process's ChatGPT sign-in. Tests override it."""
    return _chatgpt_auth


def callback_port() -> int:
    """The host port where the browser reaches this API for the sign-in callback."""
    value = os.environ.get("OPENAI_CALLBACK_PORT", "8000")
    return int(value) if value.isdigit() else 8000


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


MAX_PROMPT_CHARS = 2000
# Enough for a resource plus at least one of size, environment, or region.
# SCENARIO: fixtures are exempt; the UI sends the bare token.
MIN_PROMPT_CHARS = 30


class CreatePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prompt: str
    provider: Literal["mock", "openai", "claude"] = "mock"
    model: str | None = None
    api_key: str | None = None

    @field_validator("prompt")
    @classmethod
    def prompt_must_be_nonblank(cls, value: str) -> str:
        if value.strip() == "":
            raise ValueError("prompt must be nonblank")
        if len(value) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
        if len(value.strip()) < MIN_PROMPT_CHARS and "SCENARIO:" not in value:
            raise ValueError(
                f"prompt must be at least {MIN_PROMPT_CHARS} characters: describe the "
                "resources and, ideally, size, environment, and region"
            )
        return value


class ApprovePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan_hash: str


_SERVICE_ERROR_RESPONSES = {
    404: {"model": ApiError},
    409: {"model": ApiError},
}


def _error_content(code: str, message: str) -> dict[str, str]:
    return ApiError(code=code, message=message).model_dump()


def _uses_chatgpt_plan(body: CreatePlanRequest, auth: ChatGPTAuth) -> bool:
    """A typed OpenAI key wins; a blank key falls back to the ChatGPT sign-in."""
    blank_key = body.api_key is None or body.api_key.strip() == ""
    return body.provider == "openai" and blank_key and auth.signed_in


def _timed_model_call(body: CreatePlanRequest, auth: ChatGPTAuth) -> str:
    """Call the model and log how long it took. The key, token, and prompt are not logged."""
    start = time.perf_counter()
    outcome = "provider_error"
    credential = "chatgpt_plan" if _uses_chatgpt_plan(body, auth) else "api_key"
    try:
        plan_usage = auth.plan_usage() if credential == "chatgpt_plan" else None
        raw_output = complete_plan(
            body.provider, body.model, body.api_key, body.prompt, plan_usage=plan_usage
        )
        outcome = "ok"
        return raw_output
    except PlanUsageAuthError:
        auth.sign_out()
        raise
    finally:
        _LOG.info(
            "model call provider=%s model=%s model_ms=%d outcome=%s credential=%s",
            body.provider,
            body.model,
            round((time.perf_counter() - start) * 1000),
            outcome,
            credential,
        )


def _log_plan(event: str, record: PlanRecord) -> None:
    _LOG.info(
        "%s plan_id=%s status=%s generator=%s",
        event,
        record.id,
        record.status.value,
        record.generator,
    )


@app.exception_handler(ServiceError)
async def handle_service_error(request: Request, exc: ServiceError) -> JSONResponse:
    _LOG.warning("%s %s refused code=%s", request.method, request.url.path, exc.code)
    return JSONResponse(
        status_code=exc.http_status,
        content=_error_content(exc.code, exc.message),
    )


@app.exception_handler(ProviderError)
async def handle_provider_error(request: Request, exc: ProviderError) -> JSONResponse:
    _LOG.warning("%s %s provider_error", request.method, request.url.path)
    return JSONResponse(
        status_code=422,
        content=_error_content("provider_error", f"provider_error: {exc.message}"),
    )


@app.exception_handler(UnknownScenarioError)
async def handle_unknown_scenario(
    request: Request,
    exc: UnknownScenarioError,
) -> JSONResponse:
    _LOG.warning("%s %s unknown_scenario", request.method, request.url.path)
    # Only this planner failure is a client input error. Other ValueErrors
    # stay unhandled so they are not reported as bad prompts.
    return JSONResponse(
        status_code=422,
        content=_error_content("unknown_scenario", str(exc)),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "prompt-to-provisioning-planner",
    }


@app.post(
    "/v1/plans",
    status_code=201,
    response_model=PlanRecord,
    responses={422: {"model": ApiError}},
)
async def create_plan(
    body: CreatePlanRequest,
    service: PlanService = Depends(get_plan_service),
    auth: ChatGPTAuth = Depends(get_chatgpt_auth),
) -> PlanRecord:
    """Generate, validate, and store a plan.

    Invalid planner JSON is persisted as a draft and returned with 201.
    """
    if body.provider == "mock":
        record = service.create_plan(body.prompt)
    else:
        source = "openai-chatgpt" if _uses_chatgpt_plan(body, auth) else body.provider
        raw_output = _timed_model_call(body, auth)
        record = service.create_plan(
            body.prompt,
            raw_output=raw_output,
            generator=f"{source}:{body.model}",
        )
    _log_plan("created", record)
    return record


@app.get(
    "/v1/plans/{plan_id}",
    response_model=PlanRecord,
    responses={404: {"model": ApiError}},
)
async def get_plan(
    plan_id: UUID,
    service: PlanService = Depends(get_plan_service),
) -> PlanRecord:
    record = service.get_plan(plan_id)
    if record is None:
        raise PlanNotFound(plan_id)
    return record


@app.post(
    "/v1/plans/{plan_id}/approve",
    response_model=PlanRecord,
    responses=_SERVICE_ERROR_RESPONSES,
)
async def approve_plan(
    plan_id: UUID,
    body: ApprovePlanRequest,
    service: PlanService = Depends(get_plan_service),
) -> PlanRecord:
    record = service.approve_plan(plan_id, body.plan_hash)
    _log_plan("approved", record)
    return record


@app.post(
    "/v1/plans/{plan_id}/reject",
    response_model=PlanRecord,
    responses=_SERVICE_ERROR_RESPONSES,
)
async def reject_plan(
    plan_id: UUID,
    service: PlanService = Depends(get_plan_service),
) -> PlanRecord:
    record = service.reject_plan(plan_id)
    _log_plan("rejected", record)
    return record


@app.post(
    "/v1/plans/{plan_id}/artifact",
    response_model=PlanRecord,
    responses=_SERVICE_ERROR_RESPONSES,
)
async def generate_artifact(
    plan_id: UUID,
    service: PlanService = Depends(get_plan_service),
) -> PlanRecord:
    record = service.generate_artifact(plan_id)
    _log_plan("artifact", record)
    return record


@app.get("/v1/decisions", response_model=list[PlanRecord])
async def list_decisions(
    service: PlanService = Depends(get_plan_service),
) -> list[PlanRecord]:
    """Return plans a reviewer has approved or rejected.

    Artifact plans are included because approval already happened.
    """
    return service.list_decisions()


def _catalog(name: str) -> dict[str, object]:
    path = Path(__file__).resolve().parent / "data" / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{name} is not a JSON object")
    return payload


@app.get("/v1/catalog/prices")
async def price_catalog() -> dict[str, object]:
    """Return the synthetic price table. Rates stay JSON strings."""
    return _catalog("prices.json")


@app.get("/v1/catalog/policy")
async def policy_catalog() -> dict[str, object]:
    """Return the policy allow-lists. The check functions still apply them."""
    return _catalog("policy.json")


@app.get("/v1/schema/plan")
async def proposed_plan_schema() -> dict[str, object]:
    return ProposedPlan.model_json_schema()


@app.get("/v1/openai/sign-in")
async def openai_sign_in(auth: ChatGPTAuth = Depends(get_chatgpt_auth)) -> dict[str, str]:
    """Start "Sign in with ChatGPT". The browser must run on this machine."""
    return {"authorize_url": auth.start(callback_port())}


@app.get("/v1/openai/session")
async def openai_session(auth: ChatGPTAuth = Depends(get_chatgpt_auth)) -> dict[str, object]:
    """Whether a ChatGPT plan is signed in, and its models. Never a token.

    The model list is fetched on first check after sign-in.
    """
    if auth.signed_in:
        try:
            auth.plan_usage()
        except ProviderError as exc:
            _LOG.warning("chatgpt session check failed")
            return {**auth.status(), "message": exc.message}
    return auth.status()


@app.post("/v1/openai/sign-out")
async def openai_sign_out(auth: ChatGPTAuth = Depends(get_chatgpt_auth)) -> dict[str, object]:
    auth.sign_out()
    _LOG.info("chatgpt signed out")
    return auth.status()


@app.get("/callback", response_class=HTMLResponse, include_in_schema=False)
async def openai_callback(
    code: str | None = None,
    state: str | None = None,
    client_id: str | None = None,
    error: str | None = None,
    auth: ChatGPTAuth = Depends(get_chatgpt_auth),
) -> HTMLResponse:
    """The loopback page OpenAI sends the browser back to after sign-in."""
    if error is not None:
        return _callback_page(f"Sign-in was not completed ({error}).", status_code=400)
    try:
        auth.finish(code, state, client_id)
    except SignInError as exc:
        _LOG.warning("chatgpt sign-in refused")
        return _callback_page(exc.message, status_code=400)
    _LOG.info("chatgpt signed in")
    return _callback_page(
        "Signed in with ChatGPT. You can close this tab. The planner page updates by itself within a few seconds.",
        status_code=200,
    )


def _callback_page(message: str, *, status_code: int) -> HTMLResponse:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Sign in with ChatGPT</title></head>"
        f"<body style='font-family:sans-serif;margin:3rem'><p>{html.escape(message)}</p></body></html>"
    )
    return HTMLResponse(body, status_code=status_code)
