"""UI behavior tests for the Streamlit HTTP client.

These tests mock HTTP and do not import the planner, schema, or plan service.
"""

import ast
import copy
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

from ui.app import (
    EXAMPLE_PROMPT,
    LIVE_PROMPT_SCRIPT,
    MAX_PROMPT_CHARS,
    OUTCOME_GROUPS,
    PLAN_RECORD_KEY,
    REQUEST_TIMEOUT,
    SCENARIO_TOKENS,
    SUGGESTED_PROMPTS,
    busy_overlay_html,
    defaults_html,
    format_usd,
    live_prompt_installer_html,
    formatted_json,
    parse_decimal_string,
    plan_verdict,
    prompt_length_html,
    prompt_ready,
    submitted_prompt,
    verdict_html,
)

APP_PATH = Path(__file__).resolve().parents[1] / "ui" / "app.py"
ASSIGNMENT_EXAMPLE = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)
API_ROOT = "http://planner.test:8000"
PLAN_ID = "11111111-1111-4111-8111-111111111111"
OTHER_PLAN_ID = "22222222-2222-4222-8222-222222222222"
PLAN_HASH = "Displayed-Hash-Value"
RAW_OUTPUT = '{\n  "region": "us-east-1"\n}\n'
OLD_ARTIFACT = "OLD_ARTIFACT_MARKER\n"
SAVED_ARTIFACT = (
    '# Prototype dry-run artifact.\n'
    "# Nothing was deployed.\n"
    'resource "demo_container" "container_0" {\n'
    "}\n"
)


def _policy(
    policy_id: str,
    status: str,
    message: str,
    *,
    resource_name: str | None = None,
    field_path: str | None = None,
) -> dict[str, str | None]:
    return {
        "policy_id": policy_id,
        "status": status,
        "message": message,
        "resource_name": resource_name,
        "field_path": field_path,
    }


def _cost(
    total: str = "71",
    *,
    succeeded: bool = True,
    errors: list[str] | None = None,
    items: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if items is None:
        items = [
            {
                "resource_name": "database",
                "sku": "db-small",
                "quantity": 1,
                "unit_price": "35",
                "amount": "35",
            },
            {
                "resource_name": "web",
                "sku": "container-small",
                "quantity": 2,
                "unit_price": "18",
                "amount": "36",
            },
        ]
    return {
        "currency": "USD",
        "monthly_total": total,
        "line_items": items,
        "pricing_errors": [] if errors is None else errors,
        "succeeded": succeeded,
    }


def _proposed() -> dict[str, object]:
    return {
        "region": "us-east-1",
        "environment": "dev",
        "tags": {
            "environment": "dev",
            "owner": "dev-team",
            "cost-center": "engineering",
        },
        "resources": [
            {
                "type": "postgres",
                "name": "database",
                "sku": "db-small",
                "quantity": 1,
                "capacity_gb": None,
                "public_access": False,
            },
            {
                "type": "container",
                "name": "web",
                "sku": "container-small",
                "quantity": 2,
                "capacity_gb": None,
                "public_access": False,
            },
        ],
    }


def _record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": PLAN_ID,
        "prompt": EXAMPLE_PROMPT,
        "raw_output": RAW_OUTPUT,
        "status": "evaluated",
        "proposed": _proposed(),
        "plan_hash": PLAN_HASH,
        "validation_errors": [],
        "policy_checks": [
            _policy("allowed_regions", "passed", "Region is allowed."),
            _policy("dev_medium_cost", "passed", "No medium SKU in dev."),
        ],
        "cost": _cost(),
        "artifact": None,
        "created_at": "2026-10-07T00:00:00Z",
        "updated_at": "2026-10-07T00:00:00Z",
    }
    record.update(overrides)
    return record


def _response(status: int, payload: object) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = f"{API_ROOT}/v1/plans"
    response.encoding = "utf-8"
    if isinstance(payload, bytes):
        response._content = payload
    else:
        response._content = json.dumps(payload).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


def _assert_ran(at: AppTest) -> None:
    problems = [item.value for item in at.exception]
    assert problems == [], problems


def _page_text(at: AppTest) -> str:
    chunks: list[str] = []
    for group in (
        at.title,
        at.header,
        at.subheader,
        at.markdown,
        at.caption,
        at.text,
        at.info,
        at.warning,
        at.error,
        at.success,
        at.code,
    ):
        chunks.extend(element.value for element in group)
    for table in at.table:
        chunks.append(table.value.to_csv(index=False))
    return "\n".join(chunks)


def _disabled(at: AppTest, key: str) -> bool:
    return bool(at.button(key=key).disabled)


@pytest.fixture
def planner(monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv("API_BASE_URL", API_ROOT)
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    _assert_ran(at)
    return at


def _show(at: AppTest, record: dict[str, object]) -> AppTest:
    at.session_state[PLAN_RECORD_KEY] = copy.deepcopy(record)
    at.run()
    _assert_ran(at)
    return at


def _click(at: AppTest, key: str, effect: object) -> object:
    from unittest.mock import patch

    options: dict[str, object]
    if isinstance(effect, BaseException):
        options = {"side_effect": effect}
    else:
        options = {"return_value": effect}
    with patch("requests.request", **options) as request:
        at.button(key=key).click().run()
    _assert_ran(at)
    return request


def test_ui_source_does_not_import_backend() -> None:
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert imported
    for name in imported:
        assert name != "app"
        assert not name.startswith("app.")


def test_money_uses_decimal_strings() -> None:
    assert format_usd("71") == "USD 71.00"
    assert format_usd("73.500") == "USD 73.50"
    assert format_usd("2.500") == "USD 2.50"
    assert format_usd("1.005") == "USD 1.01"
    assert parse_decimal_string("0.025") == Decimal("0.025")
    for value in (0, 0.0, True, None, Decimal("71")):
        with pytest.raises(InvalidOperation):
            parse_decimal_string(value)


@pytest.mark.parametrize("provider", ["OpenAI", "Anthropic"])
def test_recognized_words_show_only_for_the_built_in_planner(planner: AppTest, provider: str) -> None:
    assert "Recognized words:" in _page_text(planner)
    assert "Any wording." not in _page_text(planner)

    planner.selectbox(key="planner_provider").set_value(provider).run()
    _assert_ran(planner)
    text = _page_text(planner)
    assert "Recognized words:" not in text
    assert "Any wording." in text
    assert "same schema, policies, and synthetic prices" in text


def test_interpretation_notes_show_as_plain_text(planner: AppTest) -> None:
    note = "container: the model added 1 × [click](http://evil.example); the request may not ask for it."
    _show(planner, _record(generator="openai:gpt-5", interpretation_notes=[note]))
    assert "Does the plan match the request?" in _page_text(planner)
    assert f"• {note}" in [element.value for element in planner.text]
    assert not any("evil.example" in element.value for element in planner.markdown)
    assert _disabled(planner, "approve") is False


def test_no_interpretation_panel_without_notes(planner: AppTest) -> None:
    _show(planner, _record())
    assert "Does the plan match the request?" not in _page_text(planner)


@pytest.mark.parametrize(
    ("label", "prompt", "outcome", "look_for"),
    SUGGESTED_PROMPTS,
    ids=[item[0] for item in SUGGESTED_PROMPTS],
)
def test_each_example_does_what_its_caption_says(
    label: str, prompt: str, outcome: str, look_for: str
) -> None:
    import re

    from app.services import PlanService
    from app.store import InMemoryStore

    assert outcome in {key for key, _heading in OUTCOME_GROUPS}
    assert len(prompt) <= MAX_PROMPT_CHARS
    record = PlanService(InMemoryStore()).create_plan(prompt)
    statuses = {check.status.value for check in record.policy_checks}

    if outcome in {"draft", "model"}:
        assert record.status.value == "draft", label
        assert record.validation_errors
        return
    assert record.status.value == "evaluated", label
    assert record.cost is not None and record.cost.succeeded
    if outcome == "approvable":
        assert statuses == {"passed"}, label
    elif outcome == "warning":
        assert "warning" in statuses and "error" not in statuses, label
    else:
        assert "error" in statuses, label
    stated = re.search(r"USD (\d+\.\d{2})", look_for)
    if stated:
        assert Decimal(stated.group(1)) == record.cost.monthly_total.quantize(Decimal("0.01")), label


def test_every_outcome_group_has_an_example() -> None:
    used = {item[2] for item in SUGGESTED_PROMPTS}
    assert used == {key for key, _heading in OUTCOME_GROUPS}


def test_choosing_an_example_fills_the_request_and_clears_the_scenario(planner: AppTest) -> None:
    planner.selectbox(key="scenario").set_value("malformed").run()
    target = next(index for index, item in enumerate(SUGGESTED_PROMPTS) if item[2] == "policy_error")
    planner.button(key=f"suggest_{target}").click().run()
    _assert_ran(planner)
    assert planner.text_area(key="prompt_text").value == SUGGESTED_PROMPTS[target][1]
    assert planner.selectbox(key="scenario").value == "None"
    text = _page_text(planner)
    assert "Blocked by a policy" in text
    assert SUGGESTED_PROMPTS[target][3] in text


_DRAFT = {
    "status": "draft",
    "proposed": None,
    "plan_hash": None,
    "policy_checks": [],
    "cost": None,
    "validation_errors": [
        {"code": "unrecognized_input", "message": "No recognized resource.", "field_path": "resources"}
    ],
}


def test_verdict_for_a_draft_is_a_failure_listing_each_error() -> None:
    kind, title, detail, reasons = plan_verdict(_record(**_DRAFT))
    assert (kind, title) == ("fail", "Not a valid plan")
    assert "cannot be approved" in detail
    assert reasons == ["Validation error: unrecognized_input at resources: No recognized resource."]


def test_verdict_for_policy_and_pricing_failures_is_blocked() -> None:
    policy = _record(policy_checks=[_policy("storage_public", "error", "Bucket is public.")])
    kind, title, _detail, reasons = plan_verdict(policy)
    assert (kind, title) == ("fail", "Blocked: cannot be approved")
    assert reasons == ["storage_public: Bucket is public."]

    pricing = _record(
        cost={"succeeded": False, "monthly_total": "0", "line_items": [], "pricing_errors": ["unknown SKU"]}
    )
    assert plan_verdict(pricing)[3] == ["Pricing: unknown SKU"]


def test_verdict_for_warnings_and_clean_plans() -> None:
    warned = _record(policy_checks=[_policy("dev_medium_cost", "warning", "Medium SKU in dev.")])
    kind, title, detail, reasons = plan_verdict(warned)
    assert (kind, title) == ("warn", "Approvable with 1 warning")
    assert reasons == ["dev_medium_cost: Medium SKU in dev."]
    assert "USD 71.00" in detail

    assert plan_verdict(_record())[:2] == ("pass", "Ready for approval")
    assert plan_verdict(_record(status="approved"))[:2] == ("pass", "Approved")
    assert plan_verdict(_record(status="rejected"))[0] == "rejected"
    assert plan_verdict(None) is None


_DEFAULTS = [
    "Region: the request names none, so the plan uses us-east-1.",
    "Environment: the request names none, so the plan uses dev.",
]


def test_defaults_panel_lists_each_default_and_the_banner_counts_them(planner: AppTest) -> None:
    _show(planner, _record(defaults_applied=_DEFAULTS))
    markdown = "\n".join(element.value for element in planner.markdown)
    assert 'class="p2p-defaults"' in markdown
    assert "Defaults the plan used (2)" in markdown
    for note in _DEFAULTS:
        assert f"<li>{note}</li>" in markdown
    assert "Uses 2 defaults you did not specify; see the list below." in markdown


def test_no_defaults_panel_when_nothing_was_defaulted(planner: AppTest) -> None:
    _show(planner, _record())
    markdown = "\n".join(element.value for element in planner.markdown)
    assert 'class="p2p-defaults"' not in markdown
    assert "did not specify" not in markdown


def test_defaults_html_escapes_model_text() -> None:
    assert "&lt;b&gt;" in defaults_html(["Region: <b>x</b>"])
    assert "<b>" not in defaults_html(["Region: <b>x</b>"])


def test_verdict_html_escapes_model_text() -> None:
    html_text = verdict_html(("fail", "Blocked", "detail", ["sku: <img src=x onerror=alert(1)>"]))
    assert "<img" not in html_text
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_text


def test_draft_shows_a_red_banner_and_a_failed_review_step(planner: AppTest) -> None:
    _show(planner, _record(**_DRAFT))
    markdown = "\n".join(element.value for element in planner.markdown)
    assert 'class="p2p-verdict fail"' in markdown
    assert "Not a valid plan" in markdown
    assert "No recognized resource." in markdown
    assert 'class="p2p-step failed"' in markdown
    assert "Failed validation" in markdown
    assert 'class="p2p-pill fail">Status: draft' in markdown
    # A short copy sits under Generate plan, so the result is visible without scrolling.
    assert markdown.count('class="p2p-verdict fail"') == 2
    assert "1 finding. Details are in 2 Review below." in markdown


def test_clean_plan_shows_a_green_banner_and_no_failed_step(planner: AppTest) -> None:
    _show(planner, _record())
    markdown = "\n".join(element.value for element in planner.markdown)
    assert 'class="p2p-verdict pass"' in markdown
    assert "Ready for approval" in markdown
    assert "p2p-step failed" not in markdown


COMPACT_MODEL_OUTPUT = (
    '{"region":"us-east-1","environment":"dev","tags":{"owner":"dev-team"},'
    '"resources":[{"type":"postgres","name":"db","sku":"db-small","quantity":1,"public_access":false}]}'
)


def test_formatted_json_indents_compact_output_without_changing_values() -> None:
    formatted = formatted_json(COMPACT_MODEL_OUTPUT)
    assert formatted is not None
    assert formatted.splitlines()[:3] == ["{", '  "region": "us-east-1",', '  "environment": "dev",']
    assert json.loads(formatted) == json.loads(COMPACT_MODEL_OUTPUT)
    assert list(json.loads(formatted)) == ["region", "environment", "tags", "resources"]
    assert formatted_json('{"note":"café ✓"}') == '{\n  "note": "café ✓"\n}'


@pytest.mark.parametrize("raw", ["{ truncated", "NEW_RAW_OUTPUT", "", '{"a": 1} trailing'])
def test_formatted_json_is_none_for_text_that_is_not_json(raw: str) -> None:
    assert formatted_json(raw) is None


def test_model_output_shows_formatted_and_exact_tabs(planner: AppTest) -> None:
    _show(planner, _record(raw_output=COMPACT_MODEL_OUTPUT, generator="openai:gpt-5"))
    code_values = [block.value for block in planner.code]
    assert formatted_json(COMPACT_MODEL_OUTPUT) in code_values
    assert COMPACT_MODEL_OUTPUT in code_values
    assert [tab.label for tab in planner.tabs] == ["Formatted", "Exact text as stored"]
    assert "the stored text is unchanged" in _page_text(planner)


def test_invalid_output_is_shown_exactly_without_tabs(planner: AppTest) -> None:
    _show(planner, _record(**{**_DRAFT, "raw_output": "{ truncated"}))
    assert "{ truncated" in [block.value for block in planner.code]
    assert list(planner.tabs) == []
    assert "Not valid JSON" in _page_text(planner)


@pytest.mark.parametrize(
    ("text", "planner_choice", "scenario", "ready"),
    [
        ("x" * 30, "Built-in", "None", True),
        ("  " + "x" * 29 + "  ", "Built-in", "None", False),
        ("", "Built-in", "malformed", True),
        ("", "OpenAI", "malformed", False),
        ("x" * 30, "Anthropic", "None", True),
        (None, "Built-in", "None", False),
    ],
)
def test_prompt_ready(text: object, planner_choice: str, scenario: str, ready: bool) -> None:
    assert prompt_ready(text, planner=planner_choice, scenario=scenario) is ready


def test_prompt_length_message_names_the_minimum_and_the_gap() -> None:
    assert "✓ 30 characters (minimum 30)" in prompt_length_html("x" * 30)
    short = prompt_length_html("two containers")
    assert "✗ 14 of 30 characters. Add 16 more" in short
    assert "Ctrl+Enter" in short


def test_short_request_shows_the_count_and_a_click_is_refused(planner: AppTest) -> None:
    from unittest.mock import patch

    assert planner.button(key="generate").proto.type == "primary"
    planner.text_area(key="prompt_text").set_value("two containers").run()
    _assert_ran(planner)
    markdown = "\n".join(element.value for element in planner.markdown)
    assert 'data-p2p-mode="length">✗ 14 of 30 characters' in markdown

    # The in-page script disables the button while typing; the server is the backstop.
    with patch("requests.request") as request:
        planner.button(key="generate").click().run()
    assert request.call_count == 0
    assert any("at least 30 characters" in element.value for element in planner.error)

    planner.text_area(key="prompt_text").set_value("two web containers in US East, prod").run()
    assert "✓ 35 characters" in "\n".join(element.value for element in planner.markdown)


def test_scenario_marks_the_count_line_so_the_script_enables_generate(planner: AppTest) -> None:
    planner.text_area(key="prompt_text").set_value("").run()
    planner.selectbox(key="scenario").set_value("malformed").run()
    markdown = "\n".join(element.value for element in planner.markdown)
    assert 'data-p2p-mode="scenario">Scenario selected: the request text is not sent.' in markdown
    request = _click(planner, "generate", _response(201, _record()))
    assert request.call_args.kwargs["json"]["prompt"] == "SCENARIO:malformed"


def test_live_prompt_script_targets_the_box_and_button_and_uses_the_minimum() -> None:
    assert ".st-key-prompt_text textarea" in LIVE_PROMPT_SCRIPT
    assert ".st-key-generate button" in LIVE_PROMPT_SCRIPT
    assert "const MIN = 30;" in LIVE_PROMPT_SCRIPT
    assert "MutationObserver" in LIVE_PROMPT_SCRIPT
    installer = live_prompt_installer_html()
    assert "window.parent.document" in installer
    assert "p2p-live-prompt" in installer
    # The script is embedded as a JSON string, so it cannot close the <script> tag early.
    assert "</script>" not in installer[: installer.rindex("</script>")]


def test_busy_overlay_is_centred_full_screen_and_escaped() -> None:
    overlay = busy_overlay_html("Waiting for <OpenAI>…", "Up to a minute.")
    assert "position:fixed;inset:0" in overlay
    assert "justify-content:center" in overlay
    assert "animation:p2p-spin" in overlay
    assert "&lt;OpenAI&gt;" in overlay and "<OpenAI>" not in overlay


def test_overlay_is_removed_after_the_plan_arrives(planner: AppTest) -> None:
    request = _click(planner, "generate", _response(201, _record()))
    assert request.call_count == 1
    assert not any("p2p-busy" in element.value for element in planner.markdown)


def test_submitted_prompt_sends_only_the_selected_scenario_token() -> None:
    assert submitted_prompt(ASSIGNMENT_EXAMPLE, "None") == ASSIGNMENT_EXAMPLE
    assert submitted_prompt(ASSIGNMENT_EXAMPLE, "malformed") == "SCENARIO:malformed"
    assert submitted_prompt("  ", "public_storage") == "SCENARIO:public_storage"
    assert submitted_prompt("x" * 5000, "extra_fields") == "SCENARIO:extra_fields"
    assert submitted_prompt("SCENARIO:missing_tags\nalready", "bad_region") == "SCENARIO:bad_region"


def test_scenario_selector_shows_only_for_the_built_in_planner(planner: AppTest) -> None:
    assert "scenario" in [box.key for box in planner.selectbox]
    assert set(SCENARIO_TOKENS) >= {"extra_fields", "bad_region"}
    for provider in ("OpenAI", "Anthropic"):
        planner.selectbox(key="planner_provider").set_value(provider).run()
        _assert_ran(planner)
        assert "scenario" not in [box.key for box in planner.selectbox]
        assert "Scenarios apply to the built-in planner" not in _page_text(planner)
    planner.selectbox(key="planner_provider").set_value("Built-in").run()
    _assert_ran(planner)
    assert "scenario" in [box.key for box in planner.selectbox]


def test_model_planner_ignores_a_previously_chosen_scenario(planner: AppTest) -> None:
    from unittest.mock import patch

    planner.selectbox(key="scenario").set_value("malformed").run()
    planner.selectbox(key="planner_provider").set_value("OpenAI").run()
    planner.text_input(key="openai_api_key").set_value("sk-test").run()
    with patch("requests.request", return_value=_response(201, _record())) as request:
        planner.button(key="generate").click().run()
    _assert_ran(planner)
    assert request.call_args.kwargs["json"]["prompt"] == ASSIGNMENT_EXAMPLE


def test_initial_page_shows_the_prototype_notice_and_example(planner: AppTest) -> None:
    text = _page_text(planner)
    assert planner.title[0].value == "Prompt-to-Provisioning Planner"
    assert "prototype" in text.lower()
    assert "synthetic" in text.lower()
    assert "Nothing was deployed." in text
    assert "not saved on the plan" in text
    assert "The same person writes the sentence and decides." in text
    assert planner.selectbox(key="planner_provider").value == "Built-in"
    assert planner.text_area[0].value == ASSIGNMENT_EXAMPLE
    assert EXAMPLE_PROMPT == ASSIGNMENT_EXAMPLE
    assert planner.selectbox(key="scenario").options == ["None", *SCENARIO_TOKENS]
    assert _disabled(planner, "generate") is False
    assert _disabled(planner, "approve") is True
    assert _disabled(planner, "reject") is True
    assert _disabled(planner, "artifact") is True
    assert _disabled(planner, "refresh") is True
    assert planner.download_button.len == 0


def test_valid_creation_renders_results(planner: AppTest) -> None:
    created = _record()
    request = _click(planner, "generate", _response(201, created))

    assert request.call_count == 1
    method, url = request.call_args.args
    assert method == "POST"
    assert url == f"{API_ROOT}/v1/plans"
    assert request.call_args.kwargs["json"] == {"prompt": ASSIGNMENT_EXAMPLE}
    assert request.call_args.kwargs["timeout"] == REQUEST_TIMEOUT == (5, 30)

    stored = planner.session_state[PLAN_RECORD_KEY]
    assert stored["id"] == PLAN_ID
    assert stored["status"] == "evaluated"
    assert stored["plan_hash"] == PLAN_HASH
    assert stored["artifact"] is None
    text = _page_text(planner)
    assert f"Status: {stored['status']}" in text
    assert f"Plan ID: {stored['id']}" in text
    assert f"Plan hash: {stored['plan_hash']}" in text
    assert RAW_OUTPUT in [block.value for block in planner.code]
    assert "us-east-1" in text
    assert "db-small" in text
    assert "container-small" in text
    assert "allowed_regions" in text
    assert "passed" in text
    assert "No validation errors." in text
    assert "Monthly total: USD 71.00" in text
    assert "Exact monthly total: 71" in text
    assert "Cost unavailable" not in text
    assert _disabled(planner, "approve") is False
    assert _disabled(planner, "reject") is False
    assert _disabled(planner, "artifact") is True
    assert planner.download_button.len == 0


def test_fractional_cost_keeps_exact_decimals_and_rounds_the_total(planner: AppTest) -> None:
    created = _record(
        cost=_cost(
            "73.500",
            items=[
                {
                    "resource_name": "assets",
                    "sku": "storage-standard",
                    "quantity": 1,
                    "unit_price": "0.025",
                    "amount": "2.500",
                }
            ],
        )
    )
    _click(planner, "generate", _response(201, created))
    text = _page_text(planner)
    assert "Monthly total: USD 73.50" in text
    assert "Exact monthly total: 73.500" in text
    assert "0.025" in text
    assert "2.500" in text
    assert "USD 2.50" in text
    assert "USD 0.03" not in text
    assert _disabled(planner, "approve") is False


def test_warning_enables_approval(planner: AppTest) -> None:
    created = _record(
        policy_checks=[
            _policy("dev_medium_cost", "warning", "Medium SKU in dev."),
            _policy("allowed_regions", "passed", "Region is allowed."),
        ]
    )
    _click(planner, "generate", _response(201, created))
    text = _page_text(planner)
    assert "dev_medium_cost" in text
    assert "warning" in text
    assert _disabled(planner, "approve") is False
    assert _disabled(planner, "reject") is False


def test_validation_errors_disable_approval(planner: AppTest) -> None:
    created = _record(
        status="draft",
        proposed=None,
        plan_hash=None,
        raw_output="{ truncated",
        validation_errors=[
            {
                "code": "json_invalid",
                "message": "Expecting value at line 2 column 1",
                "field_path": None,
            }
        ],
        policy_checks=[],
        cost=None,
    )
    _click(planner, "generate", _response(201, created))
    text = _page_text(planner)
    assert "Status: draft" in text
    assert "{ truncated" in [block.value for block in planner.code]
    assert "Validation error: json_invalid: Expecting value at line 2 column 1" in text
    assert "Validated proposal: none" in text
    assert "Cost unavailable" in text
    assert "Pricing did not run." in text
    assert "USD 0.00" not in text
    assert "Monthly total:" not in text
    assert _disabled(planner, "approve") is True
    assert _disabled(planner, "reject") is False
    assert _disabled(planner, "artifact") is True


def test_policy_error_disables_approval(planner: AppTest) -> None:
    created = _record(
        policy_checks=[
            _policy(
                "storage_public",
                "error",
                "Object storage is public.",
                resource_name="bucket",
            )
        ]
    )
    _click(planner, "generate", _response(201, created))
    text = _page_text(planner)
    assert "storage_public" in text
    assert "error" in text
    assert "Object storage is public." in text
    assert _disabled(planner, "approve") is True
    assert _disabled(planner, "reject") is False


def test_pricing_failure_disables_approval_and_hides_zero_total(planner: AppTest) -> None:
    created = _record(
        cost=_cost(
            "0",
            succeeded=False,
            errors=["unknown SKU 'container-large' on resource 'web'."],
            items=[],
        )
    )
    _click(planner, "generate", _response(201, created))
    text = _page_text(planner)
    assert "Cost unavailable" in text
    assert "Pricing error: unknown SKU 'container-large' on resource 'web'." in text
    assert "Monthly total:" not in text
    assert "USD 0.00" not in text
    assert _disabled(planner, "approve") is True
    assert _disabled(planner, "reject") is False


def test_approve_posts_the_displayed_hash_once(planner: AppTest) -> None:
    _click(planner, "generate", _response(201, _record()))
    displayed_hash = planner.session_state[PLAN_RECORD_KEY]["plan_hash"]
    displayed_id = planner.session_state[PLAN_RECORD_KEY]["id"]
    assert displayed_hash == PLAN_HASH
    assert f"Plan hash: {displayed_hash}" in _page_text(planner)

    approved = _record(status="approved")
    request = _click(planner, "approve", _response(200, approved))

    assert request.call_count == 1
    method, url = request.call_args.args
    assert method == "POST"
    assert url == f"{API_ROOT}/v1/plans/{displayed_id}/approve"
    assert request.call_args.kwargs["json"] == {"plan_hash": displayed_hash}
    assert request.call_args.kwargs["timeout"] == (5, 30)
    assert planner.session_state[PLAN_RECORD_KEY]["status"] == "approved"
    assert planner.session_state[PLAN_RECORD_KEY]["plan_hash"] == displayed_hash
    assert _disabled(planner, "approve") is True
    assert _disabled(planner, "reject") is True
    assert _disabled(planner, "artifact") is False


def test_reject_posts_the_plan_id_without_a_body(planner: AppTest) -> None:
    draft = _record(
        status="draft",
        proposed=None,
        plan_hash=None,
        validation_errors=[
            {"code": "missing", "message": "Field required", "field_path": "region"}
        ],
        policy_checks=[],
        cost=None,
    )
    _click(planner, "generate", _response(201, draft))
    rejected = _record(**{**draft, "status": "rejected"})
    request = _click(planner, "reject", _response(200, rejected))

    assert request.call_count == 1
    method, url = request.call_args.args
    assert method == "POST"
    assert url == f"{API_ROOT}/v1/plans/{PLAN_ID}/reject"
    assert request.call_args.kwargs["json"] is None
    assert planner.session_state[PLAN_RECORD_KEY]["status"] == "rejected"
    assert planner.session_state[PLAN_RECORD_KEY]["validation_errors"] == draft["validation_errors"]
    assert _disabled(planner, "approve") is True
    assert _disabled(planner, "reject") is True
    assert _disabled(planner, "artifact") is True


def test_artifact_generation_uses_the_plan_id_and_shows_the_download(planner: AppTest) -> None:
    _click(planner, "generate", _response(201, _record()))
    _click(planner, "approve", _response(200, _record(status="approved")))
    generated = _record(status="artifact_generated", artifact=SAVED_ARTIFACT)
    from streamlit.runtime.media_file_manager import MediaFileManager

    downloaded: dict[str, object] = {}
    original_add = MediaFileManager.add

    def _capture_download(self, path_or_data, mimetype, coordinates, **kwargs):
        if kwargs.get("is_for_static_download"):
            downloaded["data"] = path_or_data
            downloaded["file_name"] = kwargs.get("file_name")
            downloaded["mimetype"] = mimetype
        return original_add(self, path_or_data, mimetype, coordinates, **kwargs)

    with pytest.MonkeyPatch.context() as ui_patch:
        ui_patch.setattr(MediaFileManager, "add", _capture_download)
        request = _click(planner, "artifact", _response(200, generated))

    assert request.call_count == 1
    method, url = request.call_args.args
    assert method == "POST"
    assert url == f"{API_ROOT}/v1/plans/{PLAN_ID}/artifact"
    assert request.call_args.kwargs["json"] is None
    stored = planner.session_state[PLAN_RECORD_KEY]
    assert stored["status"] == "artifact_generated"
    assert stored["artifact"] == SAVED_ARTIFACT
    assert SAVED_ARTIFACT in [block.value for block in planner.code]
    assert _disabled(planner, "artifact") is True

    download = planner.download_button(key="download_main_tf")
    assert download.label == "Download main.tf"
    assert isinstance(download.url, str) and download.url
    assert downloaded["file_name"] == "main.tf"
    assert downloaded["data"] == SAVED_ARTIFACT.encode("utf-8")


def test_new_creation_replaces_the_previous_artifact(planner: AppTest) -> None:
    previous = _record(status="artifact_generated", artifact=OLD_ARTIFACT)
    _show(planner, previous)
    assert OLD_ARTIFACT in [block.value for block in planner.code]
    assert planner.download_button.len == 1

    replacement = _record(
        id=OTHER_PLAN_ID,
        raw_output="NEW_RAW_OUTPUT",
        artifact=None,
        plan_hash="replacement-hash",
    )
    request = _click(planner, "generate", _response(201, replacement))

    assert request.call_count == 1
    stored = planner.session_state[PLAN_RECORD_KEY]
    assert stored["id"] == OTHER_PLAN_ID
    assert stored["artifact"] is None
    assert stored["status"] == "evaluated"
    code_values = [block.value for block in planner.code]
    assert "NEW_RAW_OUTPUT" in code_values
    assert OLD_ARTIFACT not in code_values
    assert "OLD_ARTIFACT_MARKER" not in _page_text(planner)
    assert planner.download_button.len == 0


@pytest.mark.parametrize(
    ("status", "button_key", "effect", "snippet"),
    [
        ("artifact_generated", "generate", requests.Timeout("slow"), "timed out"),
        (
            "artifact_generated",
            "generate",
            requests.ConnectionError("refused"),
            "unavailable",
        ),
        (
            "artifact_generated",
            "generate",
            _response(
                422,
                {
                    "code": "unknown_scenario",
                    "message": "unknown_scenario: nope is not a scenario",
                },
            ),
            "unknown_scenario",
        ),
        (
            "artifact_generated",
            "generate",
            _response(
                422,
                {
                    "detail": [
                        {
                            "loc": ["body", "prompt"],
                            "msg": "prompt must be nonblank",
                            "type": "value_error",
                        }
                    ]
                },
            ),
            "prompt must be nonblank",
        ),
        ("artifact_generated", "generate", _response(201, b"not-json"), "invalid JSON"),
        (
            "artifact_generated",
            "generate",
            _response(201, {"status": "approved"}),
            "missing plan fields",
        ),
        (
            "artifact_generated",
            "refresh",
            _response(404, {"code": "not_found", "message": "not_found: missing plan"}),
            "not_found",
        ),
        ("evaluated", "approve", requests.Timeout("slow"), "timed out"),
        ("evaluated", "reject", requests.ConnectionError("refused"), "unavailable"),
        (
            "approved",
            "artifact",
            _response(500, {"error": "boom"}),
            "unexpected HTTP 500",
        ),
    ],
)
def test_api_failures_preserve_the_current_record(
    planner: AppTest,
    status: str,
    button_key: str,
    effect: object,
    snippet: str,
) -> None:
    current = _record(status=status, artifact=OLD_ARTIFACT)
    if status == "approved":
        current["artifact"] = None
    _show(planner, current)
    before = copy.deepcopy(planner.session_state[PLAN_RECORD_KEY])

    request = _click(planner, button_key, effect)

    assert request.call_count == 1
    assert request.call_args.kwargs["timeout"] == (5, 30)
    assert planner.session_state[PLAN_RECORD_KEY] == before
    text = _page_text(planner)
    assert snippet in text
    assert "not retried" in text
    assert "Traceback" not in text
    if current["artifact"] == OLD_ARTIFACT:
        assert OLD_ARTIFACT in [block.value for block in planner.code]


def test_float_money_is_rejected_and_the_record_is_preserved(planner: AppTest) -> None:
    current = _record(
        status="artifact_generated",
        artifact=OLD_ARTIFACT,
        cost=_cost("35"),
    )
    _show(planner, current)
    before = copy.deepcopy(planner.session_state[PLAN_RECORD_KEY])
    payload = _record()
    assert isinstance(payload["cost"], dict)
    payload["cost"]["monthly_total"] = 71.0

    request = _click(planner, "generate", _response(201, payload))

    assert request.call_count == 1
    assert planner.session_state[PLAN_RECORD_KEY] == before
    text = _page_text(planner)
    assert "decimal string" in text
    assert "not retried" in text
    assert "Monthly total: USD 35.00" in text
    # The example captions mention USD 71.00; the rejected total must not be shown as the plan's.
    assert "Monthly total: USD 71.00" not in text
    assert OLD_ARTIFACT in [block.value for block in planner.code]


def test_conflict_shows_the_error_and_refresh_does_not_resubmit_approval(
    planner: AppTest,
) -> None:
    current = _record()
    _show(planner, current)
    before = copy.deepcopy(planner.session_state[PLAN_RECORD_KEY])
    conflict = _click(
        planner,
        "approve",
        _response(
            409,
            {
                "code": "hash_mismatch",
                "message": "hash_mismatch: stored plan changed",
            },
        ),
    )

    assert conflict.call_count == 1
    assert conflict.call_args.kwargs["json"] == {"plan_hash": PLAN_HASH}
    assert planner.session_state[PLAN_RECORD_KEY] == before
    assert planner.session_state[PLAN_RECORD_KEY]["status"] == "evaluated"
    text = _page_text(planner)
    assert "hash_mismatch" in text
    assert "stored plan changed" in text
    assert "Approval is not submitted again." in text
    assert _disabled(planner, "refresh") is False

    refreshed = _record(updated_at="2026-10-07T00:05:00Z")
    request = _click(planner, "refresh", _response(200, refreshed))

    assert request.call_count == 1
    method, url = request.call_args.args
    assert method == "GET"
    assert url == f"{API_ROOT}/v1/plans/{PLAN_ID}"
    assert request.call_args.kwargs["json"] is None
    assert "/approve" not in url
    assert planner.session_state[PLAN_RECORD_KEY]["updated_at"] == "2026-10-07T00:05:00Z"
    assert "hash_mismatch" not in _page_text(planner)


def test_scenario_token_is_sent_without_the_prompt_text(planner: AppTest) -> None:
    planner.selectbox(key="scenario").set_value("public_storage")
    request = _click(planner, "generate", _response(201, _record()))

    posted = request.call_args.kwargs["json"]["prompt"]
    assert posted == "SCENARIO:public_storage"


def test_api_base_url_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    monkeypatch.delenv("API_BASE_URL", raising=False)
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    _assert_ran(at)
    assert "API base URL: http://localhost:8000" in _page_text(at)
    with patch("requests.request", return_value=_response(201, _record())) as request:
        at.button(key="generate").click().run()
    _assert_ran(at)
    assert request.call_args.args[1] == "http://localhost:8000/v1/plans"
