"""Infrastructure policy checks for a validated proposed plan.

Each function reports results and leaves the plan unchanged. A policy with no
findings returns one passed result. Findings are errors, except dev medium-tier
SKUs, which are warnings. Unknown SKUs are not priced here.
"""

from app.models import CheckStatus, Environment, PolicyCheck, ProposedPlan, ResourceType

_ALLOWED_REGIONS = frozenset({"us-east-1", "us-west-2", "eastus2"})
_REQUIRED_TAGS = ("environment", "owner", "cost-center")

_MAX_CONTAINER_QUANTITY = 5
_MAX_POSTGRES_QUANTITY = 2
_MAX_STORAGE_GB = 500

_CONTAINER_SKUS = frozenset({"container-small", "container-medium"})
_POSTGRES_SKUS = frozenset({"db-small", "db-medium"})
_CONTAINER_MEDIUM_SKU = "container-medium"
_POSTGRES_MEDIUM_SKU = "db-medium"


def evaluate_policies(plan: ProposedPlan) -> list[PolicyCheck]:
    """Return every policy result in a stable order."""
    checks = [
        *check_allowed_regions(plan),
        *check_required_tags(plan),
        *check_resource_limits(plan),
        *check_dev_sku_tier(plan),
        *check_storage_public(plan),
        *check_dev_medium_cost(plan),
    ]
    return checks


def check_allowed_regions(plan: ProposedPlan) -> list[PolicyCheck]:
    if plan.region in _ALLOWED_REGIONS:
        return [
            _result(
                "allowed_regions",
                CheckStatus.PASSED,
                f"Region '{plan.region}' is allowed.",
            )
        ]
    return [
        _result(
            "allowed_regions",
            CheckStatus.ERROR,
            (
                f"Region '{plan.region}' is not allowed. "
                "Allowed regions are us-east-1, us-west-2, and eastus2."
            ),
            field_path="region",
        )
    ]


def check_required_tags(plan: ProposedPlan) -> list[PolicyCheck]:
    findings: list[PolicyCheck] = []
    for key in _REQUIRED_TAGS:
        field_path = f"tags.{key}"
        if key not in plan.tags:
            findings.append(
                _result(
                    "required_tags",
                    CheckStatus.ERROR,
                    f"Required tag '{key}' is missing.",
                    field_path=field_path,
                )
            )
        elif plan.tags[key].strip() == "":
            findings.append(
                _result(
                    "required_tags",
                    CheckStatus.ERROR,
                    f"Required tag '{key}' is blank.",
                    field_path=field_path,
                )
            )
    if findings:
        return findings
    return [
        _result(
            "required_tags",
            CheckStatus.PASSED,
            "Required tags environment, owner, and cost-center are present and non-blank.",
        )
    ]


def check_resource_limits(plan: ProposedPlan) -> list[PolicyCheck]:
    container_total = 0
    postgres_total = 0
    storage_total = 0
    missing_capacity: list[PolicyCheck] = []

    for index, resource in enumerate(plan.resources):
        if resource.type is ResourceType.CONTAINER:
            container_total += resource.quantity
        elif resource.type is ResourceType.POSTGRES:
            postgres_total += resource.quantity
        elif resource.type is ResourceType.OBJECT_STORAGE:
            capacity_gb = resource.capacity_gb
            if capacity_gb is None:
                missing_capacity.append(
                    _result(
                        "resource_limits",
                        CheckStatus.ERROR,
                        f"Object storage '{resource.name}' is missing capacity_gb.",
                        resource_name=resource.name,
                        field_path=f"resources[{index}].capacity_gb",
                    )
                )
            else:
                storage_total += capacity_gb * resource.quantity

    findings: list[PolicyCheck] = []
    if container_total > _MAX_CONTAINER_QUANTITY:
        findings.append(
            _result(
                "resource_limits",
                CheckStatus.ERROR,
                (
                    f"Total container quantity is {container_total}, "
                    f"which exceeds the maximum of {_MAX_CONTAINER_QUANTITY}."
                ),
                field_path="resources",
            )
        )
    if postgres_total > _MAX_POSTGRES_QUANTITY:
        findings.append(
            _result(
                "resource_limits",
                CheckStatus.ERROR,
                (
                    f"Total postgres quantity is {postgres_total}, "
                    f"which exceeds the maximum of {_MAX_POSTGRES_QUANTITY}."
                ),
                field_path="resources",
            )
        )
    findings.extend(missing_capacity)
    if storage_total > _MAX_STORAGE_GB:
        findings.append(
            _result(
                "resource_limits",
                CheckStatus.ERROR,
                (
                    f"Total object storage capacity is {storage_total} GB, "
                    f"which exceeds the maximum of {_MAX_STORAGE_GB} GB."
                ),
                field_path="resources",
            )
        )
    if findings:
        return findings
    return [
        _result(
            "resource_limits",
            CheckStatus.PASSED,
            "Container quantity, postgres quantity, and object storage capacity are within plan limits.",
        )
    ]


def check_dev_sku_tier(plan: ProposedPlan) -> list[PolicyCheck]:
    if plan.environment is not Environment.DEV:
        return [
            _result(
                "dev_sku_tier",
                CheckStatus.PASSED,
                f"Dev SKU tier does not apply to the {plan.environment.value} environment.",
            )
        ]

    findings: list[PolicyCheck] = []
    for index, resource in enumerate(plan.resources):
        allowed = _dev_skus(resource.type)
        if allowed is None or resource.sku in allowed:
            continue
        findings.append(
            _result(
                "dev_sku_tier",
                CheckStatus.ERROR,
                (
                    f"{_resource_label(resource.type)} '{resource.name}' uses SKU '{resource.sku}', "
                    f"which is not allowed in dev. Allowed SKUs are {_allowed_sku_text(resource.type)}."
                ),
                resource_name=resource.name,
                field_path=f"resources[{index}].sku",
            )
        )
    if findings:
        return findings
    return [
        _result(
            "dev_sku_tier",
            CheckStatus.PASSED,
            "Dev container and postgres SKUs are allowed.",
        )
    ]


def check_storage_public(plan: ProposedPlan) -> list[PolicyCheck]:
    findings: list[PolicyCheck] = []
    for index, resource in enumerate(plan.resources):
        if resource.type is not ResourceType.OBJECT_STORAGE or not resource.public_access:
            continue
        findings.append(
            _result(
                "storage_public",
                CheckStatus.ERROR,
                f"Object storage '{resource.name}' must have public_access set to false.",
                resource_name=resource.name,
                field_path=f"resources[{index}].public_access",
            )
        )
    if findings:
        return findings
    return [
        _result(
            "storage_public",
            CheckStatus.PASSED,
            "Object storage resources do not allow public access.",
        )
    ]


def check_dev_medium_cost(plan: ProposedPlan) -> list[PolicyCheck]:
    if plan.environment is not Environment.DEV:
        return [
            _result(
                "dev_medium_cost",
                CheckStatus.PASSED,
                f"Dev medium-tier warnings do not apply to the {plan.environment.value} environment.",
            )
        ]

    findings: list[PolicyCheck] = []
    for index, resource in enumerate(plan.resources):
        medium_sku = _medium_sku(resource.type)
        if medium_sku is None or resource.sku != medium_sku:
            continue
        findings.append(
            _result(
                "dev_medium_cost",
                CheckStatus.WARNING,
                (
                    f"{_resource_label(resource.type)} '{resource.name}' uses medium SKU "
                    f"'{resource.sku}' in dev."
                ),
                resource_name=resource.name,
                field_path=f"resources[{index}].sku",
            )
        )
    if findings:
        return findings
    return [
        _result(
            "dev_medium_cost",
            CheckStatus.PASSED,
            "No dev medium-tier cost warning applies.",
        )
    ]


def _dev_skus(resource_type: ResourceType) -> frozenset[str] | None:
    if resource_type is ResourceType.CONTAINER:
        return _CONTAINER_SKUS
    if resource_type is ResourceType.POSTGRES:
        return _POSTGRES_SKUS
    return None


def _medium_sku(resource_type: ResourceType) -> str | None:
    if resource_type is ResourceType.CONTAINER:
        return _CONTAINER_MEDIUM_SKU
    if resource_type is ResourceType.POSTGRES:
        return _POSTGRES_MEDIUM_SKU
    return None


def _allowed_sku_text(resource_type: ResourceType) -> str:
    if resource_type is ResourceType.CONTAINER:
        return "container-small and container-medium"
    if resource_type is ResourceType.POSTGRES:
        return "db-small and db-medium"
    return ""


def _resource_label(resource_type: ResourceType) -> str:
    if resource_type is ResourceType.CONTAINER:
        return "Container"
    if resource_type is ResourceType.POSTGRES:
        return "PostgreSQL"
    return "Object storage"


def _result(
    policy_id: str,
    status: CheckStatus,
    message: str,
    *,
    resource_name: str | None = None,
    field_path: str | None = None,
) -> PolicyCheck:
    return PolicyCheck(
        policy_id=policy_id,
        status=status,
        message=message,
        resource_name=resource_name,
        field_path=field_path,
    )
