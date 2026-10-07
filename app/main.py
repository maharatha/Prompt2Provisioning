"""HTTP API for the prompt-to-provisioning planner.

One process-wide store and plan service back every request. Run a single API
worker. These handlers are async and call the service synchronously, with no
await during a create, approval, rejection, or artifact transition. This
prototype does not lock the in-memory store.
"""

from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from app.models import PlanRecord, ProposedPlan
from app.planner import UnknownScenarioError
from app.services import PlanNotFound, PlanService, ServiceError
from app.store import InMemoryStore

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


@app.exception_handler(ServiceError)
async def handle_service_error(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=_error_content(exc.code, exc.message),
    )


@app.exception_handler(UnknownScenarioError)
async def handle_unknown_scenario(
    _request: Request,
    exc: UnknownScenarioError,
) -> JSONResponse:
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
    return service.create_plan(body.prompt)


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
    return service.approve_plan(plan_id, body.plan_hash)


@app.post(
    "/v1/plans/{plan_id}/reject",
    response_model=PlanRecord,
    responses=_SERVICE_ERROR_RESPONSES,
)
async def reject_plan(
    plan_id: UUID,
    service: PlanService = Depends(get_plan_service),
) -> PlanRecord:
    return service.reject_plan(plan_id)


@app.post(
    "/v1/plans/{plan_id}/artifact",
    response_model=PlanRecord,
    responses=_SERVICE_ERROR_RESPONSES,
)
async def generate_artifact(
    plan_id: UUID,
    service: PlanService = Depends(get_plan_service),
) -> PlanRecord:
    return service.generate_artifact(plan_id)


@app.get("/v1/schema/plan")
async def proposed_plan_schema() -> dict[str, object]:
    return ProposedPlan.model_json_schema()
