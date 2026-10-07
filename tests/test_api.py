import json
import logging
import re
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.artifacts import render_artifact
from app.chatgpt_auth import ChatGPTAuth
from app.llm import PlanUsage, PlanUsageAuthError, ProviderError
from app.main import app, get_chatgpt_auth, get_plan_service
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
    auth = ChatGPTAuth()
    app.dependency_overrides[get_plan_service] = lambda: service
    app.dependency_overrides[get_chatgpt_auth] = lambda: auth
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
    ["SCENARIO:missing_region", "SCENARIO:unknown_type", "SCENARIO:extra_fields"],
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
    [
        "SCENARIO:missing_tags",
        "SCENARIO:public_storage",
        "SCENARIO:excessive_qty",
        "SCENARIO:bad_region",
    ],
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


def test_planner_claiming_approval_is_a_schema_failure(api) -> None:
    client, _store, _service = api
    created = _create(client, "SCENARIO:extra_fields")
    assert created["status"] == "draft"
    assert created["proposed"] is None
    assert created["plan_hash"] is None
    assert created["cost"] is None
    assert sorted((issue["code"], issue["field_path"]) for issue in created["validation_errors"]) == [
        ("extra_forbidden", "cost"),
        ("extra_forbidden", "status"),
    ]


def test_unlisted_region_is_a_policy_error_only(api) -> None:
    client, _store, _service = api
    created = _create(client, "SCENARIO:bad_region")
    assert created["status"] == "evaluated"
    assert created["cost"]["succeeded"] is True
    errors = [check["policy_id"] for check in created["policy_checks"] if check["status"] == "error"]
    assert errors == ["allowed_regions"]


def test_prompt_minimum_is_thirty_characters_and_named_in_the_error(api) -> None:
    client, store, _service = api
    refused = client.post("/v1/plans", json={"prompt": "two web containers in US East"})
    assert len("two web containers in US East") == 29
    assert refused.status_code == 422
    assert "at least 30 characters" in refused.text
    assert store.list_records() == []

    accepted = _create(client, "two web containers in US East.")
    assert accepted["status"] == "evaluated"


def test_scenario_tokens_are_exempt_from_the_minimum(api) -> None:
    client, _store, _service = api
    assert _create(client, "SCENARIO:malformed")["status"] == "draft"


def test_prompt_at_the_length_limit_is_accepted(api) -> None:
    client, store, _service = api
    prompt = EXAMPLE_PROMPT + " " + "x" * (2000 - len(EXAMPLE_PROMPT) - 1)
    assert len(prompt) == 2000
    assert _create(client, prompt)["status"] == "evaluated"

    refused = client.post("/v1/plans", json={"prompt": prompt + "x"})
    assert refused.status_code == 422
    assert len(store.list_records()) == 1


def _model_plan(resources: list[dict[str, object]], *, region: str = "us-east-1") -> str:
    return json.dumps(
        {
            "region": region,
            "environment": "dev",
            "tags": {"environment": "dev", "owner": "dev-team", "cost-center": "engineering"},
            "resources": resources,
        }
    )


_DB_SMALL = {"type": "postgres", "name": "database", "sku": "db-small", "quantity": 1, "public_access": False}
_TWO_WEB = {"type": "container", "name": "web", "sku": "container-small", "quantity": 2, "public_access": False}


def test_built_in_planner_plans_carry_no_interpretation_notes(api) -> None:
    client, _store, _service = api
    assert _create(client, EXAMPLE_PROMPT)["interpretation_notes"] == []


def test_model_plan_matching_the_built_in_reading_says_so(api) -> None:
    client, _store, service = api
    record = service.create_plan(
        EXAMPLE_PROMPT,
        raw_output=_model_plan([_DB_SMALL, _TWO_WEB]),
        generator="claude:claude-sonnet-5-5",
    )
    assert record.interpretation_notes == ["Matches the built-in planner's reading of the request."]
    assert client.get(f"/v1/plans/{record.id}").json()["interpretation_notes"] == record.interpretation_notes


def test_model_plan_that_adds_or_changes_resources_is_flagged_without_blocking(api) -> None:
    client, _store, service = api
    bucket = {
        "type": "object_storage",
        "name": "bucket",
        "sku": "storage-standard",
        "quantity": 1,
        "capacity_gb": 100,
        "public_access": False,
    }
    three_medium_web = {**_TWO_WEB, "sku": "container-medium", "quantity": 3}
    record = service.create_plan(
        EXAMPLE_PROMPT,
        raw_output=_model_plan([_DB_SMALL, three_medium_web, bucket], region="us-west-2"),
        generator="openai:gpt-5",
    )
    assert record.status.value == "evaluated"
    assert record.interpretation_notes == [
        "Region: the model chose us-west-2; the built-in reading is us-east-1.",
        "container: the model has 3 × container-medium; the built-in reading has 2 × container-small.",
        "object_storage: the model added 1 × storage-standard (100 GB); the request may not ask for it.",
    ]
    approved = client.post(f"/v1/plans/{record.id}/approve", json={"plan_hash": record.plan_hash})
    assert approved.status_code == 200


def test_model_plan_that_drops_a_requested_resource_is_flagged(api) -> None:
    _client, _store, service = api
    record = service.create_plan(
        EXAMPLE_PROMPT,
        raw_output=_model_plan([_TWO_WEB]),
        generator="claude:claude-haiku-4-5",
    )
    assert record.interpretation_notes == [
        "postgres: the model left out 1 × db-small, which the built-in reading found.",
    ]


def test_model_plan_is_not_compared_when_the_built_in_planner_cannot_read_the_prompt(api) -> None:
    _client, _store, service = api
    record = service.create_plan(
        "Somewhere to keep customer records in US East",
        raw_output=_model_plan([{**_DB_SMALL, "quantity": 2}]),
        generator="openai:gpt-5",
    )
    assert record.interpretation_notes == [
        "Not compared: the built-in planner could not read this request. "
        "Check the plan against the request yourself.",
    ]

    scenario = service.create_plan(
        "SCENARIO:malformed and two databases",
        raw_output=_model_plan([_DB_SMALL]),
        generator="openai:gpt-5",
    )
    assert scenario.interpretation_notes[0].startswith("Not compared:")


def test_model_draft_has_no_interpretation_notes(api) -> None:
    _client, _store, service = api
    record = service.create_plan(EXAMPLE_PROMPT, raw_output="not json", generator="openai:gpt-5")
    assert record.status.value == "draft"
    assert record.interpretation_notes == []


def test_corrected_typo_is_shown_on_the_built_in_plan(api) -> None:
    client, _store, _service = api
    created = _create(client, "I wnat to build a dataabse and container")
    assert created["status"] == "evaluated"
    assert [resource["type"] for resource in created["proposed"]["resources"]] == [
        "postgres",
        "container",
    ]
    assert created["interpretation_notes"] == [
        "Read 'dataabse' as 'database' (likely typo, one letter off)."
    ]
    assert created["cost"]["monthly_total"] == "53"


def test_defaults_are_called_out_when_the_request_omits_them(api) -> None:
    client, _store, _service = api
    created = _create(client, "I need a container and a database please")
    assert created["status"] == "evaluated"
    assert created["defaults_applied"] == [
        "Region: the request names none, so the plan uses us-east-1.",
        "Environment: the request names none, so the plan uses dev.",
        "Size: none given for database (postgres), so it uses db-small, the smallest tier.",
        "Size: none given for web (container), so it uses container-small, the smallest tier.",
        "Tag owner: none named, so the plan uses dev-team.",
        "Tag cost-center: none named, so the plan uses engineering.",
    ]


def test_fully_specified_request_only_reports_tag_defaults(api) -> None:
    client, _store, _service = api
    created = _create(client, EXAMPLE_PROMPT)
    assert created["defaults_applied"] == [
        "Tag owner: none named, so the plan uses dev-team.",
        "Tag cost-center: none named, so the plan uses engineering.",
    ]


def test_built_in_planner_says_when_it_ignores_a_named_owner(api) -> None:
    client, _store, _service = api
    created = _create(client, "a small container in prod in iad owned by payments")
    assert created["defaults_applied"] == [
        "Tag owner: the request names an owner, but the built-in planner does not read owners, "
        "so the plan uses dev-team.",
        "Tag cost-center: none named, so the plan uses engineering.",
    ]


def test_built_in_planner_says_when_it_ignores_a_named_cost_center(api) -> None:
    client, _store, _service = api
    created = _create(client, "a small container in prod in iad, cost center 42")
    assert (
        "Tag cost-center: the request names a cost center, but the built-in planner does not "
        "read cost-centers, so the plan uses engineering." in created["defaults_applied"]
    )


def test_storage_capacity_default_is_called_out(api) -> None:
    client, _store, _service = api
    created = _create(client, "object storage in prod in iad, owner and cost center are TBD")
    assert "Capacity: none given for bucket (object_storage), so it uses 100 GB." in created["defaults_applied"]


def test_model_plan_defaults_are_called_out_too(api) -> None:
    _client, _store, service = api
    record = service.create_plan(
        "I need a container and a database please",
        raw_output=_model_plan([_DB_SMALL, {**_TWO_WEB, "quantity": 1}]),
        generator="openai:gpt-5",
    )
    assert record.defaults_applied[:2] == [
        "Region: the request names none, so the plan uses us-east-1.",
        "Environment: the request names none, so the plan uses dev.",
    ]
    # A model may read owners from the request, so no "does not read owners" note.
    assert "Tag owner: none named, so the plan uses dev-team." in record.defaults_applied


def test_drafts_and_scenarios_report_no_defaults(api) -> None:
    client, _store, _service = api
    assert _create(client, "Build a rocket for the launch next week")["defaults_applied"] == []
    assert _create(client, "SCENARIO:missing_tags")["defaults_applied"] == []


def test_distant_typo_is_a_draft_not_a_partial_plan(api) -> None:
    client, _store, _service = api
    created = _create(client, "A dataabes and a container for the team")
    assert created["status"] == "draft"
    [issue] = created["validation_errors"]
    assert (issue["code"], issue["field_path"]) == ("unrecognized_input", "resources")
    assert "'dataabes' (did you mean 'database'?)" in issue["message"]
    refused = client.post(f"/v1/plans/{created['id']}/approve", json={"plan_hash": WRONG_HASH})
    _assert_service_error(refused, 409, "status")


def test_unsupported_resource_is_refused_not_dropped(api) -> None:
    client, _store, _service = api
    created = _create(client, "A redis cache and two containers in US East")
    assert created["status"] == "draft"
    assert "cannot provision the resource 'redis'" in created["validation_errors"][0]["message"]


def test_unrecognized_resource_wording_cannot_be_approved(api) -> None:
    client, _store, _service = api
    created = _create(client, "Build a rocket for the launch next week.")
    assert created["status"] == "draft"
    assert created["proposed"] is None
    assert created["plan_hash"] is None
    assert [(issue["code"], issue["field_path"]) for issue in created["validation_errors"]] == [
        ("unrecognized_input", "resources"),
    ]

    refused = client.post(
        f"/v1/plans/{created['id']}/approve",
        json={"plan_hash": WRONG_HASH},
    )
    _assert_service_error(refused, 409, "status")
    assert client.get(f"/v1/plans/{created['id']}").json()["status"] == "draft"


def test_the_word_us_still_evaluates_and_prices(api) -> None:
    client, _store, _service = api
    created = _create(client, "Give us two web containers in US East")
    assert created["status"] == "evaluated"
    assert created["cost"]["monthly_total"] == "36"


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
        {"prompt": "x" * 2001},
        {"prompt": "two containers"},
        {"prompt": "  " + "x" * 29 + "   "},
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
        client.post("/v1/plans", json={"prompt": "two web containers for the storefront"})


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
            "prompt": "one database in nowhere for reporting",
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
            "prompt": "one database for the reporting service",
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
        json={"prompt": "one database for the reporting service", "provider": "openai", "model": "gpt-5"},
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
    rejected = _create(client, "one database in US West for reporting")
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


def test_plan_actions_are_logged_without_the_api_key(api, caplog: pytest.LogCaptureFixture) -> None:
    client, _store, _service = api
    secret = "sk-demo-secret-do-not-log"
    caplog.set_level(logging.INFO, logger="app")

    created = _create(client, EXAMPLE_PROMPT)
    plan_id = str(created["id"])
    approved = client.post(
        f"/v1/plans/{plan_id}/approve",
        json={"plan_hash": created["plan_hash"]},
    )
    assert approved.status_code == 200
    artifact = client.post(f"/v1/plans/{plan_id}/artifact")
    assert artifact.status_code == 200
    refused = client.post(f"/v1/plans/{plan_id}/reject")
    assert refused.status_code == 409

    text = caplog.text
    assert f"created plan_id={plan_id} status=evaluated generator=mock" in text
    assert f"approved plan_id={plan_id} status=approved generator=mock" in text
    assert f"artifact plan_id={plan_id} status=artifact_generated generator=mock" in text
    assert "refused code=status" in text
    assert secret not in text
    assert "api_key" not in text


def test_provider_failure_log_omits_the_api_key(
    api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, _store, _service = api
    secret = "sk-demo-secret-do-not-log"
    caplog.set_level(logging.INFO, logger="app")

    def fake(*_args: object, **_kwargs: object) -> str:
        raise ProviderError(f"The model provider returned HTTP 401. {secret}")

    monkeypatch.setattr("app.main.complete_plan", fake)
    response = client.post(
        "/v1/plans",
        json={
            "prompt": "one database for the reporting service",
            "provider": "openai",
            "model": "gpt-5",
            "api_key": secret,
        },
    )
    assert response.status_code == 422
    assert "provider_error" in caplog.text
    assert secret not in caplog.text


def test_model_call_duration_is_logged_without_the_key(
    api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, _store, _service = api
    secret = "sk-demo-secret-do-not-log"
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr("app.main.complete_plan", lambda *_args, **_kwargs: _model_plan([_DB_SMALL]))
    response = client.post(
        "/v1/plans",
        json={
            "prompt": EXAMPLE_PROMPT,
            "provider": "claude",
            "model": "claude-sonnet-5-5",
            "api_key": secret,
        },
    )
    assert response.status_code == 201
    assert re.search(r"model call provider=claude model=claude-sonnet-5-5 model_ms=\d+ outcome=ok", caplog.text)
    assert secret not in caplog.text


def test_failed_model_call_duration_is_logged(
    api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, _store, _service = api
    caplog.set_level(logging.INFO, logger="app")

    def fail(*_args: object, **_kwargs: object) -> str:
        raise ProviderError("The model provider did not respond in time.")

    monkeypatch.setattr("app.main.complete_plan", fail)
    response = client.post(
        "/v1/plans",
        json={"prompt": EXAMPLE_PROMPT, "provider": "openai", "model": "gpt-5", "api_key": "k"},
    )
    assert response.status_code == 422
    assert re.search(r"model call provider=openai model=gpt-5 model_ms=\d+ outcome=provider_error", caplog.text)


CHATGPT_TOKEN = "chatgpt-access-token-do-not-store"


class _SignedIn(ChatGPTAuth):
    """A ChatGPT sign-in that never calls OpenAI."""

    def __init__(self) -> None:
        super().__init__()
        self.active = True

    @property
    def signed_in(self) -> bool:
        return self.active

    def plan_usage(self, **_kwargs: object) -> PlanUsage:
        return PlanUsage(token=CHATGPT_TOKEN, models=("gpt-6.1-sol",))

    def status(self) -> dict[str, object]:
        return {"signed_in": self.active, "models": ["gpt-6.1-sol"] if self.active else []}

    def sign_out(self) -> None:
        self.active = False


def test_chatgpt_sign_in_is_used_when_the_key_is_blank(
    api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, store, _service = api
    auth = _SignedIn()
    app.dependency_overrides[get_chatgpt_auth] = lambda: auth
    caplog.set_level(logging.INFO, logger="app")
    seen: dict[str, object] = {}

    def fake(provider: str, model: str | None, api_key: str | None, prompt: str, **kwargs: object) -> str:
        seen.update(kwargs, api_key=api_key)
        return "{not json"

    monkeypatch.setattr("app.main.complete_plan", fake)
    response = client.post(
        "/v1/plans",
        json={"prompt": EXAMPLE_PROMPT, "provider": "openai", "model": "gpt-6.1-sol", "api_key": ""},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["generator"] == "openai-chatgpt:gpt-6.1-sol"
    assert body["status"] == "draft"
    assert body["validation_errors"]
    plan_usage = seen["plan_usage"]
    assert isinstance(plan_usage, PlanUsage) and plan_usage.token == CHATGPT_TOKEN
    stored = store.get(UUID(str(body["id"])))
    assert stored is not None
    assert CHATGPT_TOKEN not in stored.model_dump_json()
    assert CHATGPT_TOKEN not in response.text
    assert CHATGPT_TOKEN not in caplog.text
    assert "credential=chatgpt_plan" in caplog.text


def test_typed_key_wins_over_the_chatgpt_sign_in(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _service = api
    app.dependency_overrides[get_chatgpt_auth] = lambda: _SignedIn()
    seen: dict[str, object] = {}

    def fake(provider: str, model: str | None, api_key: str | None, prompt: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return _model_plan([_DB_SMALL])

    monkeypatch.setattr("app.main.complete_plan", fake)
    response = client.post(
        "/v1/plans",
        json={"prompt": EXAMPLE_PROMPT, "provider": "openai", "model": "gpt-5", "api_key": "sk-test"},
    )
    assert response.status_code == 201
    assert response.json()["generator"] == "openai:gpt-5"
    assert seen["plan_usage"] is None


def test_rejected_chatgpt_sign_in_is_forgotten(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _service = api
    auth = _SignedIn()
    app.dependency_overrides[get_chatgpt_auth] = lambda: auth

    def fake(*_args: object, **_kwargs: object) -> str:
        raise PlanUsageAuthError("OpenAI no longer accepts the ChatGPT sign-in. Sign in with ChatGPT again.")

    monkeypatch.setattr("app.main.complete_plan", fake)
    response = client.post(
        "/v1/plans",
        json={"prompt": EXAMPLE_PROMPT, "provider": "openai", "model": "gpt-6.1-sol"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "provider_error"
    assert auth.signed_in is False
    assert store.list_records() == []


def test_chatgpt_session_routes_never_return_a_token(api) -> None:
    client, _store, _service = api
    auth = _SignedIn()
    app.dependency_overrides[get_chatgpt_auth] = lambda: auth
    session = client.get("/v1/openai/session")
    assert session.status_code == 200
    assert session.json() == {"signed_in": True, "models": ["gpt-6.1-sol"]}
    assert CHATGPT_TOKEN not in session.text
    signed_out = client.post("/v1/openai/sign-out")
    assert signed_out.json() == {"signed_in": False, "models": []}


def test_sign_in_route_returns_a_loopback_authorize_url(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _service = api
    monkeypatch.setenv("OPENAI_CALLBACK_PORT", "8123")
    response = client.get("/v1/openai/sign-in")
    assert response.status_code == 200
    url = response.json()["authorize_url"]
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8123%2Fcallback" in url


def test_callback_with_a_bad_state_shows_an_escaped_error_page(api) -> None:
    client, _store, _service = api
    client.get("/v1/openai/sign-in")
    response = client.get("/callback", params={"code": "c", "state": "wrong", "client_id": "x"})
    assert response.status_code == 400
    assert "did not match" in response.text
    assert "Traceback" not in response.text

    denied = client.get("/callback", params={"error": "<script>alert(1)</script>"})
    assert denied.status_code == 400
    assert "<script>" not in denied.text


def test_access_log_hides_the_sign_in_code_and_state() -> None:
    access = logging.getLogger("uvicorn.access")
    record = access.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/callback?code=ac_secret&state=s&client_id=c", "1.1", 400),
        None,
    )
    assert all(item.filter(record) for item in access.filters)
    message = record.getMessage()
    assert "ac_secret" not in message
    assert '"GET /callback?[redacted] HTTP/1.1" 400' in message

    other = access.makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %s %s %s %d",
        ("127.0.0.1:5000", "GET", "/v1/plans?x=1", "1.1", 200), None,
    )
    assert all(item.filter(other) for item in access.filters)
    assert "/v1/plans?x=1" in other.getMessage()


def test_refused_sign_in_logs_why(api, caplog: pytest.LogCaptureFixture) -> None:
    client, _store, _service = api
    caplog.set_level(logging.INFO, logger="app")
    client.get("/callback", params={"code": "ac_secret", "state": "wrong", "client_id": "x"})
    assert "chatgpt sign-in refused reason=No sign-in is waiting" in caplog.text
    assert "ac_secret" not in caplog.text


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
