import json

import pytest
from pydantic import ValidationError

from app.models import Environment, ProposedPlan, ResourceType
from app.planner import MockPlanner

EXAMPLE_PROMPT = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)

REQUIRED_TAG_KEYS = ("environment", "owner", "cost-center")

SCENARIO_ALIASES = (
    ("malformed_json", "malformed"),
    ("missing_region", "missing_region"),
    ("missing_tags", "missing_tags"),
    ("unknown_resource_type", "unknown_type"),
    ("unsupported_sku", "unsupported_sku"),
    ("excessive_quantity", "excessive_qty"),
    ("public_object_storage", "public_storage"),
)


@pytest.fixture
def planner() -> MockPlanner:
    return MockPlanner()


def _load(raw: str) -> dict[str, object]:
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def _resources(payload: dict[str, object]) -> list[dict[str, object]]:
    resources = payload["resources"]
    assert isinstance(resources, list)
    assert all(isinstance(resource, dict) for resource in resources)
    return resources


def test_assignment_example(planner: MockPlanner) -> None:
    raw = planner.generate(EXAMPLE_PROMPT)
    payload = _load(raw)
    plan = ProposedPlan.model_validate(payload)

    assert plan.region == "us-east-1"
    assert plan.environment is Environment.DEV
    assert plan.tags == {
        "environment": "dev",
        "owner": "dev-team",
        "cost-center": "engineering",
    }
    assert [(resource.type, resource.name, resource.sku, resource.quantity) for resource in plan.resources] == [
        (ResourceType.POSTGRES, "database", "db-small", 1),
        (ResourceType.CONTAINER, "web", "container-small", 2),
    ]
    assert all(resource.public_access is False for resource in plan.resources)
    assert all(resource.capacity_gb is None for resource in plan.resources)
    assert "status" not in payload
    assert "cost" not in payload
    assert "plan_hash" not in payload


def test_one_database_and_two_containers_keep_separate_quantities(planner: MockPlanner) -> None:
    payload = _load(planner.generate("one database and two web containers"))
    plan = ProposedPlan.model_validate(payload)

    by_type = {resource.type: resource.quantity for resource in plan.resources}
    assert by_type == {ResourceType.POSTGRES: 1, ResourceType.CONTAINER: 2}


def test_quantities_stay_bound_when_containers_are_mentioned_first(planner: MockPlanner) -> None:
    payload = _load(planner.generate("two web containers and one database"))
    resources = _resources(payload)

    assert [resource["type"] for resource in resources] == ["postgres", "container"]
    assert [resource["quantity"] for resource in resources] == [1, 2]


def test_all_three_resource_types_regions_and_sizes(planner: MockPlanner) -> None:
    payload = _load(
        planner.generate(
            "one small database, two medium web containers, and 100 GB object storage "
            "in Azure East US for production"
        )
    )
    plan = ProposedPlan.model_validate(payload)

    assert plan.region == "eastus2"
    assert plan.environment is Environment.PROD
    assert plan.tags["environment"] == "prod"
    assert plan.tags["owner"] == "dev-team"
    assert plan.tags["cost-center"] == "engineering"
    assert [(resource.type, resource.name, resource.sku, resource.quantity, resource.capacity_gb) for resource in plan.resources] == [
        (ResourceType.POSTGRES, "database", "db-small", 1, None),
        (ResourceType.CONTAINER, "web", "container-medium", 2, None),
        (ResourceType.OBJECT_STORAGE, "bucket", "storage-standard", 1, 100),
    ]


@pytest.mark.parametrize(
    ("phrase", "region"),
    [
        ("US East", "us-east-1"),
        ("us east", "us-east-1"),
        ("US West", "us-west-2"),
        ("Azure East US", "eastus2"),
    ],
)
def test_region_mappings(planner: MockPlanner, phrase: str, region: str) -> None:
    payload = _load(planner.generate(f"one database in {phrase}"))
    assert payload["region"] == region


@pytest.mark.parametrize(
    ("phrase", "environment"),
    [
        ("dev", "dev"),
        ("development", "dev"),
        ("test", "test"),
        ("prod", "prod"),
        ("production", "prod"),
    ],
)
def test_environment_words(planner: MockPlanner, phrase: str, environment: str) -> None:
    payload = _load(planner.generate(f"one database for {phrase}"))
    assert payload["environment"] == environment
    assert payload["tags"] == {
        "environment": environment,
        "owner": "dev-team",
        "cost-center": "engineering",
    }


def test_integer_quantities_are_copied_as_written(planner: MockPlanner) -> None:
    zero = _resources(_load(planner.generate("0 web containers")))[0]
    assert zero["quantity"] == 0

    large = _resources(_load(planner.generate("101 containers")))[0]
    assert large["quantity"] == 101


@pytest.mark.parametrize(
    ("phrase", "quantity"),
    [
        ("one", 1),
        ("two", 2),
        ("three", 3),
        ("4", 4),
    ],
)
def test_container_quantity_words_and_numbers(planner: MockPlanner, phrase: str, quantity: int) -> None:
    payload = _load(planner.generate(f"{phrase} web containers"))
    resource = _resources(payload)[0]
    assert resource["type"] == "container"
    assert resource["quantity"] == quantity
    assert resource["sku"] == "container-small"


def test_resource_phrases_map_to_stable_names(planner: MockPlanner) -> None:
    payload = _load(planner.generate("postgres, a web application, and object storage"))
    plan = ProposedPlan.model_validate(payload)
    assert [(resource.type, resource.name, resource.sku) for resource in plan.resources] == [
        (ResourceType.POSTGRES, "database", "db-small"),
        (ResourceType.CONTAINER, "web", "container-small"),
        (ResourceType.OBJECT_STORAGE, "bucket", "storage-standard"),
    ]
    storage = plan.resources[2]
    assert storage.capacity_gb == 100
    assert storage.quantity == 1


def test_medium_and_small_bind_to_their_own_resources(planner: MockPlanner) -> None:
    payload = _load(planner.generate("a medium database and two small web containers"))
    resources = _resources(payload)
    assert [(resource["type"], resource["sku"], resource["quantity"]) for resource in resources] == [
        ("postgres", "db-medium", 1),
        ("container", "container-small", 2),
    ]


def test_object_storage_sku_stays_storage_standard_for_medium(planner: MockPlanner) -> None:
    payload = _load(planner.generate("medium object storage"))
    resource = _resources(payload)[0]
    assert resource["sku"] == "storage-standard"
    assert resource["capacity_gb"] == 100


def test_defaults_when_optional_words_are_omitted(planner: MockPlanner) -> None:
    payload = _load(planner.generate("database"))
    plan = ProposedPlan.model_validate(payload)
    assert plan.region == "us-east-1"
    assert plan.environment is Environment.DEV
    assert plan.tags == {
        "environment": "dev",
        "owner": "dev-team",
        "cost-center": "engineering",
    }
    assert len(plan.resources) == 1
    resource = plan.resources[0]
    assert resource.type is ResourceType.POSTGRES
    assert resource.sku == "db-small"
    assert resource.quantity == 1
    assert resource.public_access is False


def test_unrecognized_prompt_returns_the_default_container(planner: MockPlanner) -> None:
    payload = _load(planner.generate("hello world"))
    plan = ProposedPlan.model_validate(payload)
    assert plan.region == "us-east-1"
    assert plan.environment is Environment.DEV
    assert [(resource.type, resource.name, resource.sku, resource.quantity) for resource in plan.resources] == [
        (ResourceType.CONTAINER, "web", "container-small", 1),
    ]


def test_owner_and_cost_center_are_not_read_from_the_prompt(planner: MockPlanner) -> None:
    payload = _load(planner.generate("database for owner alice and cost-center finance"))
    assert payload["tags"] == {
        "environment": "dev",
        "owner": "dev-team",
        "cost-center": "engineering",
    }


def test_low_cost_uses_small_skus_without_replacing_medium(planner: MockPlanner) -> None:
    low_cost = _load(planner.generate("two web containers optimized for low cost"))
    assert _resources(low_cost)[0]["sku"] == "container-small"

    mixed = _load(planner.generate("a medium database and two web containers, optimized for low cost"))
    assert [(resource["type"], resource["sku"]) for resource in _resources(mixed)] == [
        ("postgres", "db-medium"),
        ("container", "container-small"),
    ]


def test_leftmost_region_and_environment_win(planner: MockPlanner) -> None:
    regions = _load(planner.generate("one database in US West or US East"))
    assert regions["region"] == "us-west-2"

    environments = _load(planner.generate("one database for prod and dev"))
    assert environments["environment"] == "prod"
    assert environments["tags"]["environment"] == "prod"


def test_rightmost_quantity_and_size_in_the_window_win(planner: MockPlanner) -> None:
    quantity = _load(planner.generate("one or two web containers"))
    assert _resources(quantity)[0]["quantity"] == 2

    size = _load(planner.generate("small or medium web containers"))
    assert _resources(size)[0]["sku"] == "container-medium"


def test_first_mention_of_a_resource_type_wins(planner: MockPlanner) -> None:
    payload = _load(planner.generate("two small containers and three medium containers"))
    resources = _resources(payload)
    assert len(resources) == 1
    assert resources[0]["quantity"] == 2
    assert resources[0]["sku"] == "container-small"


def test_size_and_quantity_after_the_phrase_are_ignored(planner: MockPlanner) -> None:
    payload = _load(planner.generate("web containers that are medium, quantity three"))
    resource = _resources(payload)[0]
    assert resource["sku"] == "container-small"
    assert resource["quantity"] == 1


def test_gigabytes_set_storage_capacity_and_not_a_count(planner: MockPlanner) -> None:
    storage = _load(planner.generate("two 50 GB object storage"))
    resource = _resources(storage)[0]
    assert resource["quantity"] == 2
    assert resource["capacity_gb"] == 50

    spelled = _load(planner.generate("80 gigabytes of object storage"))
    assert _resources(spelled)[0]["quantity"] == 1
    assert _resources(spelled)[0]["capacity_gb"] == 80

    database = _load(planner.generate("100 GB database"))
    plan = ProposedPlan.model_validate(database)
    assert plan.resources[0].type is ResourceType.POSTGRES
    assert plan.resources[0].quantity == 1
    assert plan.resources[0].capacity_gb is None


def test_storage_without_object_storage_is_not_a_bucket(planner: MockPlanner) -> None:
    payload = _load(planner.generate("100 GB storage"))
    resource = _resources(payload)[0]
    assert resource["type"] == "container"
    assert "capacity_gb" not in resource


def test_region_phrases_need_the_documented_wording(planner: MockPlanner) -> None:
    doubled_space = _load(planner.generate("one database in US  East"))
    assert doubled_space["region"] == "us-east-1"

    east_us = _load(planner.generate("one database in East US"))
    assert east_us["region"] == "us-east-1"

    azure = _load(planner.generate("one database in Azure East US"))
    assert azure["region"] == "eastus2"


def test_public_word_does_not_mark_storage_public(planner: MockPlanner) -> None:
    payload = _load(planner.generate("public object storage in US East"))
    resource = _resources(payload)[0]
    assert resource["type"] == "object_storage"
    assert resource["public_access"] is False


def test_identical_inputs_produce_identical_raw_output(planner: MockPlanner) -> None:
    first = planner.generate(EXAMPLE_PROMPT)
    second = planner.generate(EXAMPLE_PROMPT)
    other = MockPlanner().generate(EXAMPLE_PROMPT)
    assert first == second == other
    assert first.endswith("\n")

    mixed_case = planner.generate("TWO WEB CONTAINERS IN US WEST FOR TEST")
    lower = planner.generate("two web containers in us west for test")
    assert mixed_case == lower


def test_malformed_json_scenario_is_not_json(planner: MockPlanner) -> None:
    raw = planner.generate("one small database in US East", scenario="malformed_json")
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert raw == planner.generate("different prompt", scenario="malformed_json")
    assert "resources" in raw


def test_missing_region_scenario_omits_region(planner: MockPlanner) -> None:
    payload = _load(planner.generate("database in US East", scenario="missing_region"))
    assert "region" not in payload
    assert payload["resources"]
    with pytest.raises(ValidationError):
        ProposedPlan.model_validate(payload)


def test_missing_tags_scenario_omits_required_keys(planner: MockPlanner) -> None:
    payload = _load(planner.generate("database", scenario="missing_tags"))
    tags = payload["tags"]
    assert isinstance(tags, dict)
    for key in REQUIRED_TAG_KEYS:
        assert key not in tags
    ProposedPlan.model_validate(payload)


def test_unknown_resource_type_scenario_emits_vm(planner: MockPlanner) -> None:
    payload = _load(planner.generate("one container", scenario="unknown_resource_type"))
    assert _resources(payload)[0]["type"] == "vm"
    with pytest.raises(ValidationError):
        ProposedPlan.model_validate(payload)


def test_unsupported_sku_scenario_keeps_container_large(planner: MockPlanner) -> None:
    payload = _load(
        planner.generate(
            "A small PostgreSQL database and two web containers in US East",
            scenario="unsupported_sku",
        )
    )
    resources = _resources(payload)
    assert len(resources) == 1
    assert resources[0]["sku"] == "container-large"
    assert resources[0]["sku"] != "container-small"
    plan = ProposedPlan.model_validate(payload)
    assert plan.environment is Environment.PROD
    assert plan.resources[0].sku == "container-large"


def test_excessive_quantity_scenario_emits_six_containers(planner: MockPlanner) -> None:
    payload = _load(planner.generate("one container", scenario="excessive_quantity"))
    resource = _resources(payload)[0]
    assert resource["type"] == "container"
    assert resource["quantity"] == 6
    ProposedPlan.model_validate(payload)


def test_public_object_storage_scenario_sets_public_access(planner: MockPlanner) -> None:
    payload = _load(planner.generate("object storage", scenario="public_object_storage"))
    resource = _resources(payload)[0]
    assert resource["type"] == "object_storage"
    assert resource["public_access"] is True
    assert resource["capacity_gb"] == 100
    assert resource["sku"] == "storage-standard"
    ProposedPlan.model_validate(payload)


@pytest.mark.parametrize(("canonical", "alias"), SCENARIO_ALIASES)
def test_scenario_argument_and_prompt_token_return_the_same_string(
    planner: MockPlanner,
    canonical: str,
    alias: str,
) -> None:
    via_argument = planner.generate("one database in US East", scenario=canonical)
    via_alias = planner.generate(f"SCENARIO:{alias} one database in US East")
    via_canonical = planner.generate(f"please SCENARIO:{canonical} and then a database")
    assert via_argument == via_alias == via_canonical
    assert via_argument == planner.generate("unrelated", scenario=alias)


def test_explicit_scenario_wins_over_prompt_token(planner: MockPlanner) -> None:
    payload = _load(planner.generate("SCENARIO:malformed", scenario="missing_region"))
    assert "region" not in payload


def test_lowercase_scenario_prefix_is_parsed_as_vocabulary(planner: MockPlanner) -> None:
    payload = _load(planner.generate("scenario:malformed two web containers"))
    resource = _resources(payload)[0]
    assert resource["type"] == "container"
    assert resource["quantity"] == 2


def test_unknown_scenario_argument_raises(planner: MockPlanner) -> None:
    with pytest.raises(ValueError, match="unknown planner scenario: nope"):
        planner.generate("two web containers", scenario="nope")


def test_unknown_prompt_scenario_raises_without_parsing_vocabulary(planner: MockPlanner) -> None:
    with pytest.raises(ValueError, match="unknown planner scenario: nope"):
        planner.generate("SCENARIO:nope two web containers in US East")
