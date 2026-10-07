"""Deterministic mock planner.

``MockPlanner.generate`` returns one raw JSON string. It does not validate that
string, apply policy, price a catalog, hash a plan, read the store, approve a
plan, or render an artifact. It does not instantiate Pydantic models.

Vocabulary
----------
Matching is case-insensitive. Only the phrases below are recognized. Other
words are ignored. This is not a general language parser.

Resource phrases, longer phrases first:

- postgres: ``postgresql``, ``postgres``, ``databases``, ``database``
- container: ``web applications``, ``web application``, ``web containers``,
  ``web container``, ``containers``, ``container``
- object storage: ``object storage``

Quantity and size are taken from the text after the previous resource phrase
and before the current phrase. In that window the rightmost quantity wins, the
rightmost size wins, and the rightmost GB amount wins. Quantities are ``one``,
``two``, ``three``, and a plain integer. The integer is copied as written,
including 0 and values above 100. Sizes are ``small`` and ``medium``.
``N gb`` or ``N gigabytes`` is object-storage capacity, not a resource count.

Regions, leftmost phrase wins. The phrase must use a single space:

- ``US East`` -> ``us-east-1``
- ``US West`` -> ``us-west-2``
- ``Azure East US`` -> ``eastus2``

Environments, leftmost word wins: ``development`` or ``dev``, ``test``,
``production`` or ``prod``.

Defaults
--------
- region ``us-east-1`` when no region phrase is present
- environment ``dev`` when no environment word is present
- tier ``small`` when a resource's window has no size word
- ``low cost`` asks for that same small tier and does not replace a ``small``
  or ``medium`` written in a resource's own window
- quantity ``1`` when a resource's window has no quantity
- object-storage capacity ``100`` when no GB amount is present
- object-storage SKU ``storage-standard`` for both small and medium
- names ``database``, ``web``, and ``bucket``
- resource order: postgres, then container, then object storage
- tags ``environment`` (the chosen environment), ``owner=dev-team``,
  ``cost-center=engineering``
- ``public_access`` false

Owner and cost-center are fixed synthetic defaults. They are not read from the
prompt, and emitting them is not a policy check.

When the prompt names no resource phrase, the plan is one small ``web``
container with the defaults above.

Ambiguity
---------
- Two region phrases: the leftmost phrase is used.
- Two environment words: the leftmost word is used.
- Two quantities or two sizes in one window: the rightmost value is used.
- The same resource type mentioned twice: the first mention supplies quantity,
  size, and capacity. Later mentions of that type are ignored.
- A size or quantity that appears only after its resource phrase is ignored.
- A GB amount on a database or container is not used as that resource's quantity.
- ``storage`` without the words ``object storage`` is not object storage.
- An unrecognized prompt still returns the default plan. It does not raise.

Scenarios
---------
Pass ``scenario`` or include ``SCENARIO:<name>`` in the prompt. Either form
skips vocabulary parsing and returns a fixed string. The string is not parsed
and not repaired. An explicit ``scenario`` argument wins when both are present.
Names are case-sensitive. An unknown name raises ``UnknownScenarioError``.

- ``malformed_json`` (prompt alias ``malformed``): truncated JSON
- ``missing_region``: JSON object with the region key omitted
- ``missing_tags``: tags omit ``environment``, ``owner``, and ``cost-center``
- ``unknown_resource_type`` (prompt alias ``unknown_type``): resource type ``vm``
- ``unsupported_sku``: SKU ``container-large``, which this planner does not replace
- ``excessive_quantity`` (prompt alias ``excessive_qty``): container quantity ``6``
- ``public_object_storage`` (prompt alias ``public_storage``): object storage
  with ``public_access`` true
"""

import json
import re
from typing import Protocol

DEFAULT_REGION = "us-east-1"
DEFAULT_ENVIRONMENT = "dev"
DEFAULT_TIER = "small"
DEFAULT_QUANTITY = 1
DEFAULT_CAPACITY_GB = 100
DEFAULT_OWNER = "dev-team"
DEFAULT_COST_CENTER = "engineering"

_RESOURCE_ORDER = ("postgres", "container", "object_storage")
_NAMES = {
    "postgres": "database",
    "container": "web",
    "object_storage": "bucket",
}
_SKU = {
    ("postgres", "small"): "db-small",
    ("postgres", "medium"): "db-medium",
    ("container", "small"): "container-small",
    ("container", "medium"): "container-medium",
}
_STORAGE_SKU = "storage-standard"
_QUANTITY_WORDS = {"one": 1, "two": 2, "three": 3}
_REGION_VALUES = {
    "azure east us": "eastus2",
    "us west": "us-west-2",
    "us east": "us-east-1",
}
_ENVIRONMENT_VALUES = {
    "production": "prod",
    "development": "dev",
    "prod": "prod",
    "test": "test",
    "dev": "dev",
}
_SCENARIO_ALIASES = {
    "malformed_json": "malformed_json",
    "malformed": "malformed_json",
    "missing_region": "missing_region",
    "missing_tags": "missing_tags",
    "unknown_resource_type": "unknown_resource_type",
    "unknown_type": "unknown_resource_type",
    "unsupported_sku": "unsupported_sku",
    "excessive_quantity": "excessive_quantity",
    "excessive_qty": "excessive_quantity",
    "public_object_storage": "public_object_storage",
    "public_storage": "public_object_storage",
}

_RESOURCE_RE = re.compile(
    r"\b(?:"
    r"object storage|web applications|web application|web containers|web container|"
    r"containers|container|postgresql|postgres|databases|database"
    r")\b",
    re.IGNORECASE,
)
_REGION_RE = re.compile(r"\b(?:azure east us|us west|us east)\b", re.IGNORECASE)
_ENVIRONMENT_RE = re.compile(
    r"\b(?:production|development|prod|test|dev)\b",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(r"\b(one|two|three|\d+)\b", re.IGNORECASE)
_TIER_RE = re.compile(r"\b(small|medium)\b", re.IGNORECASE)
_CAPACITY_RE = re.compile(r"\b(\d+)\s*(?:gb|gigabytes)\b", re.IGNORECASE)
_SCENARIO_RE = re.compile(r"SCENARIO:([A-Za-z0-9_]+)")

_MALFORMED_JSON = '{ "region": "us-east-1", "resources": [\n'


class UnknownScenarioError(ValueError):
    """The selected scenario name is not one of the fixed planner fixtures."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown planner scenario: {name}")


class Planner(Protocol):
    def generate(self, prompt: str) -> str:
        """Return one raw plan string for a prompt."""


class MockPlanner:
    """Deterministic stand-in for an untrusted proposal generator."""

    def generate(self, prompt: str, scenario: str | None = None) -> str:
        selected = scenario if scenario is not None else _scenario_name(prompt)
        if selected is not None:
            return _scenario_output(selected)
        return _dumps(_proposal(prompt))


def _scenario_name(prompt: str) -> str | None:
    match = _SCENARIO_RE.search(prompt)
    if match is None:
        return None
    return match.group(1)


def _scenario_output(name: str) -> str:
    canonical = _SCENARIO_ALIASES.get(name)
    if canonical is None:
        raise UnknownScenarioError(name)
    if canonical == "malformed_json":
        return _MALFORMED_JSON
    return _dumps(_SCENARIO_PLANS[canonical])


def _proposal(prompt: str) -> dict[str, object]:
    environment = _environment(prompt)
    return {
        "region": _region(prompt),
        "environment": environment,
        "tags": _tags(environment),
        "resources": _resources(prompt),
    }


def _region(prompt: str) -> str:
    match = _REGION_RE.search(prompt)
    if match is None:
        return DEFAULT_REGION
    return _REGION_VALUES[match.group(0).lower()]


def _environment(prompt: str) -> str:
    match = _ENVIRONMENT_RE.search(prompt)
    if match is None:
        return DEFAULT_ENVIRONMENT
    return _ENVIRONMENT_VALUES[match.group(0).lower()]


def _tags(environment: str) -> dict[str, str]:
    return {
        "environment": environment,
        "owner": DEFAULT_OWNER,
        "cost-center": DEFAULT_COST_CENTER,
    }


def _resources(prompt: str) -> list[dict[str, object]]:
    matches = list(_RESOURCE_RE.finditer(prompt))
    found: dict[str, dict[str, object]] = {}
    for index, match in enumerate(matches):
        resource_type = _resource_type(match.group(0))
        if resource_type in found:
            continue
        window_start = matches[index - 1].end() if index else 0
        window = prompt[window_start : match.start()]
        tier = _tier_in(window) or DEFAULT_TIER
        capacity = _capacity_in(window) if resource_type == "object_storage" else None
        found[resource_type] = _resource(
            resource_type,
            _quantity_in(window),
            tier,
            capacity,
        )
    if not found:
        found["container"] = _resource("container", DEFAULT_QUANTITY, DEFAULT_TIER, None)
    return [found[kind] for kind in _RESOURCE_ORDER if kind in found]


def _resource_type(phrase: str) -> str:
    normalized = phrase.lower()
    if normalized == "object storage":
        return "object_storage"
    if normalized in {
        "web application",
        "web applications",
        "web container",
        "web containers",
        "container",
        "containers",
    }:
        return "container"
    return "postgres"


def _quantity_in(window: str) -> int:
    stripped = _CAPACITY_RE.sub(" ", window)
    matches = list(_QUANTITY_RE.finditer(stripped))
    if not matches:
        return DEFAULT_QUANTITY
    token = matches[-1].group(1).lower()
    if token in _QUANTITY_WORDS:
        return _QUANTITY_WORDS[token]
    return int(token)


def _tier_in(window: str) -> str | None:
    matches = list(_TIER_RE.finditer(window))
    if not matches:
        return None
    return matches[-1].group(1).lower()


def _capacity_in(window: str) -> int | None:
    matches = list(_CAPACITY_RE.finditer(window))
    if not matches:
        return None
    return int(matches[-1].group(1))


def _resource(
    resource_type: str,
    quantity: int,
    tier: str,
    capacity_gb: int | None,
) -> dict[str, object]:
    if resource_type == "object_storage":
        sku = _STORAGE_SKU
    else:
        sku = _SKU[(resource_type, tier)]
    body: dict[str, object] = {
        "type": resource_type,
        "name": _NAMES[resource_type],
        "sku": sku,
        "quantity": quantity,
    }
    if resource_type == "object_storage":
        body["capacity_gb"] = DEFAULT_CAPACITY_GB if capacity_gb is None else capacity_gb
    body["public_access"] = False
    return body


def _dumps(payload: object) -> str:
    return json.dumps(payload, indent=2) + "\n"


def _valid_shell(
    resources: list[dict[str, object]],
    *,
    region: str = DEFAULT_REGION,
    environment: str = DEFAULT_ENVIRONMENT,
    tags: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "region": region,
        "environment": environment,
        "tags": tags if tags is not None else _tags(environment),
        "resources": resources,
    }


_SCENARIO_PLANS: dict[str, dict[str, object]] = {
    "missing_region": {
        "environment": DEFAULT_ENVIRONMENT,
        "tags": _tags(DEFAULT_ENVIRONMENT),
        "resources": [_resource("container", DEFAULT_QUANTITY, DEFAULT_TIER, None)],
    },
    "missing_tags": _valid_shell(
        [_resource("container", DEFAULT_QUANTITY, DEFAULT_TIER, None)],
        tags={"project": "demo"},
    ),
    "unknown_resource_type": _valid_shell(
        [
            {
                "type": "vm",
                "name": "web",
                "sku": "container-small",
                "quantity": DEFAULT_QUANTITY,
                "public_access": False,
            }
        ]
    ),
    "unsupported_sku": _valid_shell(
        [
            {
                "type": "container",
                "name": "web",
                "sku": "container-large",
                "quantity": DEFAULT_QUANTITY,
                "public_access": False,
            }
        ],
        environment="prod",
        tags=_tags("prod"),
    ),
    "excessive_quantity": _valid_shell(
        [_resource("container", 6, DEFAULT_TIER, None)]
    ),
    "public_object_storage": _valid_shell(
        [
            {
                "type": "object_storage",
                "name": "bucket",
                "sku": _STORAGE_SKU,
                "quantity": DEFAULT_QUANTITY,
                "capacity_gb": DEFAULT_CAPACITY_GB,
                "public_access": True,
            }
        ]
    ),
}
