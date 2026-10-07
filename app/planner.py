"""Deterministic mock planner.

``MockPlanner.generate`` returns one raw JSON string. It does not validate that
string, apply policy, price a catalog, hash a plan, read the store, approve a
plan, or render an artifact. It does not instantiate Pydantic models.

Normalization
-------------
Before parsing, ``app.lexicon.normalize`` rewrites the request using
``app/data/lexicon.json``: DevOps synonyms (``rds``, ``pods``, ``uat``, ``iad``),
quantity phrasing (``api x3``, ``3 replicas of the api``), and one-letter typos
of distinctive words (``dataabse``). It refuses unsupported resources and
regions (Redis, Frankfurt) and two-edit misspellings of resource nouns.
``MockPlanner.explain`` returns every rewrite so the reviewer sees it.

Vocabulary
----------
Matching is case-insensitive. After normalization, only the phrases below are
recognized. Other words are ignored. This is not a general language parser.

Resource phrases, longer phrases first:

- postgres: ``postgresql``, ``postgres``, ``databases``, ``database``, ``dbs``,
  ``db``
- mysql: ``mysql database``, ``mysql dbs``, ``mysql db``, ``mysql``
- container: ``web applications``, ``web application``, ``web containers``,
  ``web container``, ``websites``, ``website``, ``app servers``, ``app server``,
  ``containers``, ``container``
- object storage: ``object storage``, ``blob storage``, ``s3``

Quantity and size are taken from the text after the previous resource phrase
and before the current phrase. In that window the rightmost quantity wins, the
rightmost size wins, and the rightmost capacity wins. Quantities are ``one``,
``single``, ``two``, ``pair``, ``couple``, ``three`` through ``ten``,
``dozen``, and a plain integer. The integer is copied as written, including 0
and values above 100. Sizes are ``small``, ``medium``, ``big``, and ``large``.
``big`` and ``large`` both mean the large SKU. ``N gb`` or ``N gigabytes`` is
object-storage capacity, and ``N tb`` or ``N terabytes`` is ``N * 1000`` GB.
Neither is a resource count. Region phrases are blanked before windows are
read, so the digit in ``us-west-2`` or ``East US 2`` is not a quantity.

A window that ends in ``no``, ``not``, ``without``, ``don't need``, or
``do not need``, optionally followed by ``any``, ``a``, ``an``, ``extra``,
``additional``, or ``separate``, negates that resource. It is omitted.

Regions, leftmost phrase wins. A single space is required:

- ``US East``, ``East US``, ``us-east-1``, ``Northern Virginia`` -> ``us-east-1``
- ``US West``, ``us-west-2``, ``Oregon`` -> ``us-west-2``
- ``Azure East US``, ``eastus2``, ``East US 2`` -> ``eastus2``

A different single-space ``US <direction>`` phrase (north, south, central,
east, west, and words that start with them) or ``Azure ...`` phrase, such as
``US NORTH``, is not a plan. The planner returns an interpretation error
instead of ``us-east-1`` and instead of a made-up region id. ``us`` followed by
any other word, as in "give us two", is ordinary text.

Environments, leftmost word wins: ``development`` or ``dev``, ``test``,
``staging``, or ``qa`` (all ``test``), ``production`` or ``prod``.

Defaults
--------
- region ``us-east-1`` only when the prompt names no region phrase
- environment ``dev`` when no environment word is present
- tier ``small`` when a resource's window has no size word
- ``low cost`` asks for that same small tier and does not replace a ``small``,
  ``medium``, ``big``, or ``large`` written in a resource's own window
- quantity ``1`` when a resource's window has no quantity
- object-storage capacity ``100`` when no GB amount is present
- object-storage SKU ``storage-standard`` for small, medium, and large
- names ``database``, ``web``, and ``bucket``
- resource order: postgres, then mysql, then container, then object storage
- tags ``environment`` (the chosen environment), ``owner=dev-team``,
  ``cost-center=engineering``
- ``public_access`` false

Owner and cost-center are fixed synthetic defaults. They are not read from the
prompt, and emitting them is not a policy check.

When the prompt names no resource phrase, or negates every one it names, the
planner returns an interpretation error with ``interpretation_field``
``resources``. It does not guess a default resource.

Ambiguity
---------
- Two known region phrases: the leftmost phrase is used.
- An unknown region phrase earlier than a known one is an interpretation
  error. The known phrase does not replace it.
- Two environment words: the leftmost word is used.
- Two quantities or two sizes in one window: the rightmost value is used.
- The same resource type mentioned twice: the first non-negated mention
  supplies quantity, size, and capacity. Later mentions of that type are ignored.
- A size or quantity that appears only after its resource phrase is ignored.
- A GB amount on a database or container is not used as that resource's quantity.
- ``storage`` without the words ``object storage`` is not object storage.
- A one-letter typo of a distinctive word (``dataabse``, ``postgress``,
  ``contaner``, ``prodution``) is corrected and reported. A two-edit
  misspelling of a resource noun (``dataabes``) is an interpretation error that
  names the likely word, so a typo cannot silently drop a resource. English
  look-alikes (``staying``, ``containing``, ``dataset``) are left alone.
- An unrecognized prompt returns an interpretation error. It does not raise.

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
- ``unsupported_sku``: SKU ``container-xl``, which this catalog does not price
- ``excessive_quantity`` (prompt alias ``excessive_qty``): container quantity ``6``
- ``public_object_storage`` (prompt alias ``public_storage``): object storage
  with ``public_access`` true
- ``extra_fields``: a valid plan that also claims ``status`` ``approved`` and a
  zero ``cost``, as a generator trying to skip review would
- ``bad_region``: a valid plan in ``eu-west-1``, which no policy allows
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.lexicon import Normalized, load_lexicon, normalize

DEFAULT_REGION = "us-east-1"
DEFAULT_ENVIRONMENT = "dev"
DEFAULT_TIER = "small"
DEFAULT_QUANTITY = 1
DEFAULT_CAPACITY_GB = 100
DEFAULT_OWNER = "dev-team"
DEFAULT_COST_CENTER = "engineering"

_RESOURCE_ORDER = ("postgres", "mysql", "container", "object_storage")
_NAMES = {
    "postgres": "database",
    "mysql": "mysql",
    "container": "web",
    "object_storage": "bucket",
}
_SKU = {
    ("postgres", "small"): "db-small",
    ("postgres", "medium"): "db-medium",
    ("container", "small"): "container-small",
    ("container", "medium"): "container-medium",
    ("postgres", "large"): "db-large",
    ("container", "large"): "container-large",
    ("mysql", "small"): "mysql-small",
    ("mysql", "medium"): "mysql-medium",
    ("mysql", "large"): "mysql-large",
}
_TIER_VALUES = {
    "small": "small",
    "medium": "medium",
    "big": "large",
    "large": "large",
}
_STORAGE_SKU = "storage-standard"
_QUANTITY_WORDS = {
    "one": 1,
    "single": 1,
    "two": 2,
    "pair": 2,
    "couple": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "dozen": 12,
}
_GB_PER_UNIT = {"gb": 1, "gigabytes": 1, "tb": 1000, "terabytes": 1000}
_KNOWN_REGIONS = (
    ("northern virginia", "us-east-1"),
    ("azure east us", "eastus2"),
    ("east us 2", "eastus2"),
    ("us-east-1", "us-east-1"),
    ("us-west-2", "us-west-2"),
    ("eastus2", "eastus2"),
    ("us east", "us-east-1"),
    ("east us", "us-east-1"),
    ("us west", "us-west-2"),
    ("oregon", "us-west-2"),
)
_ENVIRONMENT_VALUES = {
    "production": "prod",
    "development": "dev",
    "staging": "test",
    "prod": "prod",
    "test": "test",
    "dev": "dev",
    "qa": "test",
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
    "extra_fields": "extra_fields",
    "bad_region": "bad_region",
}

_RESOURCE_RE = re.compile(
    r"\b(?:"
    r"blob storage|object storage|web applications|web application|web containers|"
    r"web container|websites|website|app servers|app server|containers|container|"
    r"mysql database|mysql dbs|mysql db|mysql|"
    r"postgresql|postgres|databases|database|dbs|db|s3"
    r")\b",
    re.IGNORECASE,
)
_KNOWN_REGION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(phrase) for phrase, _region in _KNOWN_REGIONS) + r")\b",
    re.IGNORECASE,
)
_UNKNOWN_REGION_RE = re.compile(
    r"\b(?:us (?:north|south|central|east|west)[a-z]*|azure [a-z]+(?: [a-z]+)?)\b",
    re.IGNORECASE,
)
_KNOWN_REGION_VALUES = {phrase: region for phrase, region in _KNOWN_REGIONS}
_ENVIRONMENT_RE = re.compile(
    r"\b(?:production|development|staging|prod|test|dev|qa)\b",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(
    r"\b(" + "|".join(_QUANTITY_WORDS) + r"|\d+)\b",
    re.IGNORECASE,
)
_TIER_RE = re.compile(r"\b(small|medium|big|large)\b", re.IGNORECASE)
_CAPACITY_RE = re.compile(r"\b(\d+)\s*(gb|gigabytes|tb|terabytes)\b", re.IGNORECASE)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|don't need|do not need)"
    r"(?:\s+(?:any|an?|extra|additional|separate))?\s*$",
    re.IGNORECASE,
)
_SCENARIO_RE = re.compile(r"SCENARIO:([A-Za-z0-9_]+)")
# "low cost" asks for the small tier, so it counts as stating a size.
_LOW_COST_RE = re.compile(r"\b(?:low[\s-]cost|cheap(?:est)?|budget)\b", re.IGNORECASE)
_OWNER_RE = re.compile(r"\b(?:owner|owned\s+by)\b", re.IGNORECASE)
_COST_CENTER_RE = re.compile(r"\bcost[\s-]?cent(?:er|re)\b", re.IGNORECASE)

_MALFORMED_JSON = '{ "region": "us-east-1", "resources": [\n'

_LEXICON = load_lexicon(Path(__file__).resolve().parent / "data" / "lexicon.json")
# Words a one-edit typo is corrected to, and the text it becomes. Each target
# was checked against a 25,000-word corpus: none has a common English word one
# edit away. Left out on purpose: "staging" (staying, stating), "application"
# (duplication), "backend" (backed), "bucket" (brackets), and quantity and size
# words ("there" is one swap from "three").
_TYPO_TARGETS = {
    "postgresql": "postgresql",
    "postgres": "postgres",
    "database": "database",
    "databases": "databases",
    "container": "container",
    "containers": "containers",
    "website": "website",
    "websites": "websites",
    "microservice": "container",
    "microservices": "container",
    "mariadb": "mysql",
    "production": "production",
    "development": "development",
    "oregon": "oregon",
    "virginia": "us-east-1",
}
# A two-edit misspelling of these is refused with a suggestion.
_SUGGEST_TARGETS = ("postgresql", "postgres", "database", "databases", "container", "containers")
_PLANNER_WORDS = frozenset(
    word
    for phrase in (
        "web object blob app server servers storage gigabytes terabytes azure northern",
        *_ENVIRONMENT_VALUES,
        *_TIER_VALUES,
        *_QUANTITY_WORDS,
        *(region for region, _value in _KNOWN_REGIONS),
    )
    for word in re.findall(r"[a-z]+", phrase)
)
_ANY_UNKNOWN_REGION_RE = re.compile(
    rf"(?:{_UNKNOWN_REGION_RE.pattern})|{_LEXICON.unsupported_region_pattern}",
    re.IGNORECASE,
)


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
        normalized = _normalize(prompt)
        text = normalized.text
        region, unrecognized = _read_region(text)
        if unrecognized is not None:
            return _dumps(
                {
                    "interpretation_error": (
                        f"Unrecognized region '{unrecognized}'. "
                        "Recognized regions are US East, US West, and Azure East US."
                    )
                }
            )
        if normalized.unsupported:
            return _dumps(
                {
                    "interpretation_error": _unsupported_message(normalized.unsupported),
                    "interpretation_field": "resources",
                }
            )
        if normalized.misspelled:
            return _dumps(
                {
                    "interpretation_error": _misspelling_message(list(normalized.misspelled)),
                    "interpretation_field": "resources",
                }
            )
        resources = _resources(text)
        if resources is None:
            return _dumps(
                {
                    "interpretation_error": (
                        "No recognized resource. Recognized resources are PostgreSQL, "
                        "MySQL, web containers, and object storage."
                    ),
                    "interpretation_field": "resources",
                }
            )
        environment = _environment(text)
        return _dumps(
            {
                "region": DEFAULT_REGION if region is None else region,
                "environment": environment,
                "tags": _tags(environment),
                "resources": resources,
            }
        )

    def explain(self, prompt: str) -> list[str]:
        """Return each synonym, typo, and quantity rewrite applied to ``prompt``.

        Deterministic and side-effect free. The service shows these to the
        reviewer next to the plan. Scenario prompts are never rewritten.
        """
        if _scenario_name(prompt) is not None:
            return []
        return list(_normalize(prompt).readings)


def _normalize(prompt: str) -> Normalized:
    return normalize(
        prompt,
        _LEXICON,
        typo_targets=_TYPO_TARGETS,
        suggest_targets=_SUGGEST_TARGETS,
        known=_PLANNER_WORDS,
        resource_pattern=_RESOURCE_RE.pattern,
        negation_re=_NEGATION_RE,
    )


def _unsupported_message(names: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{name}'" for name in names)
    noun = "resource" if len(names) == 1 else "resources"
    return (
        f"This prototype cannot provision the {noun} {quoted}. "
        "Supported resources are PostgreSQL, MySQL, containers, and object storage."
    )


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


def _misspelling_message(misspelled: list[tuple[str, str]]) -> str:
    parts = [f"'{word}' (did you mean '{suggestion}'?)" for word, suggestion in misspelled]
    if len(parts) == 1:
        named = f"Unrecognized word {parts[0]}"
    elif len(parts) == 2:
        named = f"Unrecognized words {parts[0]} and {parts[1]}"
    else:
        named = f"Unrecognized words {', '.join(parts[:-1])}, and {parts[-1]}"
    return (
        f"{named}. It is too far from a known word to correct safely. "
        "Fix the request and generate again."
    )


def _read_region(prompt: str) -> tuple[str | None, str | None]:
    """Return a canonical region, or the unrecognized phrase.

    ``(None, None)`` means the prompt named no region, so the caller uses the
    default. An unrecognized phrase is returned as written and is not mapped.
    """
    known = _KNOWN_REGION_RE.search(prompt)
    unknown = _ANY_UNKNOWN_REGION_RE.search(prompt)
    if unknown is not None and (known is None or unknown.start() < known.start()):
        return None, unknown.group(0)
    if known is None:
        return None, None
    return _KNOWN_REGION_VALUES[known.group(0).lower()], None


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


def _resources(prompt: str) -> list[dict[str, object]] | None:
    """Return resources in a fixed order, or ``None`` when none is recognized."""
    found: dict[str, dict[str, object]] = {}
    for resource_type, window in _mention_windows(prompt).items():
        tier = _tier_in(window) or DEFAULT_TIER
        capacity = _capacity_in(window) if resource_type == "object_storage" else None
        found[resource_type] = _resource(
            resource_type,
            _quantity_in(window),
            tier,
            capacity,
        )
    if not found:
        return None
    return [found[kind] for kind in _RESOURCE_ORDER if kind in found]


def _mention_windows(prompt: str) -> dict[str, str]:
    """Map each resource type to the text before its first non-negated mention.

    The window runs from the previous resource phrase to this one; size,
    quantity, and capacity are read from it.
    """
    text = _without_regions(prompt)
    matches = list(_RESOURCE_RE.finditer(text))
    windows: dict[str, str] = {}
    for index, match in enumerate(matches):
        resource_type = _resource_type(match.group(0))
        if resource_type in windows:
            continue
        window_start = matches[index - 1].end() if index else 0
        window = text[window_start : match.start()]
        if _NEGATION_RE.search(window):
            continue
        windows[resource_type] = window
    return windows


@dataclass(frozen=True)
class StatedDetails:
    """What a request spelled out, as read by the built-in planner's vocabulary.

    Anything not stated here was filled from a default. The service uses this to
    tell the reviewer which values in a plan are defaults.
    """

    region: bool
    environment: bool
    owner: bool
    cost_center: bool
    mentioned: frozenset[str]
    sized: frozenset[str]
    with_capacity: frozenset[str]


def stated_details(prompt: str) -> StatedDetails | None:
    """Report which settings ``prompt`` names. None for a ``SCENARIO:`` prompt."""
    if _scenario_name(prompt) is not None:
        return None
    text = _normalize(prompt).text
    windows = _mention_windows(text)
    low_cost = _LOW_COST_RE.search(text) is not None
    return StatedDetails(
        region=_KNOWN_REGION_RE.search(text) is not None,
        environment=_ENVIRONMENT_RE.search(text) is not None,
        owner=_OWNER_RE.search(text) is not None,
        cost_center=_COST_CENTER_RE.search(text) is not None,
        mentioned=frozenset(windows),
        sized=frozenset(
            kind for kind, window in windows.items() if low_cost or _tier_in(window) is not None
        ),
        with_capacity=frozenset(
            kind for kind, window in windows.items() if _capacity_in(window) is not None
        ),
    )


def _without_regions(prompt: str) -> str:
    """Blank region phrases so ``us-west-2`` or ``East US 2`` is not a count."""
    text = prompt
    for pattern in (_KNOWN_REGION_RE, _UNKNOWN_REGION_RE):
        text = pattern.sub(lambda match: " " * len(match.group(0)), text)
    return text


def _resource_type(phrase: str) -> str:
    normalized = phrase.lower()
    if normalized in {"mysql", "mysql database", "mysql db", "mysql dbs"}:
        return "mysql"
    if normalized in {"object storage", "blob storage", "s3"}:
        return "object_storage"
    if normalized in {
        "web application",
        "web applications",
        "web container",
        "web containers",
        "website",
        "websites",
        "app server",
        "app servers",
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
    return _TIER_VALUES[matches[-1].group(1).lower()]


def _capacity_in(window: str) -> int | None:
    matches = list(_CAPACITY_RE.finditer(window))
    if not matches:
        return None
    amount, unit = matches[-1].groups()
    return int(amount) * _GB_PER_UNIT[unit.lower()]


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
                "sku": "container-xl",
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
    "extra_fields": {
        **_valid_shell([_resource("postgres", DEFAULT_QUANTITY, DEFAULT_TIER, None)]),
        "status": "approved",
        "cost": {"monthly_total": "0.00"},
    },
    "bad_region": _valid_shell(
        [_resource("postgres", DEFAULT_QUANTITY, DEFAULT_TIER, None)],
        region="eu-west-1",
    ),
}
