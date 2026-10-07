import json

import pytest

from app.models import Environment, ProposedPlan, ResourceType, ValidationIssue
from app.planner import MockPlanner
from app.validation import ValidationResult, validate_raw_plan

EXAMPLE_PROMPT = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)

SCHEMA_VALID_SCENARIOS = (
    "missing_tags",
    "unsupported_sku",
    "excessive_quantity",
    "public_object_storage",
)


@pytest.fixture
def planner() -> MockPlanner:
    return MockPlanner()


def _tags() -> dict[str, str]:
    return {
        "environment": "dev",
        "owner": "dev-team",
        "cost-center": "engineering",
    }


def _container(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "type": "container",
        "name": "web",
        "sku": "container-small",
        "quantity": 1,
        "public_access": False,
    }
    resource.update(overrides)
    return resource


def _storage(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "type": "object_storage",
        "name": "bucket",
        "sku": "storage-standard",
        "quantity": 1,
        "capacity_gb": 100,
        "public_access": False,
    }
    resource.update(overrides)
    return resource


def _plan_json(
    resources: list[dict[str, object]],
    **overrides: object,
) -> str:
    payload: dict[str, object] = {
        "region": "us-east-1",
        "environment": "dev",
        "tags": _tags(),
        "resources": resources,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _assert_failure(result: ValidationResult) -> None:
    assert result.proposed is None
    assert result.errors
    for issue in result.errors:
        assert isinstance(issue, ValidationIssue)
        assert issue.code.strip()
        assert issue.message.strip()


def _assert_unchanged(raw: str) -> ProposedPlan:
    result = validate_raw_plan(raw)
    assert result.errors == ()
    assert result.proposed is not None
    assert result.proposed.model_dump(mode="json", exclude_none=True) == json.loads(raw)
    return result.proposed


def _issue_at(result: ValidationResult, field_path: str | None) -> ValidationIssue:
    matches = [issue for issue in result.errors if issue.field_path == field_path]
    assert len(matches) == 1
    return matches[0]


def test_valid_mock_output(planner: MockPlanner) -> None:
    plan = _assert_unchanged(planner.generate(EXAMPLE_PROMPT))

    assert plan.region == "us-east-1"
    assert plan.environment is Environment.DEV
    assert plan.tags == _tags()
    assert [
        (resource.type, resource.name, resource.sku, resource.quantity, resource.capacity_gb)
        for resource in plan.resources
    ] == [
        (ResourceType.POSTGRES, "database", "db-small", 1, None),
        (ResourceType.CONTAINER, "web", "container-small", 2, None),
    ]
    assert all(resource.public_access is False for resource in plan.resources)


def test_unrecognized_region_is_one_validation_issue(planner: MockPlanner) -> None:
    result = validate_raw_plan(planner.generate("one database in US NORTH"))
    assert result.proposed is None
    assert len(result.errors) == 1
    issue = result.errors[0]
    assert issue.code == "unrecognized_input"
    assert issue.field_path == "region"
    assert "US NORTH" in issue.message


def test_unrecognized_resources_point_at_resources(planner: MockPlanner) -> None:
    result = validate_raw_plan(planner.generate("Build a rocket for the launch next week."))
    assert result.proposed is None
    assert [(issue.code, issue.field_path) for issue in result.errors] == [
        ("unrecognized_input", "resources"),
    ]


@pytest.mark.parametrize("field", [None, "tags", 7])
def test_interpretation_field_outside_the_known_set_points_at_region(field: object) -> None:
    payload: dict[str, object] = {"interpretation_error": "Unrecognized input."}
    if field is not None:
        payload["interpretation_field"] = field
    result = validate_raw_plan(json.dumps(payload))
    assert [(issue.code, issue.field_path) for issue in result.errors] == [
        ("unrecognized_input", "region"),
    ]


def _plan_with(region: str = "us-east-1", tags: dict[str, str] | None = None) -> str:
    return json.dumps(
        {
            "region": region,
            "environment": "dev",
            "tags": tags if tags is not None else _tags(),
            "resources": [
                {"type": "postgres", "name": "database", "sku": "db-small", "quantity": 1, "public_access": False}
            ],
        }
    )


def test_region_and_tags_at_their_length_limits_are_accepted() -> None:
    result = validate_raw_plan(_plan_with(region="r" * 32, tags={**_tags(), "k" * 128: "v" * 128}))
    assert result.errors == ()


@pytest.mark.parametrize(
    ("raw", "field_path", "message"),
    [
        (_plan_with(region="r" * 33), "region", "region must be at most 32 characters"),
        (_plan_with(tags={**_tags(), "k" * 129: "v"}), "tags", "tag keys must be at most 128 characters"),
        (_plan_with(tags={**_tags(), "note": "v" * 129}), "tags", "tag values must be at most 128 characters"),
    ],
)
def test_overlong_region_and_tags_fail_schema(raw: str, field_path: str, message: str) -> None:
    result = validate_raw_plan(raw)
    assert result.proposed is None
    assert [(issue.field_path, issue.message) for issue in result.errors] == [(field_path, message)]


def test_malformed_json_is_a_validation_failure(planner: MockPlanner) -> None:
    result = validate_raw_plan(planner.generate("one database", scenario="malformed_json"))

    _assert_failure(result)
    assert len(result.errors) == 1
    issue = result.errors[0]
    assert issue.code == "json_invalid"
    assert issue.field_path is None
    assert issue.message == "Expecting value at line 2 column 1"


@pytest.mark.parametrize("raw", ["", "{", "not json", "[1, 2"])
def test_other_malformed_strings_do_not_raise(raw: str) -> None:
    result = validate_raw_plan(raw)

    _assert_failure(result)
    assert result.errors[0].code == "json_invalid"
    assert result.errors[0].field_path is None
    assert "line" in result.errors[0].message
    assert "column" in result.errors[0].message


def test_missing_region(planner: MockPlanner) -> None:
    result = validate_raw_plan(planner.generate("database in US East", scenario="missing_region"))

    _assert_failure(result)
    issue = _issue_at(result, "region")
    assert issue.code == "missing"
    assert issue.message == "Field required"


def test_unexpected_fields_at_plan_and_resource_levels() -> None:
    raw = _plan_json(
        [_container(owner="alice")],
        status="approved",
    )
    result = validate_raw_plan(raw)

    _assert_failure(result)
    plan_issue = _issue_at(result, "status")
    resource_issue = _issue_at(result, "resources[0].owner")
    assert plan_issue.code == "extra_forbidden"
    assert resource_issue.code == "extra_forbidden"
    assert "not permitted" in plan_issue.message
    assert "not permitted" in resource_issue.message


def test_unknown_resource_type(planner: MockPlanner) -> None:
    result = validate_raw_plan(planner.generate("one container", scenario="unknown_resource_type"))

    _assert_failure(result)
    issue = _issue_at(result, "resources[0].type")
    assert issue.code == "enum"
    assert "container" in issue.message
    assert "postgres" in issue.message
    assert "object_storage" in issue.message


@pytest.mark.parametrize("quantity", [True, False, 2.0, 2.5, "2"])
def test_quantity_rejects_boolean_float_and_string(quantity: object) -> None:
    result = validate_raw_plan(_plan_json([_container(quantity=quantity)]))

    _assert_failure(result)
    issue = _issue_at(result, "resources[0].quantity")
    assert issue.code == "int_type"
    assert "integer" in issue.message


@pytest.mark.parametrize("capacity_gb", [True, False, 100.0, 10.5, "100"])
def test_capacity_rejects_boolean_float_and_string(capacity_gb: object) -> None:
    result = validate_raw_plan(_plan_json([_storage(capacity_gb=capacity_gb)]))

    _assert_failure(result)
    issue = _issue_at(result, "resources[0].capacity_gb")
    assert issue.code == "int_type"
    assert "integer" in issue.message


def test_object_storage_requires_capacity_gb() -> None:
    resource = _storage()
    del resource["capacity_gb"]
    result = validate_raw_plan(_plan_json([resource]))

    _assert_failure(result)
    issue = _issue_at(result, "resources[0]")
    assert issue.code == "value_error"
    assert issue.message == "object_storage requires capacity_gb"


@pytest.mark.parametrize(
    ("resource_type", "name", "sku"),
    [
        ("container", "web", "container-small"),
        ("postgres", "database", "db-small"),
    ],
)
def test_non_storage_resources_must_omit_capacity_gb(resource_type: str, name: str, sku: str) -> None:
    result = validate_raw_plan(
        _plan_json(
            [
                {
                    "type": resource_type,
                    "name": name,
                    "sku": sku,
                    "quantity": 1,
                    "capacity_gb": 20,
                    "public_access": False,
                }
            ]
        )
    )

    _assert_failure(result)
    issue = _issue_at(result, "resources[0]")
    assert issue.code == "value_error"
    assert issue.message == f"{resource_type} must not provide capacity_gb"


def test_positive_storage_capacity_is_accepted() -> None:
    plan = _assert_unchanged(_plan_json([_storage(capacity_gb=100)]))
    assert plan.resources[0].type is ResourceType.OBJECT_STORAGE
    assert plan.resources[0].capacity_gb == 100


def test_non_positive_storage_capacity_is_rejected() -> None:
    result = validate_raw_plan(_plan_json([_storage(capacity_gb=0)]))

    _assert_failure(result)
    issue = _issue_at(result, "resources[0].capacity_gb")
    assert issue.code == "greater_than"
    assert "greater than 0" in issue.message


@pytest.mark.parametrize("scenario", SCHEMA_VALID_SCENARIOS)
def test_schema_valid_negative_scenarios_pass_through_unchanged(
    planner: MockPlanner,
    scenario: str,
) -> None:
    raw = planner.generate("two medium databases in US West for production", scenario=scenario)
    plan = _assert_unchanged(raw)

    if scenario == "missing_tags":
        assert plan.tags == {"project": "demo"}
        assert plan.resources[0].sku == "container-small"
    elif scenario == "unsupported_sku":
        assert plan.environment is Environment.PROD
        assert plan.resources[0].sku == "container-xl"
    elif scenario == "excessive_quantity":
        assert plan.resources[0].type is ResourceType.CONTAINER
        assert plan.resources[0].quantity == 6
    else:
        assert plan.resources[0].type is ResourceType.OBJECT_STORAGE
        assert plan.resources[0].public_access is True
        assert plan.resources[0].capacity_gb == 100
        assert plan.resources[0].sku == "storage-standard"


def test_policy_only_problems_remain_on_the_validated_plan() -> None:
    raw = json.dumps(
        {
            "region": "eu-west-1",
            "environment": "dev",
            "tags": {"project": "demo"},
            "resources": [
                _container(sku="container-large", quantity=6),
                {
                    "type": "postgres",
                    "name": "database",
                    "sku": "db-small",
                    "quantity": 3,
                    "public_access": False,
                },
                _storage(sku="no-such-sku", capacity_gb=600, public_access=True),
            ],
        }
    )
    plan = _assert_unchanged(raw)

    assert plan.region == "eu-west-1"
    assert plan.tags == {"project": "demo"}
    assert [(resource.sku, resource.quantity, resource.capacity_gb, resource.public_access) for resource in plan.resources] == [
        ("container-large", 6, None, False),
        ("db-small", 3, None, False),
        ("no-such-sku", 1, 600, True),
    ]


def test_one_invalid_resource_does_not_return_a_partial_plan() -> None:
    storage = _storage()
    del storage["capacity_gb"]
    result = validate_raw_plan(_plan_json([_container(quantity=2), storage]))

    _assert_failure(result)
    issue = _issue_at(result, "resources[1]")
    assert issue.code == "value_error"
    assert issue.message == "object_storage requires capacity_gb"
    assert all(error.field_path != "resources[0]" for error in result.errors)


def test_json_array_fails_schema_validation_without_a_plan() -> None:
    result = validate_raw_plan("[1, 2]")

    _assert_failure(result)
    issue = result.errors[0]
    assert issue.code == "model_type"
    assert issue.field_path is None
    assert issue.message
