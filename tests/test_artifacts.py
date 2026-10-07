import re

from app.artifacts import render_artifact
from app.models import Environment, ProposedPlan, Resource, ResourceType

_HEADER = (
    "# Prototype dry-run artifact.\n"
    "# Resources are fictional.\n"
    "# Nothing was deployed.\n"
)

_REPRESENTATIVE = """\
# Prototype dry-run artifact.
# Resources are fictional.
# Nothing was deployed.

locals {
  region = "eastus2"
  environment = "prod"
  tags = {
    "cost-center" = "finance"
    "environment" = "prod"
    "owner" = "platform"
  }
}

resource "demo_container" "container_0" {
  count = 2
  name = "web"
  sku = "container-small"
  public_access = false
}

resource "demo_object_storage" "object_storage_0" {
  count = 2
  name = "assets"
  sku = "storage-standard"
  public_access = true
  capacity_gb = 100
}

resource "demo_postgres" "postgres_0" {
  count = 1
  name = "database"
  sku = "db-small"
  public_access = false
}
"""

_BLOCK_RE = re.compile(
    r'resource "(?P<type>demo_[a-z_]+)" "(?P<label>[a-z_]+_[0-9]+)" \{\n'
    r"(?P<body>(?:  [^\n]+\n)*)\}"
)
_QUOTED_RE = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)


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
    public_access: bool = False,
) -> Resource:
    return Resource(
        type=ResourceType.POSTGRES,
        name=name,
        sku=sku,
        quantity=quantity,
        public_access=public_access,
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
    return ProposedPlan(
        region=region,
        environment=environment,
        tags={"owner": "dev-team", "environment": "dev"} if tags is None else tags,
        resources=[_container()] if resources is None else resources,
    )


def _representative_plan() -> ProposedPlan:
    return _plan(
        region="eastus2",
        environment=Environment.PROD,
        tags={
            "owner": "platform",
            "environment": "prod",
            "cost-center": "finance",
        },
        resources=[
            _postgres(),
            _container(quantity=2),
            _storage(quantity=2, capacity_gb=100, public_access=True),
        ],
    )


def _decode_hcl_string(literal: str) -> str:
    if len(literal) < 2 or literal[0] != '"' or literal[-1] != '"':
        raise AssertionError(literal)
    body = literal[1:-1]
    decoded: list[str] = []
    index = 0
    while index < len(body):
        if body.startswith("$${", index):
            decoded.append("${")
            index += 3
            continue
        if body.startswith("%%{", index):
            decoded.append("%{")
            index += 3
            continue
        if body[index] != "\\":
            decoded.append(body[index])
            index += 1
            continue
        selector = body[index + 1]
        if selector == "n":
            decoded.append("\n")
            index += 2
        elif selector == "r":
            decoded.append("\r")
            index += 2
        elif selector == "t":
            decoded.append("\t")
            index += 2
        elif selector == '"':
            decoded.append('"')
            index += 2
        elif selector == "\\":
            decoded.append("\\")
            index += 2
        elif selector == "u":
            decoded.append(chr(int(body[index + 2 : index + 6], 16)))
            index += 6
        elif selector == "U":
            decoded.append(chr(int(body[index + 2 : index + 10], 16)))
            index += 10
        else:
            raise AssertionError(body[index:])
    return "".join(decoded)


def _blocks(rendered: str) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for match in _BLOCK_RE.finditer(rendered):
        fields: dict[str, str] = {}
        for line in match.group("body").splitlines():
            key, value = line.strip().split(" = ", 1)
            fields[key] = value
        blocks.append(
            {
                "type": match.group("type"),
                "label": match.group("label"),
                "fields": fields,
            }
        )
    return blocks


def _outside_quoted_strings(rendered: str) -> str:
    return _QUOTED_RE.sub('""', rendered)


def test_renders_all_resource_types_and_fields() -> None:
    plan = _representative_plan()
    rendered = render_artifact(plan)

    assert rendered == _REPRESENTATIVE
    assert "\r" not in rendered
    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        rendered,
    )

    blocks = _blocks(rendered)
    assert [block["type"] for block in blocks] == [
        "demo_container",
        "demo_object_storage",
        "demo_postgres",
    ]
    container, storage, postgres = (block["fields"] for block in blocks)
    assert container == {
        "count": "2",
        "name": '"web"',
        "sku": '"container-small"',
        "public_access": "false",
    }
    assert storage == {
        "count": "2",
        "name": '"assets"',
        "sku": '"storage-standard"',
        "public_access": "true",
        "capacity_gb": "100",
    }
    assert postgres == {
        "count": "1",
        "name": '"database"',
        "sku": '"db-small"',
        "public_access": "false",
    }
    assert "capacity_gb" not in container
    assert "capacity_gb" not in postgres
    assert 'public_access = "true"' not in rendered
    assert 'public_access = "false"' not in rendered
    assert 'count = "' not in rendered
    assert 'capacity_gb = "' not in rendered


def test_identical_input_produces_identical_output() -> None:
    plan = _representative_plan()

    assert render_artifact(plan) == render_artifact(plan)
    assert render_artifact(_representative_plan()) == render_artifact(_representative_plan())


def test_resource_order_and_tag_insertion_order_do_not_change_output() -> None:
    resources = [
        _container(name="web", quantity=5),
        _container(name="web", quantity=1),
        _storage(name="assets", capacity_gb=50),
        _postgres(),
    ]
    forward_tags = {"owner": "team", "environment": "dev", "cost-center": "eng"}
    reverse_tags = {"cost-center": "eng", "owner": "team", "environment": "dev"}
    forward = _plan(tags=forward_tags, resources=resources)
    reverse = _plan(tags=reverse_tags, resources=list(reversed(resources)))

    assert list(forward.tags) != list(reverse.tags)
    assert [item.name for item in forward.resources] != [item.name for item in reverse.resources]

    rendered = render_artifact(forward)
    assert rendered == render_artifact(reverse)
    assert rendered.index('    "cost-center" = "eng"') < rendered.index('    "environment" = "dev"')
    assert rendered.index('    "environment" = "dev"') < rendered.index('    "owner" = "team"')
    assert rendered.index("count = 1\n  name = \"web\"") < rendered.index("count = 5\n  name = \"web\"")


def test_sort_uses_every_resource_field() -> None:
    cases = [
        (
            [_postgres(name="alpha"), _container(name="alpha")],
            'resource "demo_container"',
            'resource "demo_postgres"',
        ),
        (
            [_container(name="beta"), _container(name="alpha")],
            'name = "alpha"',
            'name = "beta"',
        ),
        (
            [
                _container(name="web", sku="container-small"),
                _container(name="web", sku="container-medium"),
            ],
            'sku = "container-medium"',
            'sku = "container-small"',
        ),
        (
            [
                _container(name="web", quantity=9),
                _container(name="web", quantity=2),
            ],
            "count = 2",
            "count = 9",
        ),
        (
            [
                _storage(name="bucket", capacity_gb=80),
                _storage(name="bucket", capacity_gb=15),
            ],
            "capacity_gb = 15",
            "capacity_gb = 80",
        ),
        (
            [
                _container(name="web", public_access=True),
                _container(name="web", public_access=False),
            ],
            "public_access = false",
            "public_access = true",
        ),
    ]

    for original, earlier, later in cases:
        swapped = list(reversed(original))
        rendered = render_artifact(_plan(resources=original))
        assert rendered == render_artifact(_plan(resources=swapped))
        assert rendered.index(earlier) < rendered.index(later)
        assert [resource.model_dump() for resource in original] != [
            resource.model_dump() for resource in swapped
        ]


def test_duplicate_and_similar_names_get_unique_labels() -> None:
    names = ["WEB_APP", "Web App", "web-app", "web_app", "web_app", "container_0"]
    plan = _plan(resources=[_container(name=name) for name in names])
    rendered = render_artifact(plan)
    blocks = _blocks(rendered)

    labels = [str(block["label"]) for block in blocks]
    decoded_names = [_decode_hcl_string(str(block["fields"]["name"])) for block in blocks]

    assert labels == [f"container_{index}" for index in range(len(names))]
    assert len(set(labels)) == len(names)
    assert decoded_names == ["WEB_APP", "Web App", "container_0", "web-app", "web_app", "web_app"]
    assert labels[decoded_names.index("container_0")] == "container_2"
    assert "web_app" not in labels
    assert "web-app" not in labels
    assert "WEB_APP" not in labels


def test_user_text_is_escaped_in_names_and_tags() -> None:
    name = 'a"b\\c\r\n\té${u}%{v}\x01'
    sku = 'sku"\\\t'
    region = 'eastus2\n${region}%{directive}東京'
    tag_key = 'own"er\\${key}%{\n'
    tag_value = 'val"ue\\${value}%{if}\r\n東京\x7f'
    assert len(name) <= 64
    assert len(sku) <= 64

    rendered = render_artifact(
        _plan(
            region=region,
            resources=[_container(name=name, sku=sku)],
            tags={tag_key: tag_value},
        )
    )

    assert rendered.startswith(_HEADER)
    assert "\\n" in rendered
    assert "\\r" in rendered
    assert "\\t" in rendered
    assert '\\"' in rendered
    assert "\\\\" in rendered
    assert "$${" in rendered
    assert "%%{" in rendered
    assert "\\u0001" in rendered
    assert "\\u007f" in rendered
    assert "東京" in rendered
    assert "é" in rendered
    assert "\r" not in rendered
    assert "\t" not in rendered
    assert "\x01" not in rendered
    assert "\x7f" not in rendered

    decoded = [_decode_hcl_string(literal) for literal in _QUOTED_RE.findall(rendered)]
    assert name in decoded
    assert sku in decoded
    assert region in decoded
    assert tag_key in decoded
    assert tag_value in decoded

    outside = _outside_quoted_strings(rendered)
    assert "${" not in outside
    assert "%{" not in outside
    assert "東京" not in outside
    assert "é" not in outside


def test_hcl_injection_stays_inside_quoted_strings() -> None:
    name = 'x"\n}\nresource "aws_instance" "bad" {\n'
    tag_key = 'k"\nresource "azurerm_resource_group" "g" {\n'
    tag_value = 'v ${path} %{ for x in xs ~}\nprovider "aws" {\n'
    assert len(name) <= 64

    rendered = render_artifact(
        _plan(
            resources=[_container(name=name, sku="container-small")],
            tags={tag_key: tag_value},
        )
    )
    decoded = [_decode_hcl_string(literal) for literal in _QUOTED_RE.findall(rendered)]
    outside = _outside_quoted_strings(rendered)

    assert name in decoded
    assert tag_key in decoded
    assert tag_value in decoded
    assert 'resource "aws_instance"' not in rendered
    assert 'resource "azurerm_resource_group"' not in rendered
    assert 'provider "aws"' not in rendered
    assert "aws_instance" not in outside
    assert "azurerm_" not in outside
    assert "provider" not in outside
    assert "${" not in outside
    assert "%{" not in outside
    assert rendered.startswith(_HEADER)


def test_render_leaves_proposal_unchanged() -> None:
    plan = _plan(
        tags={"b": "two", "a": "one"},
        resources=[_storage(name="later"), _container(name="first"), _postgres(name="middle")],
    )
    before = plan.model_dump(mode="json")
    names = [resource.name for resource in plan.resources]
    tag_keys = list(plan.tags)

    render_artifact(plan)

    assert plan.model_dump(mode="json") == before
    assert [resource.name for resource in plan.resources] == names
    assert names == ["later", "first", "middle"]
    assert list(plan.tags) == tag_keys
    assert tag_keys == ["b", "a"]


def test_header_is_present_and_no_provider_configuration() -> None:
    rendered = render_artifact(_representative_plan())
    outside = _outside_quoted_strings(rendered)

    assert rendered.startswith(_HEADER)
    assert "Nothing was deployed." in rendered
    assert "fictional" in rendered
    assert "dry-run artifact" in rendered
    assert "provider" not in outside
    assert "required_providers" not in rendered
    assert "aws_" not in rendered
    assert "azurerm_" not in rendered
    assert 'resource "demo_container"' in rendered
    assert 'resource "demo_postgres"' in rendered
    assert 'resource "demo_object_storage"' in rendered


def test_template_loads_from_the_module_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    rendered = render_artifact(_representative_plan())

    assert rendered == _REPRESENTATIVE
