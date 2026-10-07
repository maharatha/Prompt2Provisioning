"""HTTP API for the prompt-to-provisioning planner.

One process-wide store and plan service back every request. Run a single API
worker. These handlers are async and call the service synchronously, with no
await during a create, approval, rejection, or artifact transition. This
prototype does not lock the in-memory store.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from app.llm import ProviderError, complete_plan
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


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


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
) -> PlanRecord:
    """Generate, validate, and store a plan.

    Invalid planner JSON is persisted as a draft and returned with 201.
    """
    if body.provider == "mock":
        record = service.create_plan(body.prompt)
    else:
        raw_output = complete_plan(body.provider, body.model, body.api_key, body.prompt)
        record = service.create_plan(
            body.prompt,
            raw_output=raw_output,
            generator=f"{body.provider}:{body.model}",
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
