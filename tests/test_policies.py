import json
from pathlib import Path

import pytest

from app.models import CheckStatus, Environment, PolicyCheck, ProposedPlan, Resource, ResourceType
from app.policies import evaluate_policies

_POLICY_ORDER = (
    "allowed_regions",
    "required_tags",
    "resource_limits",
    "dev_sku_tier",
    "storage_public",
    "dev_medium_cost",
)

_CONTAINER_SKUS = "container-small, container-medium, and container-large"
_POSTGRES_SKUS = "db-small, db-medium, and db-large"

_LIMITS_PASSED = PolicyCheck(
    policy_id="resource_limits",
    status=CheckStatus.PASSED,
    message=(
        "Container, PostgreSQL, MySQL, and object storage capacity are within plan limits."
    ),
)


def _tags() -> dict[str, str]:
    return {
        "environment": "dev",
        "owner": "dev-team",
        "cost-center": "engineering",
    }


def _container(
    name: str = "web",
    sku: str = "container-small",
    quantity: int = 1,
    public_access: bool = False,
) -> Resource:
    return Resource(
        type=ResourceType.CONTAINER,
        name=name,
        sku=sku,
        quantity=quantity,
        public_access=public_access,
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
    public_access: bool = False,
) -> Resource:
    return Resource(
        type=ResourceType.OBJECT_STORAGE,
        name=name,
        sku=sku,
        quantity=quantity,
        capacity_gb=capacity_gb,
        public_access=public_access,
    )


def _plan(
    *,
    region: str = "us-east-1",
    environment: Environment = Environment.DEV,
    tags: dict[str, str] | None = None,
    resources: list[Resource] | None = None,
) -> ProposedPlan:
    if resources is None:
        resources = [
            _postgres(name="database", quantity=1),
            _container(name="web", quantity=2),
            _storage(name="assets", quantity=1, capacity_gb=100),
        ]
    return ProposedPlan(
        region=region,
        environment=environment,
        tags=dict(_tags() if tags is None else tags),
        resources=list(resources),
    )


def _evaluate(plan: ProposedPlan) -> list[PolicyCheck]:
    checks = evaluate_policies(plan)
    ranks = {policy_id: index for index, policy_id in enumerate(_POLICY_ORDER)}
    assert [ranks[check.policy_id] for check in checks] == sorted(
        ranks[check.policy_id] for check in checks
    )
    assert set(_POLICY_ORDER) <= {check.policy_id for check in checks}
    grouped: dict[str, list[PolicyCheck]] = {policy_id: [] for policy_id in _POLICY_ORDER}
    for check in checks:
        assert check.message.strip()
        assert check.policy_id in ranks
        grouped[check.policy_id].append(check)
        if check.status is CheckStatus.WARNING:
            assert check.policy_id == "dev_medium_cost"
        if check.policy_id == "dev_medium_cost":
            assert check.status is not CheckStatus.ERROR
    for group in grouped.values():
        statuses = {check.status for check in group}
        if CheckStatus.PASSED in statuses:
            assert statuses == {CheckStatus.PASSED}
            assert len(group) == 1
    return checks


def _for(checks: list[PolicyCheck], policy_id: str) -> list[PolicyCheck]:
    return [check for check in checks if check.policy_id == policy_id]


def _sku_error(resource: Resource, allowed: str, index: int = 0) -> PolicyCheck:
    label = "Container" if resource.type is ResourceType.CONTAINER else "PostgreSQL"
    return PolicyCheck(
        policy_id="dev_sku_tier",
        status=CheckStatus.ERROR,
        message=(
            f"{label} '{resource.name}' uses SKU '{resource.sku}', "
            f"which is not allowed in dev. Allowed SKUs are {allowed}."
        ),
        resource_name=resource.name,
        field_path=f"resources[{index}].sku",
    )


def _medium_warning(resource: Resource, index: int) -> PolicyCheck:
    label = "Container" if resource.type is ResourceType.CONTAINER else "PostgreSQL"
    return PolicyCheck(
        policy_id="dev_medium_cost",
        status=CheckStatus.WARNING,
        message=f"{label} '{resource.name}' uses medium SKU '{resource.sku}' in dev.",
        resource_name=resource.name,
        field_path=f"resources[{index}].sku",
    )


def _violating_plan() -> ProposedPlan:
    plan = _plan(
        region="eu-west-1",
        tags={"environment": "dev", "cost-center": "engineering"},
        resources=[
            _container(name="web-a", sku="container-xl", quantity=4),
            _container(name="web-b", sku="container-medium", quantity=2),
            _postgres(name="db-a", sku="db-medium", quantity=3),
            _postgres(name="db-b", sku="container-small", quantity=1),
            _storage(
                name="logs",
                quantity=2,
                capacity_gb=200,
                public_access=True,
            ),
            _storage(name="assets", quantity=1, capacity_gb=150, public_access=False),
        ],
    )
    plan.tags["cost-center"] = "   "
    return plan


def test_valid_plan_passes_every_policy() -> None:
    checks = _evaluate(_plan())

    assert [(check.policy_id, check.status) for check in checks] == [
        (policy_id, CheckStatus.PASSED) for policy_id in _POLICY_ORDER
    ]
    assert all(check.resource_name is None and check.field_path is None for check in checks)


@pytest.mark.parametrize("region", ["us-east-1", "us-west-2", "eastus2"])
def test_allowed_regions_pass(region: str) -> None:
    checks = _for(_evaluate(_plan(region=region)), "allowed_regions")

    assert checks == [
        PolicyCheck(
            policy_id="allowed_regions",
            status=CheckStatus.PASSED,
            message=f"Region '{region}' is allowed.",
        )
    ]


@pytest.mark.parametrize(
    "region",
    ["eu-west-1", "us-east-2", "eastus", "US-EAST-1", "us-east-1 ", "eastus2-extra"],
)
def test_unsupported_region_is_an_error(region: str) -> None:
    checks = _evaluate(_plan(region=region))

    assert _for(checks, "allowed_regions") == [
        PolicyCheck(
            policy_id="allowed_regions",
            status=CheckStatus.ERROR,
            message=(
                f"Region '{region}' is not allowed. "
                "Allowed regions are us-east-1, us-west-2, and eastus2."
            ),
            field_path="region",
        )
    ]
    assert [check.policy_id for check in checks if check.status is CheckStatus.ERROR] == [
        "allowed_regions"
    ]


@pytest.mark.parametrize("key", ["environment", "owner", "cost-center"])
def test_each_missing_required_tag_is_an_error(key: str) -> None:
    tags = _tags()
    del tags[key]
    checks = _for(_evaluate(_plan(tags=tags)), "required_tags")

    assert checks == [
        PolicyCheck(
            policy_id="required_tags",
            status=CheckStatus.ERROR,
            message=f"Required tag '{key}' is missing.",
            field_path=f"tags.{key}",
        )
    ]


@pytest.mark.parametrize("key", ["environment", "owner", "cost-center"])
@pytest.mark.parametrize("value", ["", " ", "   ", "\t", "\n", " \t\n "])
def test_each_blank_required_tag_is_an_error(key: str, value: str) -> None:
    plan = _plan()
    plan.tags[key] = value
    checks = _for(_evaluate(plan), "required_tags")

    assert checks == [
        PolicyCheck(
            policy_id="required_tags",
            status=CheckStatus.ERROR,
            message=f"Required tag '{key}' is blank.",
            field_path=f"tags.{key}",
        )
    ]


def test_missing_tags_are_reported_in_required_key_order() -> None:
    checks = _for(_evaluate(_plan(tags={"team": "platform"})), "required_tags")

    assert [check.field_path for check in checks] == [
        "tags.environment",
        "tags.owner",
        "tags.cost-center",
    ]
    assert [check.message for check in checks] == [
        "Required tag 'environment' is missing.",
        "Required tag 'owner' is missing.",
        "Required tag 'cost-center' is missing.",
    ]


def test_missing_and_blank_tags_are_reported_together() -> None:
    plan = _plan()
    del plan.tags["owner"]
    plan.tags["cost-center"] = "\t"
    checks = _for(_evaluate(plan), "required_tags")

    assert [(check.field_path, check.message) for check in checks] == [
        ("tags.owner", "Required tag 'owner' is missing."),
        ("tags.cost-center", "Required tag 'cost-center' is blank."),
    ]


def test_padded_required_tag_values_pass() -> None:
    checks = _for(
        _evaluate(
            _plan(
                tags={
                    "cost-center": " engineering ",
                    "environment": " dev ",
                    "owner": "\tplatform\t",
                    "team": "platform",
                }
            )
        ),
        "required_tags",
    )

    assert checks == [
        PolicyCheck(
            policy_id="required_tags",
            status=CheckStatus.PASSED,
            message="Required tags environment, owner, and cost-center are present and non-blank.",
        )
    ]


@pytest.mark.parametrize(
    "resources",
    [
        [_container(quantity=5)],
        [_container(name="web-a", quantity=2), _container(name="web-b", quantity=3)],
        [_postgres(quantity=2)],
        [_postgres(name="db-a", quantity=1), _postgres(name="db-b", quantity=1)],
        [_storage(quantity=1, capacity_gb=500)],
        [_storage(quantity=2, capacity_gb=250)],
        [_storage(quantity=5, capacity_gb=100)],
        [
            _storage(name="logs", quantity=1, capacity_gb=200),
            _storage(name="media", quantity=2, capacity_gb=150),
        ],
        [
            _container(name="web-a", quantity=2),
            _container(name="web-b", quantity=3),
            _postgres(name="db-a", quantity=1),
            _postgres(name="db-b", quantity=1),
            _storage(name="logs", quantity=2, capacity_gb=150),
            _storage(name="media", quantity=1, capacity_gb=200),
        ],
    ],
)
def test_quantities_at_the_limit_pass(resources: list[Resource]) -> None:
    assert _for(_evaluate(_plan(resources=resources)), "resource_limits") == [_LIMITS_PASSED]


@pytest.mark.parametrize(
    ("resources", "message"),
    [
        (
            [_container(quantity=6)],
            "Total container quantity is 6, which exceeds the maximum of 5.",
        ),
        (
            [_container(name="web-a", quantity=4), _container(name="web-b", quantity=2)],
            "Total container quantity is 6, which exceeds the maximum of 5.",
        ),
        (
            [_postgres(quantity=3)],
            "Total postgres quantity is 3, which exceeds the maximum of 2.",
        ),
        (
            [_postgres(name="db-a", quantity=2), _postgres(name="db-b", quantity=1)],
            "Total postgres quantity is 3, which exceeds the maximum of 2.",
        ),
        (
            [_storage(quantity=1, capacity_gb=501)],
            "Total object storage capacity is 501 GB, which exceeds the maximum of 500 GB.",
        ),
        (
            [_storage(quantity=2, capacity_gb=251)],
            "Total object storage capacity is 502 GB, which exceeds the maximum of 500 GB.",
        ),
        (
            [_storage(quantity=2, capacity_gb=500)],
            "Total object storage capacity is 1000 GB, which exceeds the maximum of 500 GB.",
        ),
        (
            [
                _storage(name="logs", quantity=1, capacity_gb=300),
                _storage(name="media", quantity=1, capacity_gb=201),
            ],
            "Total object storage capacity is 501 GB, which exceeds the maximum of 500 GB.",
        ),
    ],
)
def test_quantities_above_the_limit_fail(resources: list[Resource], message: str) -> None:
    checks = _for(_evaluate(_plan(resources=resources)), "resource_limits")

    assert checks == [
        PolicyCheck(
            policy_id="resource_limits",
            status=CheckStatus.ERROR,
            message=message,
            field_path="resources",
        )
    ]


def test_split_resources_cannot_bypass_limits() -> None:
    resources = [
        _container(name="web-a", quantity=3),
        _container(name="web-b", quantity=3),
        _postgres(name="db-a", quantity=1),
        _postgres(name="db-b", quantity=1),
        _postgres(name="db-c", quantity=1),
        _storage(name="logs", quantity=4, capacity_gb=100),
        _storage(name="media", quantity=2, capacity_gb=51),
    ]
    checks = _for(_evaluate(_plan(resources=resources)), "resource_limits")

    assert [check.message for check in checks] == [
        "Total container quantity is 6, which exceeds the maximum of 5.",
        "Total postgres quantity is 3, which exceeds the maximum of 2.",
        "Total object storage capacity is 502 GB, which exceeds the maximum of 500 GB.",
    ]
    assert all(check.status is CheckStatus.ERROR for check in checks)
    assert all(check.resource_name is None for check in checks)
    assert all(check.field_path == "resources" for check in checks)


def test_storage_limit_multiplies_capacity_by_quantity() -> None:
    passing = _for(
        _evaluate(_plan(resources=[_storage(quantity=4, capacity_gb=125)])),
        "resource_limits",
    )
    failing = _for(
        _evaluate(_plan(resources=[_storage(quantity=4, capacity_gb=126)])),
        "resource_limits",
    )

    assert passing == [_LIMITS_PASSED]
    assert failing == [
        PolicyCheck(
            policy_id="resource_limits",
            status=CheckStatus.ERROR,
            message="Total object storage capacity is 504 GB, which exceeds the maximum of 500 GB.",
            field_path="resources",
        )
    ]


def test_public_object_storage_is_rejected() -> None:
    checks = _for(
        _evaluate(
            _plan(
                resources=[
                    _container(public_access=True),
                    _storage(name="logs", public_access=True, capacity_gb=10),
                    _storage(name="assets", public_access=False, capacity_gb=10),
                ]
            )
        ),
        "storage_public",
    )

    assert checks == [
        PolicyCheck(
            policy_id="storage_public",
            status=CheckStatus.ERROR,
            message="Object storage 'logs' must have public_access set to false.",
            resource_name="logs",
            field_path="resources[1].public_access",
        )
    ]


def test_each_public_storage_resource_is_reported() -> None:
    checks = _for(
        _evaluate(
            _plan(
                resources=[
                    _storage(name="logs", public_access=True),
                    _container(),
                    _storage(name="media", public_access=True, capacity_gb=20),
                ]
            )
        ),
        "storage_public",
    )

    assert [check.resource_name for check in checks] == ["logs", "media"]
    assert [check.field_path for check in checks] == [
        "resources[0].public_access",
        "resources[2].public_access",
    ]


def test_private_object_storage_is_accepted() -> None:
    checks = _for(
        _evaluate(_plan(resources=[_storage(public_access=False), _container(public_access=True)])),
        "storage_public",
    )

    assert checks == [
        PolicyCheck(
            policy_id="storage_public",
            status=CheckStatus.PASSED,
            message="Object storage resources do not allow public access.",
        )
    ]


def test_dev_small_skus_pass_without_warnings() -> None:
    checks = _evaluate(
        _plan(
            resources=[
                _container(sku="container-small"),
                _postgres(sku="db-small"),
                _storage(sku="storage-standard"),
            ]
        )
    )

    assert all(check.status is CheckStatus.PASSED for check in checks)
    assert _for(checks, "dev_sku_tier") == [
        PolicyCheck(
            policy_id="dev_sku_tier",
            status=CheckStatus.PASSED,
            message="Dev SKUs are allowed.",
        )
    ]
    assert _for(checks, "dev_medium_cost") == [
        PolicyCheck(
            policy_id="dev_medium_cost",
            status=CheckStatus.PASSED,
            message="No dev medium-tier cost warning applies.",
        )
    ]


def test_dev_medium_skus_warn_without_errors() -> None:
    database = _postgres(name="database", sku="db-medium")
    web = _container(name="web", sku="container-medium")
    checks = _evaluate(_plan(resources=[database, web, _storage(sku="storage-standard")]))

    assert all(check.status is not CheckStatus.ERROR for check in checks)
    assert _for(checks, "dev_sku_tier")[0].status is CheckStatus.PASSED
    assert _for(checks, "dev_medium_cost") == [
        _medium_warning(database, 0),
        _medium_warning(web, 1),
    ]


@pytest.mark.parametrize(
    ("resource", "allowed"),
    [
        (_container(name="web", sku="container-xl"), _CONTAINER_SKUS),
        (_container(name="web", sku="container-small-extra"), _CONTAINER_SKUS),
        (_container(name="web", sku="container-medium-plus"), _CONTAINER_SKUS),
        (_container(name="web", sku="container-medium "), _CONTAINER_SKUS),
        (_container(name="web", sku="Container-small"), _CONTAINER_SKUS),
        (_container(name="web", sku="db-small"), _CONTAINER_SKUS),
        (_container(name="web", sku="db-medium"), _CONTAINER_SKUS),
        (_container(name="web", sku="storage-standard"), _CONTAINER_SKUS),
        (_container(name="web", sku="small"), _CONTAINER_SKUS),
        (_postgres(name="database", sku="db-xl"), _POSTGRES_SKUS),
        (_postgres(name="database", sku="db-smallish"), _POSTGRES_SKUS),
        (_postgres(name="database", sku=" db-medium"), _POSTGRES_SKUS),
        (_postgres(name="database", sku="container-small"), _POSTGRES_SKUS),
        (_postgres(name="database", sku="container-medium"), _POSTGRES_SKUS),
    ],
)
def test_dev_disallowed_or_incompatible_sku_is_an_error(resource: Resource, allowed: str) -> None:
    checks = _evaluate(_plan(resources=[resource]))

    assert _for(checks, "dev_sku_tier") == [_sku_error(resource, allowed)]
    assert _for(checks, "dev_medium_cost")[0].status is CheckStatus.PASSED


def test_each_disallowed_dev_sku_is_reported_in_resource_order() -> None:
    checks = _for(
        _evaluate(
            _plan(
                resources=[
                    _storage(name="assets", sku="storage-large"),
                    _container(name="web", sku="container-xl"),
                    _postgres(name="database", sku="db-small"),
                    _postgres(name="analytics", sku="db-xl"),
                ]
            )
        ),
        "dev_sku_tier",
    )

    assert [check.resource_name for check in checks] == ["web", "analytics"]
    assert [check.field_path for check in checks] == ["resources[1].sku", "resources[3].sku"]


@pytest.mark.parametrize("sku", ["storage-standard", "storage-large", "container-large", "db-medium"])
def test_object_storage_is_exempt_from_dev_sku_rules(sku: str) -> None:
    checks = _evaluate(_plan(resources=[_storage(name="assets", sku=sku)]))

    assert _for(checks, "dev_sku_tier")[0].status is CheckStatus.PASSED
    assert _for(checks, "dev_medium_cost")[0].status is CheckStatus.PASSED
    assert all(check.status is not CheckStatus.ERROR for check in checks)


@pytest.mark.parametrize("environment", [Environment.TEST, Environment.PROD])
@pytest.mark.parametrize(
    "resources",
    [
        [_container(sku="container-medium"), _postgres(sku="db-medium")],
        [_container(sku="container-large"), _postgres(sku="db-large")],
        [_storage(sku="storage-standard")],
    ],
)
def test_test_and_prod_do_not_apply_dev_sku_rules(
    environment: Environment,
    resources: list[Resource],
) -> None:
    checks = _evaluate(_plan(environment=environment, resources=resources))

    assert all(check.status is CheckStatus.PASSED for check in checks)
    assert _for(checks, "dev_sku_tier") == [
        PolicyCheck(
            policy_id="dev_sku_tier",
            status=CheckStatus.PASSED,
            message=f"Dev SKU tier does not apply to the {environment.value} environment.",
        )
    ]
    assert _for(checks, "dev_medium_cost") == [
        PolicyCheck(
            policy_id="dev_medium_cost",
            status=CheckStatus.PASSED,
            message=(
                f"Dev medium-tier warnings do not apply to the {environment.value} environment."
            ),
        )
    ]


def test_multiple_violations_are_returned_together() -> None:
    web_b = _container(name="web-b", sku="container-medium", quantity=2)
    db_a = _postgres(name="db-a", sku="db-medium", quantity=3)
    checks = _evaluate(_violating_plan())

    assert checks == [
        PolicyCheck(
            policy_id="allowed_regions",
            status=CheckStatus.ERROR,
            message=(
                "Region 'eu-west-1' is not allowed. "
                "Allowed regions are us-east-1, us-west-2, and eastus2."
            ),
            field_path="region",
        ),
        PolicyCheck(
            policy_id="required_tags",
            status=CheckStatus.ERROR,
            message="Required tag 'owner' is missing.",
            field_path="tags.owner",
        ),
        PolicyCheck(
            policy_id="required_tags",
            status=CheckStatus.ERROR,
            message="Required tag 'cost-center' is blank.",
            field_path="tags.cost-center",
        ),
        PolicyCheck(
            policy_id="resource_limits",
            status=CheckStatus.ERROR,
            message="Total container quantity is 6, which exceeds the maximum of 5.",
            field_path="resources",
        ),
        PolicyCheck(
            policy_id="resource_limits",
            status=CheckStatus.ERROR,
            message="Total postgres quantity is 4, which exceeds the maximum of 2.",
            field_path="resources",
        ),
        PolicyCheck(
            policy_id="resource_limits",
            status=CheckStatus.ERROR,
            message="Total object storage capacity is 550 GB, which exceeds the maximum of 500 GB.",
            field_path="resources",
        ),
        _sku_error(_container(name="web-a", sku="container-xl"), _CONTAINER_SKUS, index=0),
        _sku_error(_postgres(name="db-b", sku="container-small"), _POSTGRES_SKUS, index=3),
        PolicyCheck(
            policy_id="storage_public",
            status=CheckStatus.ERROR,
            message="Object storage 'logs' must have public_access set to false.",
            resource_name="logs",
            field_path="resources[4].public_access",
        ),
        _medium_warning(web_b, 1),
        _medium_warning(db_a, 2),
    ]


def test_results_are_deterministic_and_input_is_unchanged() -> None:
    plan = _violating_plan()
    before = plan.model_dump(mode="json")
    tag_id = id(plan.tags)
    resource_ids = [id(resource) for resource in plan.resources]

    first = evaluate_policies(plan)
    second = evaluate_policies(plan)

    assert plan.model_dump(mode="json") == before
    assert id(plan.tags) == tag_id
    assert [id(resource) for resource in plan.resources] == resource_ids
    assert [check.model_dump() for check in first] == [check.model_dump() for check in second]
    assert first == _evaluate(plan)


def test_policy_file_can_remove_an_allowed_region(tmp_path: Path) -> None:
    source = json.loads(
        (Path(__file__).resolve().parents[1] / "app" / "data" / "policy.json").read_text(
            encoding="utf-8"
        )
    )
    source["allowed_regions"] = ["us-west-2"]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    checks = evaluate_policies(_plan(region="us-east-1"), config_path=path)

    assert _for(checks, "allowed_regions")[0].status is CheckStatus.ERROR
    assert "us-west-2" in _for(checks, "allowed_regions")[0].message


def test_configured_large_database_warns_and_mysql_is_limited() -> None:
    database = _postgres(name="database", sku="db-large")
    mysql = Resource(
        type=ResourceType.MYSQL,
        name="mysql",
        sku="mysql-large",
        quantity=3,
    )
    checks = _evaluate(_plan(resources=[database, mysql]))

    assert _for(checks, "dev_sku_tier")[0].status is CheckStatus.PASSED
    assert _for(checks, "dev_medium_cost")[0].status is CheckStatus.WARNING
    assert "higher-cost" in _for(checks, "dev_medium_cost")[0].message
    assert _for(checks, "resource_limits") == [
        PolicyCheck(
            policy_id="resource_limits",
            status=CheckStatus.ERROR,
            message="Total mysql quantity is 3, which exceeds the maximum of 2.",
            field_path="resources",
        )
    ]


def test_missing_storage_capacity_fails_closed() -> None:
    plan = _plan(resources=[_storage(name="assets")])
    plan.resources[0].capacity_gb = None
    checks = _for(_evaluate(plan), "resource_limits")

    assert checks == [
        PolicyCheck(
            policy_id="resource_limits",
            status=CheckStatus.ERROR,
            message="Object storage 'assets' is missing capacity_gb.",
            resource_name="assets",
            field_path="resources[0].capacity_gb",
        )
    ]
