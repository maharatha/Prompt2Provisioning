from decimal import Decimal
from uuid import uuid4

import pytest

from app.hashing import canonical_hash
from app.models import (
    CheckStatus,
    Environment,
    PlanStatus,
    ProposedPlan,
    Resource,
    ResourceType,
)
from app.planner import MockPlanner
from app.services import PlanService
from app.store import InMemoryStore

EXAMPLE_PROMPT = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)
MEDIUM_DEV_PROMPT = "A medium PostgreSQL database for a development team in US East."

_POLICY_IDS = (
    "allowed_regions",
    "required_tags",
    "resource_limits",
    "dev_sku_tier",
    "storage_public",
    "dev_medium_cost",
)

_VALID_RAW = (
    '{"region":"us-east-1","environment":"dev",'
    '"tags":{"environment":"dev","owner":"dev-team","cost-center":"engineering"},'
    '"resources":[{"type":"postgres","name":"database","sku":"db-small","quantity":1,'
    '"public_access":false}]}\n'
)
_MALFORMED_RAW = '{ "region": "us-east-1", "resources": [\n'


class RecordingPlanner:
    def __init__(self, output: str) -> None:
        self.output = output
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.output


def _service(planner: RecordingPlanner | None = None) -> PlanService:
    if planner is None:
        return PlanService(InMemoryStore())
    return PlanService(InMemoryStore(), planner)


def _expected_assignment_plan() -> ProposedPlan:
    return ProposedPlan(
        region="us-east-1",
        environment=Environment.DEV,
        tags={
            "environment": "dev",
            "owner": "dev-team",
            "cost-center": "engineering",
        },
        resources=[
            Resource(
                type=ResourceType.POSTGRES,
                name="database",
                sku="db-small",
                quantity=1,
            ),
            Resource(
                type=ResourceType.CONTAINER,
                name="web",
                sku="container-small",
                quantity=2,
            ),
        ],
    )


def test_assignment_prompt_is_evaluated_with_cost_and_hash() -> None:
    service = _service()
    record = service.create_plan(EXAMPLE_PROMPT)
    expected = _expected_assignment_plan()

    assert record.status is PlanStatus.EVALUATED
    assert record.prompt == EXAMPLE_PROMPT
    assert record.raw_output == MockPlanner().generate(EXAMPLE_PROMPT)
    assert record.validation_errors == []
    assert record.proposed == expected
    assert [check.policy_id for check in record.policy_checks] == list(_POLICY_IDS)
    assert all(check.status is CheckStatus.PASSED for check in record.policy_checks)
    assert record.cost is not None
    assert record.cost.succeeded is True
    assert record.cost.pricing_errors == []
    assert record.cost.monthly_total == Decimal("71")
    assert [(item.sku, item.quantity, item.amount) for item in record.cost.line_items] == [
        ("db-small", 1, Decimal("35")),
        ("container-small", 2, Decimal("36")),
    ]
    assert record.plan_hash == canonical_hash(expected)


@pytest.mark.parametrize(
    "prompt",
    [
        "SCENARIO:malformed",
        "SCENARIO:missing_region",
        "SCENARIO:unknown_type",
    ],
)
def test_invalid_planner_output_persists_as_draft(prompt: str) -> None:
    service = _service()
    record = service.create_plan(prompt)
    stored = service.get_plan(record.id)

    assert stored is not None
    assert stored.status is PlanStatus.DRAFT
    assert stored.proposed is None
    assert stored.plan_hash is None
    assert stored.policy_checks == []
    assert stored.cost is None
    assert stored.validation_errors
    assert stored.raw_output == MockPlanner().generate(prompt)
    assert stored.model_dump() == record.model_dump()

    if prompt == "SCENARIO:malformed":
        assert stored.validation_errors[0].code == "json_invalid"
    else:
        assert all(issue.code != "json_invalid" for issue in stored.validation_errors)


@pytest.mark.parametrize(
    ("prompt", "policy_id"),
    [
        ("SCENARIO:excessive_qty", "resource_limits"),
        ("SCENARIO:missing_tags", "required_tags"),
        ("SCENARIO:public_storage", "storage_public"),
    ],
)
def test_policy_errors_remain_on_evaluated_records(prompt: str, policy_id: str) -> None:
    service = _service()
    record = service.create_plan(prompt)

    assert record.status is PlanStatus.EVALUATED
    assert record.proposed is not None
    assert record.plan_hash is not None
    assert record.validation_errors == []
    assert record.cost is not None
    assert record.cost.succeeded is True
    assert any(
        check.policy_id == policy_id and check.status is CheckStatus.ERROR
        for check in record.policy_checks
    )

    stored = service.get_plan(record.id)
    assert stored is not None
    assert stored.policy_checks == record.policy_checks
    assert stored.status is PlanStatus.EVALUATED


def test_unknown_sku_is_evaluated_with_failed_pricing() -> None:
    service = _service()
    record = service.create_plan("SCENARIO:unsupported_sku")

    assert record.status is PlanStatus.EVALUATED
    assert record.proposed is not None
    assert record.proposed.resources[0].sku == "container-large"
    assert record.plan_hash == canonical_hash(record.proposed)
    assert record.validation_errors == []
    assert record.cost is not None
    assert record.cost.succeeded is False
    assert record.cost.pricing_errors
    assert record.cost.line_items == []
    assert record.cost.monthly_total == Decimal("0")


def test_dev_medium_warning_is_preserved_without_errors() -> None:
    service = _service()
    record = service.create_plan(MEDIUM_DEV_PROMPT)

    warnings = [check for check in record.policy_checks if check.status is CheckStatus.WARNING]
    errors = [check for check in record.policy_checks if check.status is CheckStatus.ERROR]

    assert record.status is PlanStatus.EVALUATED
    assert record.proposed is not None
    assert record.proposed.resources[0].sku == "db-medium"
    assert len(warnings) == 1
    assert warnings[0].policy_id == "dev_medium_cost"
    assert errors == []
    assert record.cost is not None
    assert record.cost.succeeded is True
    assert record.cost.monthly_total == Decimal("95")


@pytest.mark.parametrize("raw_output", [_VALID_RAW, _MALFORMED_RAW])
def test_raw_output_is_retained_exactly(raw_output: str) -> None:
    planner = RecordingPlanner(raw_output)
    service = _service(planner)
    record = service.create_plan("keep this raw string")

    assert planner.prompts == ["keep this raw string"]
    assert record.raw_output == raw_output
    stored = service.get_plan(record.id)
    assert stored is not None
    assert stored.raw_output == raw_output


def test_planner_is_called_once_per_creation() -> None:
    planner = RecordingPlanner(_VALID_RAW)
    service = _service(planner)

    first = service.create_plan("first prompt")
    second = service.create_plan("second prompt")

    assert planner.prompts == ["first prompt", "second prompt"]
    assert first.id != second.id
    assert first.raw_output == _VALID_RAW
    assert second.raw_output == _VALID_RAW


def test_separate_creations_have_distinct_ids() -> None:
    service = _service()
    first = service.create_plan(EXAMPLE_PROMPT)
    second = service.create_plan("one database in US West for test")

    assert first.id != second.id
    fetched_first = service.get_plan(first.id)
    fetched_second = service.get_plan(second.id)
    assert fetched_first is not None
    assert fetched_second is not None
    assert fetched_first.prompt == EXAMPLE_PROMPT
    assert fetched_first.proposed is not None
    assert fetched_first.proposed.region == "us-east-1"
    assert fetched_second.prompt == "one database in US West for test"
    assert fetched_second.proposed is not None
    assert fetched_second.proposed.region == "us-west-2"
    assert fetched_second.proposed.environment is Environment.TEST


def test_retrieval_and_not_found() -> None:
    service = _service()
    created = service.create_plan(EXAMPLE_PROMPT)

    fetched = service.get_plan(created.id)
    assert fetched is not None
    assert fetched is not created
    assert fetched.model_dump() == created.model_dump()
    assert service.get_plan(uuid4()) is None


def test_mutating_returned_record_does_not_change_stored_data() -> None:
    service = _service()
    created = service.create_plan(EXAMPLE_PROMPT)
    before = created.model_dump(mode="json")

    created.prompt = "mutated prompt"
    created.raw_output = "mutated raw"
    created.status = PlanStatus.DRAFT
    created.plan_hash = "mutated-hash"
    assert created.proposed is not None
    created.proposed.region = "mutated-region"
    created.proposed.resources[0].name = "mutated-name"
    created.policy_checks.clear()
    created.validation_errors.clear()
    assert created.cost is not None
    created.cost.monthly_total = Decimal("1")

    fetched = service.get_plan(created.id)
    assert fetched is not None
    assert fetched.model_dump(mode="json") == before

    fetched.prompt = "mutated fetch"
    assert fetched.proposed is not None
    fetched.proposed.region = "fetched-region"
    again = service.get_plan(created.id)
    assert again is not None
    assert again is not fetched
    assert again.model_dump(mode="json") == before
    assert again.prompt == EXAMPLE_PROMPT
    assert again.proposed is not None
    assert again.proposed.region == "us-east-1"


def test_stored_hash_equals_canonical_hash_of_stored_proposal() -> None:
    service = _service()
    created = service.create_plan(EXAMPLE_PROMPT)
    stored = service.get_plan(created.id)

    assert stored is not None
    assert stored.proposed is not None
    assert stored.plan_hash == canonical_hash(stored.proposed)
    assert stored.plan_hash == created.plan_hash
    assert created.proposed is not None
    assert created.plan_hash == canonical_hash(created.proposed)
