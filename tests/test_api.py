from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.artifacts import render_artifact
from app.main import app, get_plan_service
from app.models import PlanRecord, ProposedPlan
from app.services import PlanService
from app.store import InMemoryStore


class _CountingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.creates = 0

    def create(self, record: PlanRecord) -> PlanRecord:
        self.creates += 1
        return super().create(record)

EXAMPLE_PROMPT = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)
STORAGE_PROMPT = (
    "A small PostgreSQL database and two web containers and 100 gb object storage "
    "for a development team in US East, optimized for low cost."
)
MEDIUM_DEV_PROMPT = "A medium PostgreSQL database for a development team in US East."
WRONG_HASH = "a" * 64

_RECORD_FIELDS = set(PlanRecord.model_fields)


class _BoomPlanner:
    def generate(self, prompt: str) -> str:
        raise ValueError("not a scenario failure")


@pytest.fixture
def api():
    store = InMemoryStore()
    service = PlanService(store)
    app.dependency_overrides[get_plan_service] = lambda: service
    try:
        yield TestClient(app), store, service
    finally:
        app.dependency_overrides.clear()


def _assert_service_error(response, status_code: int, code: str) -> dict[str, str]:
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"code", "message"}
    assert body["code"] == code
    assert code in body["message"]
    assert "Traceback" not in response.text
    return body


def _assert_no_floats(value: object) -> None:
    if isinstance(value, float):
        raise AssertionError(f"money was encoded as float: {value}")
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_floats(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_floats(item)


def _create(client: TestClient, prompt: str) -> dict[str, object]:
    response = client.post("/v1/plans", json={"prompt": prompt})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == _RECORD_FIELDS
    _assert_no_floats(body)
    return body


def test_application_service_is_shared() -> None:
    app.dependency_overrides.clear()
    assert get_plan_service() is get_plan_service()


def test_requests_use_the_injected_store(api) -> None:
    client, _store, service = api
    created = _create(client, EXAMPLE_PROMPT)
    plan_id = UUID(str(created["id"]))

    assert service.get_plan(plan_id) is not None
    assert get_plan_service().get_plan(plan_id) is None


def test_create_retrieve_approve_artifact_and_retrieve(api) -> None:
    client, _store, _service = api
    created = _create(client, EXAMPLE_PROMPT)
    plan_id = created["id"]
    plan_hash = created["plan_hash"]

    assert created["status"] == "evaluated"
    assert created["prompt"] == EXAMPLE_PROMPT
    assert created["artifact"] is None
    assert created["validation_errors"] == []
    assert isinstance(plan_hash, str)
    cost = created["cost"]
    assert isinstance(cost, dict)
    assert cost["currency"] == "USD"
    assert cost["succeeded"] is True
    assert cost["monthly_total"] == "71"
    assert [
        (item["sku"], item["quantity"], item["unit_price"], item["amount"])
        for item in cost["line_items"]
    ] == [
        ("db-small", 1, "35", "35"),
        ("container-small", 2, "18", "36"),
    ]

    fetched = client.get(f"/v1/plans/{plan_id}")
    assert fetched.status_code == 200
    assert fetched.json()["plan_hash"] == plan_hash
    assert fetched.json()["raw_output"] == created["raw_output"]

    approved = client.post(
        f"/v1/plans/{plan_id}/approve",
        json={"plan_hash": plan_hash},
    )
    assert approved.status_code == 200
    approved_body = approved.json()
    assert approved_body["status"] == "approved"
    assert approved_body["plan_hash"] == plan_hash
    assert approved_body["artifact"] is None

    generated = client.post(f"/v1/plans/{plan_id}/artifact")
    assert generated.status_code == 200
    generated_body = generated.json()
    artifact = generated_body["artifact"]
    assert generated_body["status"] == "artifact_generated"
    assert generated_body["plan_hash"] == plan_hash
    assert isinstance(artifact, str)
    assert "Nothing was deployed." in artifact
    assert 'resource "demo_postgres" "postgres_0"' in artifact
    assert 'resource "demo_container" "container_0"' in artifact
    assert "aws_" not in artifact
    assert "azurerm_" not in artifact
    proposed = ProposedPlan.model_validate(created["proposed"])
    assert artifact == render_artifact(proposed)

    again = client.get(f"/v1/plans/{plan_id}")
    assert again.status_code == 200
    assert again.json()["status"] == "artifact_generated"
    assert again.json()["artifact"] == artifact
    assert again.json()["plan_hash"] == plan_hash


def test_fractional_usd_amounts_stay_decimal_strings(api) -> None:
    client, _store, _service = api
    created = _create(client, STORAGE_PROMPT)
    cost = created["cost"]
    assert isinstance(cost, dict)
    expected = Decimal("35") + (Decimal("18") * 2) + (Decimal("0.025") * 100)
    assert cost["currency"] == "USD"
    assert cost["succeeded"] is True
    assert cost["monthly_total"] == str(expected)
    assert cost["monthly_total"] == "73.500"
    storage = next(item for item in cost["line_items"] if item["sku"] == "storage-standard")
    assert storage["unit_price"] == "0.025"
    assert storage["amount"] == "2.500"


def test_original_prompt_text_is_preserved(api) -> None:
    client, _store, _service = api
    prompt = f"  {EXAMPLE_PROMPT}  "
    created = _create(client, prompt)
    assert created["prompt"] == prompt


def test_malformed_planner_output_is_a_draft(api) -> None:
    client, _store, _service = api
    created = _create(client, "SCENARIO:malformed")
    plan_id = created["id"]

    assert created["status"] == "draft"
    assert created["proposed"] is None
    assert created["plan_hash"] is None
    assert created["cost"] is None
    assert created["policy_checks"] == []
    assert created["validation_errors"][0]["code"] == "json_invalid"

    fetched = client.get(f"/v1/plans/{plan_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "draft"
    assert fetched.json()["validation_errors"] == created["validation_errors"]

    refused = client.post(
        f"/v1/plans/{plan_id}/approve",
        json={"plan_hash": WRONG_HASH},
    )
    error = _assert_service_error(refused, 409, "status")
    assert "evaluated" in error["message"]
    assert client.get(f"/v1/plans/{plan_id}").json()["status"] == "draft"


@pytest.mark.parametrize(
    "prompt",
    ["SCENARIO:missing_region", "SCENARIO:unknown_type"],
)
def test_schema_failures_cannot_be_approved(api, prompt: str) -> None:
    client, _store, _service = api
    created = _create(client, prompt)
    assert created["status"] == "draft"
    assert created["validation_errors"]

    refused = client.post(
        f"/v1/plans/{created['id']}/approve",
        json={"plan_hash": WRONG_HASH},
    )
    _assert_service_error(refused, 409, "status")
    assert client.get(f"/v1/plans/{created['id']}").json()["status"] == "draft"


@pytest.mark.parametrize(
    "prompt",
    ["SCENARIO:missing_tags", "SCENARIO:public_storage", "SCENARIO:excessive_qty"],
)
def test_policy_failures_cannot_be_approved(api, prompt: str) -> None:
    client, _store, _service = api
    created = _create(client, prompt)
    assert created["status"] == "evaluated"
    assert any(check["status"] == "error" for check in created["policy_checks"])

    refused = client.post(
        f"/v1/plans/{created['id']}/approve",
        json={"plan_hash": created["plan_hash"]},
    )
    _assert_service_error(refused, 409, "policy_error")
    assert client.get(f"/v1/plans/{created['id']}").json()["status"] == "evaluated"


def test_unknown_region_wording_cannot_be_approved(api) -> None:
    client, _store, _service = api
    created = _create(
        client,
        "A small PostgreSQL database and two web containers for a development team "
        "in US NORTH, optimized for low cost.",
    )
    assert created["status"] == "draft"
    assert created["proposed"] is None
    assert created["plan_hash"] is None
    messages = [issue["message"] for issue in created["validation_errors"]]
    assert any("US NORTH" in message for message in messages)
    assert all("us-east-1" not in message for message in messages)

    refused = client.post(
        f"/v1/plans/{created['id']}/approve",
        json={"plan_hash": WRONG_HASH},
    )
    _assert_service_error(refused, 409, "status")
    assert client.get(f"/v1/plans/{created['id']}").json()["status"] == "draft"


def test_big_dev_database_is_not_rewritten_as_small(api) -> None:
    client, _store, _service = api
    created = _create(
        client,
        "A big PostgreSQL database and two web containers for a development team "
        "in US East, optimized for low cost.",
    )
    assert created["status"] == "evaluated"
    proposed = created["proposed"]
    assert isinstance(proposed, dict)
    database = proposed["resources"][0]
    assert database["sku"] == "db-large"
    assert any(
        check["policy_id"] == "dev_sku_tier" and check["status"] == "passed"
        for check in created["policy_checks"]
    )
    assert any(
        check["policy_id"] == "dev_medium_cost" and check["status"] == "warning"
        for check in created["policy_checks"]
    )
    cost = created["cost"]
    assert isinstance(cost, dict)
    assert cost["succeeded"] is True
    assert cost["monthly_total"] == "276"

    approved = client.post(
        f"/v1/plans/{created['id']}/approve",
        json={"plan_hash": created["plan_hash"]},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"


def test_pricing_failure_cannot_be_approved(api) -> None:
    client, _store, _service = api
    created = _create(client, "SCENARIO:unsupported_sku")
    cost = created["cost"]
    assert created["status"] == "evaluated"
    assert isinstance(cost, dict)
    assert cost["succeeded"] is False
    assert cost["monthly_total"] == "0"
    assert cost["pricing_errors"]

    refused = client.post(
        f"/v1/plans/{created['id']}/approve",
        json={"plan_hash": created["plan_hash"]},
    )
    _assert_service_error(refused, 409, "pricing")
    assert client.get(f"/v1/plans/{created['id']}").json()["status"] == "evaluated"


def test_warning_permits_approval(api) -> None:
    client, _store, _service = api
    created = _create(client, MEDIUM_DEV_PROMPT)
    warnings = [check for check in created["policy_checks"] if check["status"] == "warning"]
    assert created["status"] == "evaluated"
    assert len(warnings) == 1
    assert warnings[0]["policy_id"] == "dev_medium_cost"
    assert all(check["status"] != "error" for check in created["policy_checks"])

    approved = client.post(
        f"/v1/plans/{created['id']}/approve",
        json={"plan_hash": created["plan_hash"]},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["plan_hash"] == created["plan_hash"]
    assert any(check["status"] == "warning" for check in approved.json()["policy_checks"])


def test_wrong_hash_and_malformed_hash_block_approval(api) -> None:
    client, _store, _service = api
    created = _create(client, EXAMPLE_PROMPT)
    plan_id = created["id"]

    wrong = client.post(f"/v1/plans/{plan_id}/approve", json={"plan_hash": WRONG_HASH})
    _assert_service_error(wrong, 409, "submitted_hash")

    malformed = client.post(f"/v1/plans/{plan_id}/approve", json={"plan_hash": "not-a-hash"})
    _assert_service_error(malformed, 409, "malformed_submitted_hash")

    missing = client.post(f"/v1/plans/{plan_id}/approve", json={"plan_hash": ""})
    _assert_service_error(missing, 409, "missing_submitted_hash")

    stored = client.get(f"/v1/plans/{plan_id}").json()
    assert stored["status"] == "evaluated"
    assert stored["plan_hash"] == created["plan_hash"]


def test_proposal_mutation_blocks_approval(api) -> None:
    client, store, _service = api
    created = _create(client, EXAMPLE_PROMPT)
    plan_id = UUID(str(created["id"]))
    _replace_first_quantity(store, plan_id)

    refused = client.post(
        f"/v1/plans/{plan_id}/approve",
        json={"plan_hash": created["plan_hash"]},
    )
    _assert_service_error(refused, 409, "current_hash")
    stored = client.get(f"/v1/plans/{plan_id}").json()
    assert stored["status"] == "evaluated"
    assert stored["plan_hash"] == created["plan_hash"]
    assert stored["proposed"]["resources"][0]["quantity"] == 2


def test_mutation_after_approval_blocks_artifact(api) -> None:
    client, store, _service = api
    created = _create(client, EXAMPLE_PROMPT)
    plan_id = UUID(str(created["id"]))
    approved = client.post(
        f"/v1/plans/{plan_id}/approve",
        json={"plan_hash": created["plan_hash"]},
    )
    assert approved.status_code == 200
    _replace_first_quantity(store, plan_id)

    refused = client.post(f"/v1/plans/{plan_id}/artifact")
    _assert_service_error(refused, 409, "current_hash")
    stored = client.get(f"/v1/plans/{plan_id}").json()
    assert stored["status"] == "approved"
    assert stored["artifact"] is None
    assert stored["plan_hash"] == created["plan_hash"]


def test_draft_and_evaluated_plans_can_be_rejected(api) -> None:
    client, _store, _service = api
    draft = _create(client, "SCENARIO:malformed")
    rejected_draft = client.post(f"/v1/plans/{draft['id']}/reject")
    assert rejected_draft.status_code == 200
    draft_body = rejected_draft.json()
    assert draft_body["status"] == "rejected"
    assert draft_body["proposed"] is None
    assert draft_body["plan_hash"] is None
    assert draft_body["validation_errors"] == draft["validation_errors"]
    assert draft_body["raw_output"] == draft["raw_output"]

    evaluated = _create(client, EXAMPLE_PROMPT)
    rejected = client.post(f"/v1/plans/{evaluated['id']}/reject")
    assert rejected.status_code == 200
    rejected_body = rejected.json()
    assert rejected_body["status"] == "rejected"
    assert rejected_body["plan_hash"] == evaluated["plan_hash"]
    assert rejected_body["cost"] == evaluated["cost"]
    assert rejected_body["policy_checks"] == evaluated["policy_checks"]
    assert rejected_body["proposed"] == evaluated["proposed"]


@pytest.mark.parametrize("prompt", ["SCENARIO:malformed", EXAMPLE_PROMPT])
def test_rejected_plan_cannot_approve_reject_or_generate(api, prompt: str) -> None:
    client, _store, _service = api
    created = _create(client, prompt)
    assert client.post(f"/v1/plans/{created['id']}/reject").status_code == 200

    approve = client.post(
        f"/v1/plans/{created['id']}/approve",
        json={"plan_hash": created["plan_hash"] or WRONG_HASH},
    )
    _assert_service_error(approve, 409, "status")
    artifact = client.post(f"/v1/plans/{created['id']}/artifact")
    _assert_service_error(artifact, 409, "status")
    again = client.post(f"/v1/plans/{created['id']}/reject")
    _assert_service_error(again, 409, "status")
    assert client.get(f"/v1/plans/{created['id']}").json()["status"] == "rejected"


def test_repeat_approval_and_artifact_generation_are_refused(api) -> None:
    client, _store, _service = api
    created = _create(client, EXAMPLE_PROMPT)
    plan_id = created["id"]
    first = client.post(f"/v1/plans/{plan_id}/approve", json={"plan_hash": created["plan_hash"]})
    assert first.status_code == 200

    second = client.post(f"/v1/plans/{plan_id}/approve", json={"plan_hash": created["plan_hash"]})
    _assert_service_error(second, 409, "status")
    assert client.get(f"/v1/plans/{plan_id}").json()["status"] == "approved"

    generated = client.post(f"/v1/plans/{plan_id}/artifact")
    assert generated.status_code == 200
    artifact = generated.json()["artifact"]

    repeated = client.post(f"/v1/plans/{plan_id}/artifact")
    _assert_service_error(repeated, 409, "status")
    approved_again = client.post(
        f"/v1/plans/{plan_id}/approve",
        json={"plan_hash": created["plan_hash"]},
    )
    _assert_service_error(approved_again, 409, "status")
    rejected = client.post(f"/v1/plans/{plan_id}/reject")
    _assert_service_error(rejected, 409, "status")

    stored = client.get(f"/v1/plans/{plan_id}").json()
    assert stored["status"] == "artifact_generated"
    assert stored["artifact"] == artifact
    assert stored["plan_hash"] == created["plan_hash"]


def test_evaluated_plan_cannot_generate_an_artifact(api) -> None:
    client, _store, _service = api
    created = _create(client, EXAMPLE_PROMPT)
    refused = client.post(f"/v1/plans/{created['id']}/artifact")
    _assert_service_error(refused, 409, "status")
    assert client.get(f"/v1/plans/{created['id']}").json()["artifact"] is None


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("post", "/approve"), ("post", "/reject"), ("post", "/artifact")],
)
def test_missing_plan_is_404(api, method: str, suffix: str) -> None:
    client, _store, _service = api
    plan_id = uuid4()
    kwargs = {"json": {"plan_hash": WRONG_HASH}} if suffix == "/approve" else {}
    response = getattr(client, method)(f"/v1/plans/{plan_id}{suffix}", **kwargs)
    error = _assert_service_error(response, 404, "not_found")
    assert str(plan_id) in error["message"]


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("post", "/approve"), ("post", "/reject"), ("post", "/artifact")],
)
def test_invalid_uuid_is_422(api, method: str, suffix: str) -> None:
    client, _store, _service = api
    kwargs = {"json": {"plan_hash": WRONG_HASH}} if suffix == "/approve" else {}
    response = getattr(client, method)(f"/v1/plans/not-a-uuid{suffix}", **kwargs)
    assert response.status_code == 422
    assert "detail" in response.json()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": "\n"},
        {"prompt": 1},
        {"prompt": None},
        {"prompt": ["database"]},
        {"prompt": True},
        {"prompt": EXAMPLE_PROMPT, "region": "us-east-1"},
    ],
)
def test_invalid_create_requests_are_422(api, payload: dict[str, object]) -> None:
    client, _store, _service = api
    response = client.post("/v1/plans", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert "detail" in body
    assert "code" not in body


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"plan_hash": 1},
        {"plan_hash": None},
        {"plan_hash": True},
        {"plan_hash": ["abc"]},
        {"plan_hash": WRONG_HASH, "force": True},
    ],
)
def test_invalid_approve_requests_are_422(api, payload: dict[str, object]) -> None:
    client, _store, _service = api
    created = _create(client, EXAMPLE_PROMPT)
    response = client.post(f"/v1/plans/{created['id']}/approve", json=payload)
    assert response.status_code == 422
    assert "detail" in response.json()
    assert client.get(f"/v1/plans/{created['id']}").json()["status"] == "evaluated"


def test_unknown_scenario_is_422(api) -> None:
    client, _store, _service = api
    counting = _CountingStore()
    app.dependency_overrides[get_plan_service] = lambda: PlanService(counting)
    response = client.post("/v1/plans", json={"prompt": "SCENARIO:nope two web containers"})
    assert response.status_code == 422
    assert response.json() == {
        "code": "unknown_scenario",
        "message": "unknown planner scenario: nope",
    }
    assert counting.creates == 0


def test_unrelated_planner_value_error_is_not_an_input_error(api) -> None:
    _client, _store, _service = api
    isolated = PlanService(InMemoryStore(), _BoomPlanner())
    app.dependency_overrides[get_plan_service] = lambda: isolated
    client = TestClient(app)
    with pytest.raises(ValueError, match="not a scenario failure"):
        client.post("/v1/plans", json={"prompt": "two web containers"})


def test_model_api_key_is_not_stored(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _service = api
    secret = "sk-demo-secret-do-not-store"

    def fake(provider: str, model: str | None, api_key: str | None, prompt: str, **_kwargs: object) -> str:
        assert provider == "openai"
        assert model == "gpt-4.1-mini"
        assert api_key == secret
        assert secret not in prompt
        return '{"interpretation_error":"Unrecognized region \'nowhere\'."}'

    monkeypatch.setattr("app.main.complete_plan", fake)
    response = client.post(
        "/v1/plans",
        json={
            "prompt": "one database in nowhere",
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": secret,
        },
    )
    assert response.status_code == 201
    assert secret not in response.text
    body = response.json()
    assert body["generator"] == "openai:gpt-4.1-mini"
    assert body["status"] == "draft"
    stored = store.get(UUID(str(body["id"])))
    assert stored is not None
    assert secret not in stored.model_dump_json()


def test_provider_failure_stores_nothing(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _service = api
    secret = "sk-demo-secret-do-not-store"

    def fake(*_args: object, **_kwargs: object) -> str:
        from app.llm import ProviderError

        raise ProviderError("The model provider returned HTTP 401.")

    monkeypatch.setattr("app.main.complete_plan", fake)
    response = client.post(
        "/v1/plans",
        json={
            "prompt": "one database",
            "provider": "claude",
            "model": "claude-sonnet-5-5",
            "api_key": secret,
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "provider_error"
    assert secret not in response.text
    assert store.list_records() == []


def test_missing_model_key_stores_nothing(api) -> None:
    client, store, _service = api
    response = client.post(
        "/v1/plans",
        json={"prompt": "one database", "provider": "openai", "model": "gpt-5"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "provider_error"
    assert store.list_records() == []


def test_decisions_keep_approved_and_rejected_plans(api) -> None:
    client, _store, _service = api
    approved = _create(client, EXAMPLE_PROMPT)
    client.post(
        f"/v1/plans/{approved['id']}/approve",
        json={"plan_hash": approved["plan_hash"]},
    )
    rejected = _create(client, "one database in US West")
    client.post(f"/v1/plans/{rejected['id']}/reject")
    draft = _create(client, "SCENARIO:malformed")

    response = client.get("/v1/decisions")
    assert response.status_code == 200
    listed = {item["id"]: item["status"] for item in response.json()}
    assert listed[approved["id"]] == "approved"
    assert listed[rejected["id"]] == "rejected"
    assert draft["id"] not in listed
    assert all("api_key" not in item for item in response.json())


def test_catalog_endpoints_return_the_json_files(api) -> None:
    client, _store, _service = api
    prices = client.get("/v1/catalog/prices")
    policy = client.get("/v1/catalog/policy")
    assert prices.status_code == 200
    assert policy.status_code == 200
    assert prices.json()["postgres"]["db-large"] == "240"
    assert "us-east-1" in policy.json()["allowed_regions"]
    assert "db-large" in policy.json()["dev_allowed_skus"]["postgres"]


def test_schema_endpoint_matches_proposed_plan_schema(api) -> None:
    client, _store, _service = api
    response = client.get("/v1/schema/plan")
    assert response.status_code == 200
    assert response.json() == ProposedPlan.model_json_schema()


def _replace_first_quantity(store: InMemoryStore, plan_id: UUID) -> None:
    record = store.get(plan_id)
    assert record is not None
    assert record.proposed is not None
    first = record.proposed.resources[0]
    updated_first = first.model_copy(update={"quantity": first.quantity + 1})
    proposed = record.proposed.model_copy(
        update={"resources": [updated_first, *record.proposed.resources[1:]]}
    )
    store.update(record.model_copy(update={"proposed": proposed}))
