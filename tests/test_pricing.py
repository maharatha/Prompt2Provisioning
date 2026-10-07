import json
from decimal import Decimal
from pathlib import Path

import pytest

from app import pricing
from app.models import CostEstimate, Environment, ProposedPlan, Resource, ResourceType
from app.pricing import estimate_cost


def _container(
    name: str = "web",
    sku: str = "container-small",
    quantity: int = 1,
) -> Resource:
    return Resource(
        type=ResourceType.CONTAINER,
        name=name,
        sku=sku,
        quantity=quantity,
    )


def _postgres(
    name: str = "database",
    sku: str = "db-small",
    quantity: int = 1,
) -> Resource:
    return Resource(
        type=ResourceType.POSTGRES,
        name=name,
        sku=sku,
        quantity=quantity,
    )


def _storage(
    name: str = "assets",
    sku: str = "storage-standard",
    quantity: int = 1,
    capacity_gb: int = 100,
) -> Resource:
    return Resource(
        type=ResourceType.OBJECT_STORAGE,
        name=name,
        sku=sku,
        quantity=quantity,
        capacity_gb=capacity_gb,
    )


def _plan(resources: list[Resource]) -> ProposedPlan:
    return ProposedPlan(
        region="us-east-1",
        environment=Environment.DEV,
        tags={
            "environment": "dev",
            "owner": "dev-team",
            "cost-center": "engineering",
        },
        resources=list(resources),
    )


def _assignment_plan() -> ProposedPlan:
    return _plan(
        [
            _postgres(name="database", sku="db-small", quantity=1),
            _container(name="web", sku="container-small", quantity=2),
        ]
    )


def _mixed_plan() -> ProposedPlan:
    return _plan(
        [
            _container(name="web", sku="container-small", quantity=2),
            _postgres(name="database", sku="db-small", quantity=1),
            _storage(name="assets", sku="storage-standard", quantity=1, capacity_gb=100),
            _storage(name="logs", sku="storage-standard", quantity=1, capacity_gb=1),
        ]
    )


def _catalog_payload() -> dict[str, dict[str, object]]:
    return {
        "container": {
            "container-small": "18",
            "container-medium": "42",
        },
        "postgres": {
            "db-small": "35",
            "db-medium": "95",
        },
        "object_storage": {
            "storage-standard": "0.025",
        },
    }


def _write_catalog(directory: Path, payload: object) -> Path:
    path = directory / "prices.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _assert_success(
    result: CostEstimate,
    total: str,
    lines: list[tuple[str, str, int, str, str]],
) -> None:
    assert result.succeeded is True
    assert result.pricing_errors == []
    assert result.currency == "USD"
    assert isinstance(result.monthly_total, Decimal)
    assert result.monthly_total == Decimal(total)
    assert str(result.monthly_total) == total
    assert [
        (item.resource_name, item.sku, item.quantity, str(item.unit_price), str(item.amount))
        for item in result.line_items
    ] == lines
    assert sum((item.amount for item in result.line_items), Decimal("0")) == result.monthly_total
    assert all(
        isinstance(item.unit_price, Decimal) and isinstance(item.amount, Decimal)
        for item in result.line_items
    )


def _assert_no_estimate(result: CostEstimate) -> None:
    assert result.succeeded is False
    assert result.line_items == []
    assert result.pricing_errors
    assert all(isinstance(error, str) and error.strip() for error in result.pricing_errors)
    assert isinstance(result.monthly_total, Decimal)
    assert result.monthly_total == Decimal("0")
    assert str(result.monthly_total) == "0"
    assert result.currency == "USD"


def test_assignment_example_totals_71() -> None:
    result = estimate_cost(_assignment_plan())
    _assert_success(
        result,
        "71",
        [
            ("database", "db-small", 1, "35", "35"),
            ("web", "container-small", 2, "18", "36"),
        ],
    )


def test_container_and_database_tiers() -> None:
    plan = _plan(
        [
            _container(name="web", sku="container-small", quantity=1),
            _container(name="api", sku="container-medium", quantity=1),
            _postgres(name="primary", sku="db-small", quantity=1),
            _postgres(name="replica", sku="db-medium", quantity=1),
        ]
    )
    result = estimate_cost(plan)
    _assert_success(
        result,
        "190",
        [
            ("web", "container-small", 1, "18", "18"),
            ("api", "container-medium", 1, "42", "42"),
            ("primary", "db-small", 1, "35", "35"),
            ("replica", "db-medium", 1, "95", "95"),
        ],
    )


def test_storage_100_gb_costs_2_500() -> None:
    result = estimate_cost(_plan([_storage(name="assets", quantity=1, capacity_gb=100)]))
    _assert_success(
        result,
        "2.500",
        [("assets", "storage-standard", 1, "0.025", "2.500")],
    )


def test_storage_quantity_multiplies_capacity() -> None:
    result = estimate_cost(_plan([_storage(name="assets", quantity=2, capacity_gb=100)]))
    _assert_success(
        result,
        "5.000",
        [("assets", "storage-standard", 2, "0.025", "5.000")],
    )


def test_mixed_resources_keep_fractional_precision() -> None:
    result = estimate_cost(_mixed_plan())
    _assert_success(
        result,
        "73.525",
        [
            ("web", "container-small", 2, "18", "36"),
            ("database", "db-small", 1, "35", "35"),
            ("assets", "storage-standard", 1, "0.025", "2.500"),
            ("logs", "storage-standard", 1, "0.025", "0.025"),
        ],
    )


def test_unknown_sku_alone_produces_no_estimate() -> None:
    result = estimate_cost(_plan([_container(name="web", sku="container-large", quantity=2)]))
    _assert_no_estimate(result)
    assert any(
        "unknown SKU 'container-large'" in error and "web" in error
        for error in result.pricing_errors
    )


def test_unknown_sku_alongside_priced_resources_produces_no_estimate() -> None:
    plan = _plan(
        [
            _postgres(name="database", sku="db-small", quantity=1),
            _container(name="web", sku="container-large", quantity=2),
        ]
    )
    result = estimate_cost(plan)
    _assert_no_estimate(result)
    assert any("container-large" in error for error in result.pricing_errors)
    assert result.monthly_total != Decimal("35")
    assert result.monthly_total != Decimal("71")


def test_resource_type_sku_mismatch_is_not_priced_as_the_other_type() -> None:
    result = estimate_cost(_plan([_postgres(name="database", sku="container-small", quantity=2)]))
    _assert_no_estimate(result)
    assert any(
        "container-small" in error and "postgres" in error and "does not match" in error
        for error in result.pricing_errors
    )
    assert all("unknown SKU" not in error for error in result.pricing_errors)
    assert result.monthly_total != Decimal("36")


def test_missing_capacity_after_mutation_fails_closed() -> None:
    plan = _plan(
        [
            _postgres(name="database", sku="db-small", quantity=1),
            _storage(name="assets", quantity=2, capacity_gb=100),
        ]
    )
    plan.resources[1].capacity_gb = None
    before = plan.model_dump()
    result = estimate_cost(plan)
    _assert_no_estimate(result)
    assert any(
        "assets" in error and "capacity_gb" in error and "missing" in error
        for error in result.pricing_errors
    )
    assert plan.model_dump() == before
    assert plan.resources[1].capacity_gb is None


@pytest.mark.parametrize("rate", [18, 18.0, True, None])
def test_non_string_catalog_rate_fails_closed(tmp_path: Path, rate: object) -> None:
    payload = _catalog_payload()
    payload["object_storage"]["storage-standard"] = rate
    result = estimate_cost(_assignment_plan(), catalog_path=_write_catalog(tmp_path, payload))
    _assert_no_estimate(result)
    assert any(
        "storage-standard" in error and "must be a string" in error
        for error in result.pricing_errors
    )


@pytest.mark.parametrize("rate", ["-1", "-0.025"])
def test_negative_catalog_rate_fails_closed(tmp_path: Path, rate: str) -> None:
    payload = _catalog_payload()
    payload["object_storage"]["storage-standard"] = rate
    result = estimate_cost(_assignment_plan(), catalog_path=_write_catalog(tmp_path, payload))
    _assert_no_estimate(result)
    assert any(
        "storage-standard" in error and "must not be negative" in error
        for error in result.pricing_errors
    )


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-Infinity", "inf"])
def test_non_finite_catalog_rate_fails_closed(tmp_path: Path, rate: str) -> None:
    payload = _catalog_payload()
    payload["object_storage"]["storage-standard"] = rate
    result = estimate_cost(_assignment_plan(), catalog_path=_write_catalog(tmp_path, payload))
    _assert_no_estimate(result)
    assert any("storage-standard" in error and "finite" in error for error in result.pricing_errors)


def test_malformed_catalog_returns_pricing_errors(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text("{", encoding="utf-8")
    result = estimate_cost(_assignment_plan(), catalog_path=path)
    _assert_no_estimate(result)


def test_non_object_catalog_returns_pricing_errors(tmp_path: Path) -> None:
    result = estimate_cost(_assignment_plan(), catalog_path=_write_catalog(tmp_path, []))
    _assert_no_estimate(result)


def test_catalog_file_stores_rates_as_strings() -> None:
    catalog_path = Path(pricing.__file__).resolve().parent / "data" / "prices.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert payload == {
        "container": {
            "container-small": "18",
            "container-medium": "42",
        },
        "postgres": {
            "db-small": "35",
            "db-medium": "95",
        },
        "object_storage": {
            "storage-standard": "0.025",
        },
    }


def test_catalog_path_does_not_depend_on_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = estimate_cost(_assignment_plan())
    _assert_success(
        result,
        "71",
        [
            ("database", "db-small", 1, "35", "35"),
            ("web", "container-small", 2, "18", "36"),
        ],
    )


def test_estimate_is_deterministic_and_leaves_the_plan_unchanged() -> None:
    plan = _mixed_plan()
    before = plan.model_dump()
    first = estimate_cost(plan)
    second = estimate_cost(plan)
    assert first == second
    assert plan.model_dump() == before
    assert str(first.monthly_total) == "73.525"
