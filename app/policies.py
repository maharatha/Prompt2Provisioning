"""Infrastructure policy checks for a validated proposed plan.

Each function reports results and leaves the plan unchanged. Thresholds and
allow-lists are read from ``app/data/policy.json``. The functions still decide
what those values mean. A policy with no findings returns one passed result.
Findings are errors, except the configured higher-cost dev SKUs, which are
warnings. Unknown SKUs are not priced here.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.models import CheckStatus, Environment, PolicyCheck, ProposedPlan, ResourceType

_POLICY_PATH = Path(__file__).resolve().parent / "data" / "policy.json"
_LABELS = {
    "container": "Container",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "object_storage": "Object storage",
}


@dataclass(frozen=True)
class PolicyConfig:
    allowed_regions: tuple[str, ...]
    required_tags: tuple[str, ...]
    quantity_limits: tuple[tuple[str, int], ...]
    storage_gb: int
    dev_allowed_skus: dict[str, tuple[str, ...]]
    dev_warning_skus: dict[str, frozenset[str]]


def evaluate_policies(
    plan: ProposedPlan,
    *,
    config_path: Path | None = None,
) -> list[PolicyCheck]:
    """Return every policy result in a stable order."""
    config = load_policy(config_path)
    return [
        *check_allowed_regions(plan, config),
        *check_required_tags(plan, config),
        *check_resource_limits(plan, config),
        *check_dev_sku_tier(plan, config),
        *check_storage_public(plan),
        *check_dev_medium_cost(plan, config),
    ]


def load_policy(path: Path | None = None) -> PolicyConfig:
    """Load the policy allow-lists from JSON."""
    return _read_policy(_POLICY_PATH if path is None else path)


def check_allowed_regions(plan: ProposedPlan, config: PolicyConfig) -> list[PolicyCheck]:
    if plan.region in config.allowed_regions:
        return [
            _result(
                "allowed_regions",
                CheckStatus.PASSED,
                f"Region '{plan.region}' is allowed.",
            )
        ]
    allowed = _join(config.allowed_regions)
    return [
        _result(
            "allowed_regions",
            CheckStatus.ERROR,
            f"Region '{plan.region}' is not allowed. Allowed regions are {allowed}.",
            field_path="region",
        )
    ]


def check_required_tags(plan: ProposedPlan, config: PolicyConfig) -> list[PolicyCheck]:
    findings: list[PolicyCheck] = []
    for key in config.required_tags:
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
            f"Required tags {_join(config.required_tags)} are present and non-blank.",
        )
    ]


def check_resource_limits(plan: ProposedPlan, config: PolicyConfig) -> list[PolicyCheck]:
    totals = {type_name: 0 for type_name, _limit in config.quantity_limits}
    storage_total = 0
    missing_capacity: list[PolicyCheck] = []

    for index, resource in enumerate(plan.resources):
        type_name = resource.type.value
        if type_name in totals:
            totals[type_name] += resource.quantity
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
    for type_name, limit in config.quantity_limits:
        total = totals[type_name]
        if total > limit:
            findings.append(
                _result(
                    "resource_limits",
                    CheckStatus.ERROR,
                    f"Total {type_name} quantity is {total}, which exceeds the maximum of {limit}.",
                    field_path="resources",
                )
            )
    findings.extend(missing_capacity)
    if storage_total > config.storage_gb:
        findings.append(
            _result(
                "resource_limits",
                CheckStatus.ERROR,
                (
                    f"Total object storage capacity is {storage_total} GB, "
                    f"which exceeds the maximum of {config.storage_gb} GB."
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
            "Container, PostgreSQL, MySQL, and object storage capacity are within plan limits.",
        )
    ]


def check_dev_sku_tier(plan: ProposedPlan, config: PolicyConfig) -> list[PolicyCheck]:
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
        allowed = config.dev_allowed_skus.get(resource.type.value)
        if allowed is None or resource.sku in allowed:
            continue
        findings.append(
            _result(
                "dev_sku_tier",
                CheckStatus.ERROR,
                (
                    f"{_label(resource.type.value)} '{resource.name}' uses SKU '{resource.sku}', "
                    f"which is not allowed in dev. Allowed SKUs are {_join(allowed)}."
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
            "Dev SKUs are allowed.",
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


def check_dev_medium_cost(plan: ProposedPlan, config: PolicyConfig) -> list[PolicyCheck]:
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
        warnings = config.dev_warning_skus.get(resource.type.value)
        if warnings is None or resource.sku not in warnings:
            continue
        tier = "medium" if resource.sku.endswith("-medium") else "higher-cost"
        findings.append(
            _result(
                "dev_medium_cost",
                CheckStatus.WARNING,
                (
                    f"{_label(resource.type.value)} '{resource.name}' uses {tier} SKU "
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


def _read_policy(path: Path) -> PolicyConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Policy config could not be loaded: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Policy config must be a JSON object.")
    return PolicyConfig(
        allowed_regions=_string_list(payload, "allowed_regions"),
        required_tags=_string_list(payload, "required_tags"),
        quantity_limits=_quantity_limits(payload),
        storage_gb=_positive_int(payload, "object_storage_capacity_gb"),
        dev_allowed_skus=_sku_map(payload, "dev_allowed_skus"),
        dev_warning_skus={
            key: frozenset(values) for key, values in _sku_map(payload, "dev_warning_skus").items()
        },
    )


def _string_list(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"Policy config '{key}' must be a non-empty list of strings.")
    return tuple(value)


def _positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f"Policy config '{key}' must be a positive integer.")
    return value


def _quantity_limits(payload: dict[str, object]) -> tuple[tuple[str, int], ...]:
    value = payload.get("quantity_limits")
    if not isinstance(value, dict) or not value:
        raise ValueError("Policy config 'quantity_limits' must be a JSON object.")
    limits: list[tuple[str, int]] = []
    for type_name, limit in value.items():
        if not isinstance(type_name, str) or type(limit) is not int or limit <= 0:
            raise ValueError("Policy config quantity limits must map a type to a positive integer.")
        limits.append((type_name, limit))
    return tuple(limits)


def _sku_map(payload: dict[str, object], key: str) -> dict[str, tuple[str, ...]]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Policy config '{key}' must be a JSON object.")
    mapped: dict[str, tuple[str, ...]] = {}
    for type_name, skus in value.items():
        if not isinstance(type_name, str) or not isinstance(skus, list):
            raise ValueError(f"Policy config '{key}' entries must be lists of SKUs.")
        if not skus or not all(isinstance(sku, str) and sku for sku in skus):
            raise ValueError(f"Policy config '{key}' lists must contain SKU strings.")
        mapped[type_name] = tuple(skus)
    return mapped


def _join(items: tuple[str, ...] | list[str]) -> str:
    values = list(items)
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return ", ".join(values[:-1]) + f", and {values[-1]}"


def _label(type_name: str) -> str:
    return _LABELS.get(type_name, type_name)


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
