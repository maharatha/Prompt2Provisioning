from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.models import (
    CheckStatus,
    CostEstimate,
    CostLineItem,
    Environment,
    PlanRecord,
    PlanStatus,
    ProposedPlan,
    Resource,
    ResourceType,
)


def valid_container(**overrides: object) -> Resource:
    data: dict[str, object] = {
        "type": ResourceType.CONTAINER,
        "name": "web",
        "sku": "container-small",
        "quantity": 2,
    }
    data.update(overrides)
    return Resource.model_validate(data)


def valid_proposed_plan(**overrides: object) -> ProposedPlan:
    data: dict[str, object] = {
        "region": "us-east-1",
        "environment": Environment.DEV,
        "tags": {
            "environment": "dev",
            "owner": "dev-team",
            "cost-center": "engineering",
        },
        "resources": [valid_container()],
    }
    data.update(overrides)
    return ProposedPlan.model_validate(data)


def valid_line_item(**overrides: object) -> CostLineItem:
    data: dict[str, object] = {
        "resource_name": "web",
        "sku": "container-small",
        "quantity": 2,
        "unit_price": Decimal("18"),
        "amount": Decimal("36"),
    }
    data.update(overrides)
    return CostLineItem.model_validate(data)


def assert_json_compatible(value: object) -> None:
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        raise AssertionError("JSON-mode dump must not contain floats")
    if isinstance(value, list):
        for item in value:
            assert_json_compatible(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            assert_json_compatible(item)
        return
    raise AssertionError(f"non-JSON-compatible type: {type(value)!r}")


def test_valid_proposed_plan_construction() -> None:
    plan = valid_proposed_plan()
    assert plan.region == "us-east-1"
    assert plan.environment is Environment.DEV
    assert plan.resources[0].type is ResourceType.CONTAINER
    assert plan.resources[0].quantity == 2
    assert plan.resources[0].capacity_gb is None
    assert plan.resources[0].public_access is False


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Resource.model_validate(
            {
                "type": ResourceType.CONTAINER,
                "name": "web",
                "sku": "container-small",
                "quantity": 1,
                "owner": "not-allowed",
            }
        )


def test_blank_resource_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        valid_container(name="   ")


def test_blank_sku_is_rejected() -> None:
    with pytest.raises(ValidationError):
        valid_container(sku="")


def test_quantity_zero_is_rejected() -> None:
    with pytest.raises(ValidationError):
        valid_container(quantity=0)


def test_quantity_over_100_is_rejected() -> None:
    with pytest.raises(ValidationError):
        valid_container(quantity=101)


def test_quantity_rejects_boolean() -> None:
    with pytest.raises(ValidationError):
        valid_container(quantity=True)
    with pytest.raises(ValidationError):
        valid_container(quantity=False)


def test_quantity_rejects_float_including_whole_numbers() -> None:
    with pytest.raises(ValidationError):
        valid_container(quantity=2.0)
    with pytest.raises(ValidationError):
        valid_container(quantity=2.5)


def test_quantity_rejects_numeric_string() -> None:
    with pytest.raises(ValidationError):
        valid_container(quantity="2")


def test_quantity_accepts_valid_integer() -> None:
    resource = valid_container(quantity=3)
    assert resource.quantity == 3
    assert isinstance(resource.quantity, int)


def test_object_storage_without_capacity_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Resource.model_validate(
            {
                "type": ResourceType.OBJECT_STORAGE,
                "name": "assets",
                "sku": "storage-standard",
                "quantity": 1,
            }
        )


def test_object_storage_with_positive_capacity_is_accepted() -> None:
    storage = Resource.model_validate(
        {
            "type": ResourceType.OBJECT_STORAGE,
            "name": "assets",
            "sku": "storage-standard",
            "quantity": 1,
            "capacity_gb": 100,
        }
    )
    assert storage.capacity_gb == 100
    assert isinstance(storage.capacity_gb, int)


def test_capacity_gb_rejects_boolean_float_and_numeric_string() -> None:
    payload = {
        "type": ResourceType.OBJECT_STORAGE,
        "name": "assets",
        "sku": "storage-standard",
        "quantity": 1,
    }
    for value in (True, False, 100.0, 10.5, "100"):
        with pytest.raises(ValidationError):
            Resource.model_validate({**payload, "capacity_gb": value})


def test_container_with_capacity_gb_is_rejected() -> None:
    with pytest.raises(ValidationError):
        valid_container(capacity_gb=20)


def test_postgres_with_capacity_gb_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Resource.model_validate(
            {
                "type": ResourceType.POSTGRES,
                "name": "app-db",
                "sku": "db-small",
                "quantity": 1,
                "capacity_gb": 50,
            }
        )


def test_blank_tag_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        valid_proposed_plan(tags={"   ": "dev-team"})


def test_blank_tag_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        valid_proposed_plan(tags={"owner": ""})


def test_proposed_plan_rejects_status_cost_hash_and_approval_fields() -> None:
    base = valid_proposed_plan().model_dump()
    forbidden = {
        "name": "dev-web-stack",
        "status": PlanStatus.EVALUATED,
        "cost": {"monthly_total": "71.00", "succeeded": True},
        "evaluated_plan_hash": "abc123",
        "plan_hash": "abc123",
        "approved": True,
        "approval_blocked": False,
    }
    for field, value in forbidden.items():
        payload = {**base, field: value}
        with pytest.raises(ValidationError):
            ProposedPlan.model_validate(payload)


def test_decimal_cost_calculation_remains_exact() -> None:
    container_amount = Decimal("18") * Decimal("2")
    database_amount = Decimal("35")
    storage_amount = Decimal("0.025") * Decimal("100")
    monthly_total = container_amount + database_amount + storage_amount

    estimate = CostEstimate(
        monthly_total=monthly_total,
        line_items=[
            valid_line_item(amount=container_amount),
            valid_line_item(
                resource_name="db",
                sku="db-small",
                quantity=1,
                unit_price=Decimal("35"),
                amount=database_amount,
            ),
            valid_line_item(
                resource_name="assets",
                sku="storage-standard",
                quantity=100,
                unit_price=Decimal("0.025"),
                amount=storage_amount,
            ),
        ],
        succeeded=True,
    )

    assert monthly_total == Decimal("73.50")
    assert estimate.monthly_total == Decimal("73.50")
    with pytest.raises(ValidationError):
        CostLineItem.model_validate(
            {
                "resource_name": "web",
                "sku": "container-small",
                "quantity": 1,
                "unit_price": 18.0,
                "amount": 18.0,
            }
        )


def test_successful_cost_estimate_cannot_contain_pricing_errors() -> None:
    with pytest.raises(ValidationError):
        CostEstimate(
            monthly_total=Decimal("36"),
            line_items=[valid_line_item()],
            pricing_errors=["unknown sku"],
            succeeded=True,
        )


def test_failed_cost_estimate_must_contain_pricing_error() -> None:
    with pytest.raises(ValidationError):
        CostEstimate(
            monthly_total=Decimal("0"),
            line_items=[],
            pricing_errors=[],
            succeeded=False,
        )


def test_successful_cost_estimate_total_must_equal_line_items() -> None:
    with pytest.raises(ValidationError):
        CostEstimate(
            monthly_total=Decimal("99.00"),
            line_items=[valid_line_item()],
            succeeded=True,
        )


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanRecord(
            prompt="provision a small database",
            raw_output="",
            created_at=datetime(2026, 10, 6, 22, 0, 0),
            updated_at=datetime.now(timezone.utc),
        )


def test_json_mode_serialization_is_json_compatible() -> None:
    record = PlanRecord(
        prompt="provision a small database",
        raw_output='{"region":"us-east-1"}',
        status=PlanStatus.DRAFT,
        proposed=valid_proposed_plan(),
        cost=CostEstimate(
            monthly_total=Decimal("36.00"),
            line_items=[valid_line_item(amount=Decimal("36.00"))],
            succeeded=True,
        ),
    )
    dumped = record.model_dump(mode="json", exclude_none=True)
    proposed = record.proposed.model_dump(mode="json", exclude_none=True)

    UUID(dumped["id"])
    assert dumped["status"] == "draft"
    assert dumped["proposed"]["environment"] == "dev"
    assert dumped["cost"]["monthly_total"] == "36.00"
    assert dumped["cost"]["line_items"][0]["unit_price"] == "18"
    assert dumped["created_at"].endswith("+00:00") or dumped["created_at"].endswith("Z")
    assert "capacity_gb" not in proposed["resources"][0]
    assert_json_compatible(dumped)
    assert_json_compatible(proposed)


def test_check_status_values() -> None:
    assert CheckStatus.PASSED == "passed"
    assert CheckStatus.WARNING == "warning"
    assert CheckStatus.ERROR == "error"
