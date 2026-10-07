"""Streamlit HTTP client for the prompt-to-provisioning planner.

The UI sends prompts and review actions to the API and renders the returned
plan record. It does not validate plans, apply policy, price resources, hash
proposals, or render Terraform itself.
"""

from __future__ import annotations

import copy
import html
import os
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests
import streamlit as st
import streamlit.components.v1 as components

DEFAULT_API_BASE_URL = "http://localhost:8000"
REQUEST_TIMEOUT = (5, 30)
EXAMPLE_PROMPT = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)
SUGGESTED_PROMPTS = (
    ("Small dev database and two web containers", EXAMPLE_PROMPT),
    (
        "Medium database in development",
        "A medium PostgreSQL database for a development team in US East.",
    ),
    (
        "Three production web containers in US West",
        "Three small web containers for production in US West.",
    ),
    (
        "Object storage in Azure East US",
        "100 GB object storage for a test team in Azure East US.",
    ),
    (
        "Unrecognized wording",
        "Build a rocket for the launch next week.",
    ),
)
_WORKFLOW_HTML = Path(__file__).resolve().parents[1] / "docs" / "button-flow.html"
SCENARIO_NONE = "None"
SCENARIO_TOKENS = (
    "malformed",
    "missing_region",
    "missing_tags",
    "unknown_type",
    "unsupported_sku",
    "excessive_qty",
    "public_storage",
)
PLAN_RECORD_KEY = "plan_record"
API_ERROR_KEY = "api_error"
DECISION_LOG_KEY = "decision_log"
PLANNER_BUILTIN = "Built-in"
OPENAI_MODEL = "gpt-5"
ANTHROPIC_MODEL = "claude-sonnet-5-5"
_DECISION_STATUSES = frozenset({"approved", "rejected", "artifact_generated"})

_RECORD_KEYS = (
    "id",
    "prompt",
    "raw_output",
    "status",
    "proposed",
    "plan_hash",
    "validation_errors",
    "policy_checks",
    "cost",
    "artifact",
    "created_at",
    "updated_at",
)
_STATUSES = frozenset(
    {
        "draft",
        "evaluated",
        "approved",
        "rejected",
        "artifact_generated",
    }
)
_CHECK_STATUSES = frozenset({"passed", "warning", "error"})
_CENTS = Decimal("0.01")
_CONFLICT_HINT = (
    "Refresh the plan to load the current record. Approval is not submitted again."
)


class ApiClientError(Exception):
    """A failed API call that should be shown and not retried."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        text = message.rstrip()
        if "not retried" not in text:
            text = f"{text} The request was not retried."
        super().__init__(text)
        self.status_code = status_code


def api_base_url() -> str:
    configured = os.environ.get("API_BASE_URL", "").strip()
    if not configured:
        return DEFAULT_API_BASE_URL
    return configured.rstrip("/")


def submitted_prompt(text: str, scenario: str) -> str:
    """Return the prompt to post, including a selected demo scenario token."""
    prompt = text if isinstance(text, str) else ""
    if scenario not in SCENARIO_TOKENS:
        return prompt
    token = f"SCENARIO:{scenario}"
    if prompt.lstrip().startswith(token):
        return prompt
    body = prompt.strip()
    if not body:
        return token
    return f"{token}\n{body}"


def parse_decimal_string(value: object) -> Decimal:
    """Parse a monetary JSON string. Floats and other non-strings are rejected."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise InvalidOperation("monetary values must be decimal strings")
    amount = Decimal(value)
    if not amount.is_finite():
        raise InvalidOperation("monetary values must be finite")
    return amount


def format_usd(value: str) -> str:
    amount = parse_decimal_string(value).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return f"USD {amount}"


def cost_is_successful(cost: object) -> bool:
    if not isinstance(cost, dict) or cost.get("succeeded") is not True:
        return False
    errors = cost.get("pricing_errors")
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors) or errors:
        return False
    items = cost.get("line_items")
    if not isinstance(items, list):
        return False
    try:
        parse_decimal_string(cost.get("monthly_total"))
        for item in items:
            if not isinstance(item, dict):
                return False
            parse_decimal_string(item.get("unit_price"))
            parse_decimal_string(item.get("amount"))
    except InvalidOperation:
        return False
    return True


def can_approve(record: object) -> bool:
    if not isinstance(record, dict) or record.get("status") != "evaluated":
        return False
    if not isinstance(record.get("proposed"), dict):
        return False
    plan_hash = record.get("plan_hash")
    if not isinstance(plan_hash, str) or plan_hash == "":
        return False
    errors = record.get("validation_errors")
    if not isinstance(errors, list) or errors:
        return False
    checks = record.get("policy_checks")
    if not isinstance(checks, list):
        return False
    for check in checks:
        if not isinstance(check, dict) or check.get("status") not in _CHECK_STATUSES:
            return False
        if check.get("status") == "error":
            return False
    return cost_is_successful(record.get("cost"))


def can_reject(record: object) -> bool:
    return isinstance(record, dict) and record.get("status") in {"draft", "evaluated"}


def can_generate_artifact(record: object) -> bool:
    return isinstance(record, dict) and record.get("status") == "approved"


def _plan_id(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    plan_id = record.get("id")
    if isinstance(plan_id, str) and plan_id:
        return plan_id
    return None


def parse_plan_record(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiClientError("The API response was not a JSON object.")
    missing = [key for key in _RECORD_KEYS if key not in body]
    if missing:
        raise ApiClientError(
            "The API response was missing plan fields: " + ", ".join(missing) + "."
        )
    if not isinstance(body["id"], str) or body["id"].strip() == "":
        raise ApiClientError("The API response had an invalid plan id.")
    if body["status"] not in _STATUSES:
        raise ApiClientError("The API response had an unknown plan status.")
    if not isinstance(body["prompt"], str) or not isinstance(body["raw_output"], str):
        raise ApiClientError("The API response had an invalid prompt or raw output.")
    if body["proposed"] is not None and not isinstance(body["proposed"], dict):
        raise ApiClientError("The API response had an invalid proposal.")
    if body["plan_hash"] is not None and not isinstance(body["plan_hash"], str):
        raise ApiClientError("The API response had an invalid plan hash.")
    if not _is_issue_list(body["validation_errors"]):
        raise ApiClientError("The API response had invalid validation errors.")
    if not _is_policy_list(body["policy_checks"]):
        raise ApiClientError("The API response had invalid policy results.")
    _require_cost(body["cost"])
    if body["artifact"] is not None and not isinstance(body["artifact"], str):
        raise ApiClientError("The API response had an invalid artifact.")
    if not isinstance(body["created_at"], str) or not isinstance(body["updated_at"], str):
        raise ApiClientError("The API response had invalid timestamps.")
    return body


def _is_issue_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("code"), str) or not isinstance(item.get("message"), str):
            return False
        field_path = item.get("field_path")
        if field_path is not None and not isinstance(field_path, str):
            return False
    return True


def _is_policy_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("policy_id"), str) or not isinstance(item.get("message"), str):
            return False
        if item.get("status") not in _CHECK_STATUSES:
            return False
    return True


def _require_cost(cost: object) -> None:
    if cost is None:
        return
    if not isinstance(cost, dict):
        raise ApiClientError("The API response had an invalid cost.")
    if not isinstance(cost.get("succeeded"), bool):
        raise ApiClientError("The API response had an invalid cost status.")
    errors = cost.get("pricing_errors")
    if not isinstance(errors, list) or not all(isinstance(item, str) for item in errors):
        raise ApiClientError("The API response had invalid pricing errors.")
    if "monthly_total" in cost:
        _require_money(cost.get("monthly_total"))
    items = cost.get("line_items", [])
    if not isinstance(items, list):
        raise ApiClientError("The API response had invalid cost line items.")
    for item in items:
        if not isinstance(item, dict):
            raise ApiClientError("The API response had an invalid cost line item.")
        for field in ("unit_price", "amount"):
            if field in item:
                _require_money(item.get(field))


def _require_money(value: object) -> None:
    try:
        parse_decimal_string(value)
    except InvalidOperation as exc:
        raise ApiClientError(
            "The API response included a monetary amount that is not a decimal string."
        ) from exc


def _format_detail(detail: object) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts: list[str] = []
        for item in detail:
            if isinstance(item, dict):
                message = item.get("msg")
                location = item.get("loc")
                where = ""
                if isinstance(location, list):
                    where = ".".join(str(part) for part in location)
                if where and isinstance(message, str):
                    parts.append(f"{where}: {message}")
                elif isinstance(message, str):
                    parts.append(message)
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "; ".join(parts)
    return "request validation failed"


def _format_http_error(response: requests.Response) -> str:
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        return (
            f"The API returned HTTP {status} with a non-JSON body. "
            "The request was not retried."
        )
    if isinstance(body, dict):
        code = body.get("code")
        message = body.get("message")
        if isinstance(code, str) and isinstance(message, str):
            return (
                f"The API returned HTTP {status} ({code}): {message} "
                "The request was not retried."
            )
        if "detail" in body:
            return (
                f"The API returned HTTP {status}: {_format_detail(body['detail'])} "
                "The request was not retried."
            )
    return (
        f"The API returned HTTP {status} with an unexpected error body. "
        "The request was not retried."
    )


def request_json(
    method: str,
    path: str,
    *,
    json_body: dict[str, str] | None,
) -> dict[str, Any]:
    """Perform one HTTP call. Mutation requests are not retried."""
    payload = _request_payload(method, path, json_body=json_body)
    return parse_plan_record(payload)


def _request_payload(
    method: str,
    path: str,
    *,
    json_body: dict[str, str] | None,
) -> object:
    url = f"{api_base_url()}{path}"
    try:
        response = requests.request(
            method,
            url,
            json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise ApiClientError("The API timed out. The request was not retried.") from exc
    except requests.ConnectionError as exc:
        raise ApiClientError(
            f"The API at {api_base_url()} is unavailable. The request was not retried."
        ) from exc
    except requests.RequestException as exc:
        raise ApiClientError(
            f"The API request failed ({exc.__class__.__name__}). The request was not retried."
        ) from exc

    if response.status_code in (200, 201):
        try:
            return response.json()
        except ValueError as exc:
            raise ApiClientError(
                "The API returned invalid JSON. The request was not retried."
            ) from exc
    if response.status_code in (404, 409, 422):
        raise ApiClientError(
            _format_http_error(response),
            status_code=response.status_code,
        )
    raise ApiClientError(
        f"The API returned unexpected HTTP {response.status_code}. The request was not retried.",
        status_code=response.status_code,
    )


def create_plan(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    body = {"prompt": prompt}
    if provider is not None:
        body["provider"] = provider
        body["model"] = model or ""
        body["api_key"] = api_key or ""
    return request_json("POST", "/v1/plans", json_body=body)


def fetch_catalog(path: str) -> dict[str, Any]:
    payload = _request_payload("GET", path, json_body=None)
    if not isinstance(payload, dict):
        raise ApiClientError("The API catalog was not a JSON object.")
    return payload


def fetch_decisions() -> list[dict[str, Any]]:
    payload = _request_payload("GET", "/v1/decisions", json_body=None)
    if not isinstance(payload, list):
        raise ApiClientError("The API decision log was not a list.")
    return [parse_plan_record(item) for item in payload]


def approve_plan(plan_id: str, plan_hash: str) -> dict[str, Any]:
    return request_json(
        "POST",
        f"/v1/plans/{plan_id}/approve",
        json_body={"plan_hash": plan_hash},
    )


def reject_plan(plan_id: str) -> dict[str, Any]:
    return request_json("POST", f"/v1/plans/{plan_id}/reject", json_body=None)


def generate_artifact(plan_id: str) -> dict[str, Any]:
    return request_json("POST", f"/v1/plans/{plan_id}/artifact", json_body=None)


def fetch_plan(plan_id: str) -> dict[str, Any]:
    return request_json("GET", f"/v1/plans/{plan_id}", json_body=None)


def _ensure_state() -> None:
    if PLAN_RECORD_KEY not in st.session_state:
        st.session_state[PLAN_RECORD_KEY] = None
    if API_ERROR_KEY not in st.session_state:
        st.session_state[API_ERROR_KEY] = None
    if DECISION_LOG_KEY not in st.session_state:
        st.session_state[DECISION_LOG_KEY] = []


def _remember_success(record: Mapping[str, Any]) -> None:
    stored = copy.deepcopy(dict(record))
    st.session_state[PLAN_RECORD_KEY] = stored
    st.session_state[API_ERROR_KEY] = None
    _remember_decision(stored)


def _remember_decision(record: Mapping[str, Any]) -> None:
    if record.get("status") not in _DECISION_STATUSES:
        return
    current = st.session_state.get(DECISION_LOG_KEY)
    log = list(current) if isinstance(current, list) else []
    plan_id = record.get("id")
    log = [item for item in log if not isinstance(item, dict) or item.get("id") != plan_id]
    log.insert(0, copy.deepcopy(dict(record)))
    st.session_state[DECISION_LOG_KEY] = log


def _remember_error(exc: ApiClientError) -> None:
    st.session_state[API_ERROR_KEY] = {
        "message": str(exc),
        "status_code": exc.status_code,
    }


def _call(operation: Callable[[], dict[str, Any]]) -> None:
    try:
        record = operation()
    except ApiClientError as exc:
        _remember_error(exc)
        return
    _remember_success(record)


def _on_generate() -> None:
    text = st.session_state.get("prompt_text", "")
    scenario = st.session_state.get("scenario", SCENARIO_NONE)
    if not isinstance(text, str):
        text = ""
    if not isinstance(scenario, str):
        scenario = SCENARIO_NONE
    provider = st.session_state.get("planner_provider", PLANNER_BUILTIN)
    if provider == "OpenAI":
        _generate_with_model("openai", OPENAI_MODEL, "openai_api_key", text)
        return
    if provider == "Anthropic":
        _generate_with_model("claude", ANTHROPIC_MODEL, "anthropic_api_key", text)
        return
    prompt = submitted_prompt(text, scenario)
    _call(lambda: create_plan(prompt))


def _generate_with_model(provider: str, model: str, secret_key: str, text: str) -> None:
    secret = st.session_state.get(secret_key, "")
    if not isinstance(secret, str) or secret.strip() == "":
        _remember_error(ApiClientError("Enter an API key. The key is not stored."))
        return
    _call(
        lambda: create_plan(
            text,
            provider=provider,
            model=model,
            api_key=secret,
        )
    )


def _on_load_decisions() -> None:
    try:
        records = fetch_decisions()
    except ApiClientError as exc:
        _remember_error(exc)
        return
    st.session_state[DECISION_LOG_KEY] = records
    st.session_state[API_ERROR_KEY] = None


def _on_show_decision() -> None:
    log = st.session_state.get(DECISION_LOG_KEY)
    choice = st.session_state.get("open_decision", 0)
    if not isinstance(log, list) or not isinstance(choice, int):
        return
    if choice < 0 or choice >= len(log) or not isinstance(log[choice], dict):
        return
    st.session_state[PLAN_RECORD_KEY] = copy.deepcopy(log[choice])
    st.session_state[API_ERROR_KEY] = None


def _on_approve() -> None:
    record = st.session_state.get(PLAN_RECORD_KEY)
    if not can_approve(record):
        return
    plan_id = _plan_id(record)
    plan_hash = record["plan_hash"]
    if plan_id is None or not isinstance(plan_hash, str):
        return
    _call(lambda: approve_plan(plan_id, plan_hash))


def _on_reject() -> None:
    record = st.session_state.get(PLAN_RECORD_KEY)
    if not can_reject(record):
        return
    plan_id = _plan_id(record)
    if plan_id is None:
        return
    _call(lambda: reject_plan(plan_id))


def _on_artifact() -> None:
    record = st.session_state.get(PLAN_RECORD_KEY)
    if not can_generate_artifact(record):
        return
    plan_id = _plan_id(record)
    if plan_id is None:
        return
    _call(lambda: generate_artifact(plan_id))


def _on_refresh() -> None:
    plan_id = _plan_id(st.session_state.get(PLAN_RECORD_KEY))
    if plan_id is None:
        return
    _call(lambda: fetch_plan(plan_id))


_PHASES = (
    ("request", "Request", "Workload"),
    ("review", "Review", "Policy and price"),
    ("decision", "Decision", "Reviewer gate"),
    ("artifact", "Artifact", "Dry-run file"),
)


def _status_of(record: object) -> str | None:
    if isinstance(record, dict) and isinstance(record.get("status"), str):
        return record["status"]
    return None


def _phase(record: object) -> str:
    status = _status_of(record)
    if status == "draft":
        return "review"
    if status in {"evaluated", "rejected"}:
        return "decision"
    if status in {"approved", "artifact_generated"}:
        return "artifact"
    return "request"


def _step_class(name: str, phase: str, status: str | None) -> str:
    order = [item[0] for item in _PHASES]
    if status == "artifact_generated":
        return "current" if name == "artifact" else "done"
    if status == "rejected":
        if name == "decision":
            return "current"
        if name == "artifact":
            return "wait"
    index = order.index(name)
    current = order.index(phase)
    if index < current:
        return "done"
    if index == current:
        return "current"
    return "wait"


def _step_detail(name: str, status: str | None) -> str:
    if name == "decision" and status == "rejected":
        return "Rejected"
    if name == "artifact" and status == "artifact_generated":
        return "Stored on the record"
    for phase_name, _label, detail in _PHASES:
        if phase_name == name:
            return detail
    return ""


def _stepper(record: object) -> str:
    phase = _phase(record)
    status = _status_of(record)
    parts = ['<div class="p2p-bar">']
    for index, (name, label, _detail) in enumerate(_PHASES):
        kind = _step_class(name, phase, status)
        if index:
            rule = "done" if _step_class(_PHASES[index - 1][0], phase, status) == "done" else ""
            parts.append(f'<div class="p2p-rule {rule}"></div>')
        parts.append(
            f'<div class="p2p-step {kind}">'
            f'<span class="p2p-num">{index + 1}</span>'
            f"<span><strong>{label}</strong><small>{_step_detail(name, status)}</small></span>"
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _approval_block_reason(record: object) -> str | None:
    if can_approve(record):
        return None
    status = _status_of(record)
    if status is None:
        return "Generate a plan before approval."
    if status == "rejected":
        return "This plan is rejected. A new request creates a new plan."
    if status == "approved":
        return "This plan is approved. The next step is the dry-run artifact."
    if status == "artifact_generated":
        return "The artifact is already stored. Download it, or start a new request."
    if status == "draft":
        return "Approval needs an evaluated plan. This draft can be rejected."
    if status != "evaluated":
        return "Approval requires status evaluated."
    errors = record.get("validation_errors") if isinstance(record, dict) else None
    if isinstance(errors, list) and errors:
        return "Approval is blocked because the plan has validation errors."
    checks = record.get("policy_checks") if isinstance(record, dict) else None
    if isinstance(checks, list) and any(
        isinstance(item, dict) and item.get("status") == "error" for item in checks
    ):
        return "Approval is blocked because a policy result is an error."
    if isinstance(record, dict) and not cost_is_successful(record.get("cost")):
        return "Approval is blocked because pricing did not succeed."
    return "Approval is blocked."


def _inject_style() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stMainBlockContainer"],
        .block-container {
          max-width: 1080px;
          padding-top: 4rem;
          padding-bottom: 1.25rem;
        }
        [data-testid="stMainBlockContainer"] h1 {
          margin-top: 0;
          padding-top: 0;
        }
        section[data-testid="stSidebar"],
        [data-testid="stSidebarCollapsedControl"] {
          display: none;
        }
        .p2p-bar {
          display: flex;
          align-items: flex-start;
          gap: 8px;
          margin: 4px 0 8px;
        }
        .p2p-step {
          display: flex;
          gap: 10px;
          align-items: center;
          min-width: 148px;
        }
        .p2p-num {
          width: 28px;
          height: 28px;
          border: 1px solid color-mix(in srgb, var(--text-color) 28%, transparent);
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
          font-size: 13px;
          color: var(--text-color);
          background: var(--secondary-background-color);
          flex: 0 0 auto;
        }
        .p2p-step strong { display: block; font-size: 14px; color: var(--text-color); }
        .p2p-step small { display: block; color: var(--text-color); opacity: 0.72; font-size: 12px; }
        .p2p-step.current .p2p-num {
          background: var(--primary-color);
          border-color: var(--primary-color);
          color: var(--background-color);
        }
        .p2p-step.done .p2p-num {
          background: #1d6b45;
          border-color: #1d6b45;
          color: #f7fbf8;
        }
        .p2p-step.wait { opacity: 0.45; }
        .p2p-rule {
          flex: 1;
          height: 1px;
          background: color-mix(in srgb, var(--text-color) 22%, transparent);
          margin-top: 14px;
        }
        .p2p-rule.done { background: #1d6b45; }
        .p2p-policy { width: 100%; border-collapse: collapse; }
        .p2p-policy th, .p2p-policy td {
            text-align: left;
            padding: 0.4rem 0.65rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.15);
            vertical-align: top;
        }
        .p2p-pass, .p2p-fail, .p2p-warn { font-weight: 650; white-space: nowrap; }
        .p2p-pass { color: #1d6b45; }
        .p2p-fail { color: #b42318; }
        .p2p-warn { color: #b54708; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _use_suggested_prompt(prompt: str) -> None:
    st.session_state["prompt_text"] = prompt


def _render_prompt_help() -> None:
    with st.popover("Suggested prompts", type="tertiary"):
        st.markdown("**Suggested prompts**")
        st.caption(
            "Choosing one replaces the request text. "
            "Unrecognized wording still returns one small web container."
        )
        for index, (label, prompt) in enumerate(SUGGESTED_PROMPTS):
            st.button(
                label,
                key=f"suggest_{index}",
                on_click=_use_suggested_prompt,
                args=(prompt,),
                use_container_width=True,
            )
            st.caption(prompt)


def _render_intro(record: object) -> None:
    title_col, reload_col = st.columns([5, 1], gap="small")
    with title_col:
        st.title("Prompt-to-Provisioning Planner")
    with reload_col:
        st.button(
            "Refresh",
            key="refresh",
            disabled=_plan_id(record) is None,
            on_click=_on_refresh,
            use_container_width=True,
        )
    st.caption(
        "Prototype only. Prices are synthetic estimates. Nothing was deployed. "
        f"API base URL: {api_base_url()}"
    )
    st.markdown(_stepper(record), unsafe_allow_html=True)


def _render_request(record: object) -> None:
    st.subheader("1 Request")
    with st.container(border=True):
        st.info(
            "Demo comparison. The built-in planner is a local phrase list. "
            "OpenAI uses gpt-5. Anthropic uses claude-sonnet-5-5. "
            "Compare those readings with the built-in phrase list. "
            "The API key is sent only with this request. It is not saved on the plan, in logs, or on disk. "
            "The same schema, policy, and price checks run after every model."
        )
        st.caption(
            "Describe the workload. The planner returns JSON only. "
            "Schema, policy, price, and approval run after that."
        )
        prompt_col, side_col = st.columns([1.7, 1], gap="medium")
        with prompt_col:
            label_col, help_col, _spacer = st.columns(
                [1.55, 1.35, 1.5],
                gap="small",
                vertical_alignment="center",
            )
            with label_col:
                st.markdown("Infrastructure request")
            with help_col:
                _render_prompt_help()
            st.text_area(
                "Infrastructure request",
                value=EXAMPLE_PROMPT,
                height=148,
                key="prompt_text",
                label_visibility="collapsed",
            )
        with side_col:
            st.selectbox(
                "Planner",
                options=(PLANNER_BUILTIN, "OpenAI", "Anthropic"),
                key="planner_provider",
            )
            planner_choice = st.session_state.get("planner_provider", PLANNER_BUILTIN)
            if planner_choice == "OpenAI":
                st.caption(f"Model: {OPENAI_MODEL}. Chosen for this interpretation task.")
                st.text_input(
                    "API key",
                    type="password",
                    key="openai_api_key",
                    help="Used for this request only. It is not stored.",
                )
            elif planner_choice == "Anthropic":
                st.caption(f"Model: {ANTHROPIC_MODEL}. Chosen for this interpretation task.")
                st.text_input(
                    "API key",
                    type="password",
                    key="anthropic_api_key",
                    help="Used for this request only. It is not stored.",
                )
            st.selectbox(
                "Demo scenario",
                options=(SCENARIO_NONE, *SCENARIO_TOKENS),
                key="scenario",
            )
            st.caption(
                "Scenarios apply to the built-in planner. None uses the request text. "
                "Any other value sends a fixed fixture and skips vocabulary parsing."
            )
            st.button(
                "Generate plan",
                key="generate",
                type="primary" if _phase(record) == "request" else "secondary",
                on_click=_on_generate,
                use_container_width=True,
            )
        st.caption(
            "Recognized words: database, mysql, website, container, object storage, blob storage, "
            "small, medium, big, large, one, single, two, pair, couple, three, dev, test, prod, "
            "US East, Northern Virginia, US West, Oregon, Azure East US. "
            "An unrecognized US or Azure region is an error. It is not replaced with US East. "
            "Other wording is ignored."
        )


def _render_error() -> None:
    error = st.session_state.get(API_ERROR_KEY)
    if not isinstance(error, dict):
        return
    message = error.get("message")
    if isinstance(message, str) and message:
        st.error(message)
    if error.get("status_code") == 409 and _plan_id(st.session_state.get(PLAN_RECORD_KEY)):
        st.info(_CONFLICT_HINT)


def _render_review(record: object) -> None:
    st.subheader("2 Review")
    with st.container(border=True):
        st.caption(
            "Each block is one fact to check. A green tick passed. A red cross blocks approval. "
            "A warning does not."
        )
        if record is None:
            st.text("Generate a plan to review the proposal.")
            return
        if not isinstance(record, dict):
            st.error("The saved plan could not be displayed.")
            return

        _render_review_identity(record)
        resource_col, side_col = st.columns([1.35, 1], gap="medium")
        with resource_col:
            _render_proposal(record.get("proposed"))
        with side_col:
            _render_region_card(record.get("proposed"))
            _render_tags(record.get("proposed"))
        _render_validation(record.get("validation_errors"))
        _render_cost(record.get("cost"))
        _render_policies(record.get("policy_checks"))
        _render_raw_output(record.get("raw_output"))


def _render_review_identity(record: Mapping[str, Any]) -> None:
    st.markdown(f"Status: {record.get('status')} · Plan ID: {record.get('id')}")
    plan_hash = record.get("plan_hash")
    if isinstance(plan_hash, str) and plan_hash:
        st.caption(f"Plan hash: {plan_hash}")
    else:
        st.caption("Plan hash: none")
    generator = record.get("generator")
    if isinstance(generator, str) and generator:
        st.caption(f"Planner: {generator}")
        if generator.startswith("openai:") or generator.startswith("claude:"):
            st.caption(
                "The model interpreted the sentence. The checks below are the decision."
            )
    prompt = record.get("prompt")
    if isinstance(prompt, str):
        st.caption(f"Submitted prompt: {prompt}")


def _render_raw_output(raw_output: object) -> None:
    with st.expander("Untrusted planner output", expanded=True):
        st.caption("Stored unchanged. The planner does not validate this JSON.")
        if isinstance(raw_output, str):
            st.code(_exact_code(raw_output), language="json")
        else:
            st.text("Raw planner output is missing.")


def _render_decision(record: object) -> None:
    st.subheader("3 Decision")
    with st.container(border=True):
        st.caption(
            "The person who writes the request is not the person who accepts it. "
            "If the wording is wrong, change the request and generate again. "
            "That creates a new plan. It leaves this one unchanged."
        )
        st.caption(
            "Reject is the reviewer's record that this exact plan must not proceed. "
            "The price, policies, and hash stay stored, and the plan cannot be approved later. "
            "Approve is the reviewer's record that this hash was accepted."
        )
        reason = _approval_block_reason(record)
        if can_approve(record):
            st.caption(
                "Approve sends the stored plan hash. The service checks that hash, "
                "recomputes it, and does not save the recomputation."
            )
        elif reason:
            st.caption(reason)
        approve_col, reject_col = st.columns(2, gap="medium")
        with approve_col:
            st.button(
                "Approve",
                key="approve",
                type="primary" if can_approve(record) else "secondary",
                disabled=not can_approve(record),
                on_click=_on_approve,
                use_container_width=True,
            )
        with reject_col:
            st.button(
                "Reject",
                key="reject",
                disabled=not can_reject(record),
                on_click=_on_reject,
                use_container_width=True,
            )
        st.caption("Reject sends the plan id only. A rejected plan cannot be approved.")


def _render_decision_log() -> None:
    st.subheader("Decision log")
    with st.container(border=True):
        st.caption(
            "Approved and rejected plans stay in the API process, including plans "
            "that later receive an artifact. Restarting the API clears them. "
            "Load the log after a page refresh."
        )
        st.button("Load stored decisions", key="load_decisions", on_click=_on_load_decisions)
        log = st.session_state.get(DECISION_LOG_KEY)
        rows = [item for item in log if isinstance(item, dict)] if isinstance(log, list) else []
        if not rows:
            st.text("No approved or rejected plans yet.")
            return
        st.table(
            [
                {
                    "Status": item.get("status"),
                    "Planner": item.get("generator", "mock"),
                    "Prompt": _short_prompt(item.get("prompt")),
                    "Plan ID": item.get("id"),
                }
                for item in rows
            ]
        )
        st.selectbox(
            "Open a stored decision",
            options=list(range(len(rows))),
            format_func=lambda index: _decision_label(rows[index]),
            key="open_decision",
        )
        st.button("Show this decision", key="show_decision", on_click=_on_show_decision)


def _short_prompt(prompt: object) -> str:
    if not isinstance(prompt, str):
        return ""
    compact = " ".join(prompt.split())
    if len(compact) <= 72:
        return compact
    return compact[:69] + "..."


def _decision_label(record: dict[str, Any]) -> str:
    return f"{record.get('status')} · {_short_prompt(record.get('prompt'))}"


def _render_artifact_step(record: object) -> None:
    st.subheader("4 Artifact")
    with st.container(border=True):
        st.caption(
            "Dry-run HCL uses fictional demo_* resources. Terraform is not executed."
        )
        st.button(
            "Generate artifact",
            key="artifact",
            type="primary" if can_generate_artifact(record) else "secondary",
            disabled=not can_generate_artifact(record),
            on_click=_on_artifact,
            use_container_width=True,
        )
        artifact = record.get("artifact") if isinstance(record, dict) else None
        _render_artifact(artifact)


def _render_proposal(proposed: object) -> None:
    with st.container(border=True):
        st.markdown("**Resources**")
        st.caption("What the schema accepted. Quantity is the instance count, not the number of rows.")
        _render_resource_table(proposed)


def _render_region_card(proposed: object) -> None:
    with st.container(border=True):
        st.markdown("**Region**")
        st.caption("Where the workload would run. Only US East, US West, and Azure East US are accepted.")
        if not isinstance(proposed, dict):
            st.text("Validated proposal: none")
            return
        st.table(
            [
                {
                    "Region": proposed.get("region"),
                    "Environment": proposed.get("environment"),
                }
            ]
        )


def _render_resource_table(proposed: object) -> None:
    if not isinstance(proposed, dict):
        st.text("Validated proposal: none")
        return
    resources = proposed.get("resources")
    rows: list[dict[str, object]] = []
    has_capacity = False
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            capacity = resource.get("capacity_gb")
            if capacity is not None:
                has_capacity = True
            public = resource.get("public_access")
            rows.append(
                {
                    "Name": resource.get("name"),
                    "Type": resource.get("type"),
                    "SKU": resource.get("sku"),
                    "Qty": resource.get("quantity"),
                    "GB": "" if capacity is None else capacity,
                    "Public": "yes" if public is True else "no" if public is False else public,
                }
            )
    if rows and not has_capacity:
        for row in rows:
            row.pop("GB", None)
    if rows:
        st.table(rows)
    else:
        st.text("Resources: none")


def _render_tags(proposed: object) -> None:
    with st.container(border=True):
        st.markdown("**Tags**")
        st.caption("Policy requires environment, owner, and cost-center.")
        _render_tag_table(proposed)


def _render_tag_table(proposed: object) -> None:
    if not isinstance(proposed, dict):
        st.text("Tags: none")
        return
    tags = proposed.get("tags")
    tag_rows: list[dict[str, str]] = []
    if isinstance(tags, dict):
        tag_rows = [{"Tag": str(key), "Value": str(value)} for key, value in tags.items()]
    if tag_rows:
        st.table(tag_rows)
    else:
        st.text("Tags: none")


def _render_validation(errors: object) -> None:
    with st.container(border=True):
        st.markdown("**Validation**")
        st.caption("Schema result. Invalid planner output is not repaired.")
        _render_validation_body(errors)


def _render_validation_body(errors: object) -> None:
    if not isinstance(errors, list) or not errors:
        st.text("No validation errors.")
        return
    rows: list[dict[str, str]] = []
    for issue in errors:
        if not isinstance(issue, dict):
            continue
        field_path = issue.get("field_path")
        location = f" at {field_path}" if isinstance(field_path, str) and field_path else ""
        rows.append(
            {
                "Field": field_path if isinstance(field_path, str) else "",
                "Message": f"Validation error: {issue.get('code')}{location}: {issue.get('message')}",
            }
        )
    if rows:
        st.table(rows)


def _render_policies(checks: object) -> None:
    with st.container(border=True):
        st.markdown("**Policies**")
        st.caption("A green tick passed. A red cross blocks approval. A warning does not.")
        if not isinstance(checks, list) or not checks:
            st.text("No policy results.")
            return
        passed = sum(isinstance(item, dict) and item.get("status") == "passed" for item in checks)
        warnings = sum(
            isinstance(item, dict) and item.get("status") == "warning" for item in checks
        )
        errors = sum(isinstance(item, dict) and item.get("status") == "error" for item in checks)
        st.caption(f"Passed {passed}. Warnings {warnings}. Errors {errors}.")
        rows: list[dict[str, str]] = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            resource_name = check.get("resource_name")
            field_path = check.get("field_path")
            rows.append(
                {
                    "Policy": str(check.get("policy_id", "")),
                    "Result": str(check.get("status", "")),
                    "Detail": str(check.get("message", "")),
                    "Resource": resource_name if isinstance(resource_name, str) else "",
                    "Field": field_path if isinstance(field_path, str) else "",
                }
            )
        rows = _drop_blank_columns(rows, keep=("Policy", "Result", "Detail"))
        if rows:
            st.markdown(_policy_table(rows), unsafe_allow_html=True)
        else:
            st.text("No policy results.")


def _policy_mark(status: str) -> str:
    if status == "passed":
        return '<span class="p2p-pass">✓ passed</span>'
    if status == "error":
        return '<span class="p2p-fail">✗ error</span>'
    if status == "warning":
        return '<span class="p2p-warn">! warning</span>'
    return html.escape(status)


def _policy_table(rows: list[dict[str, str]]) -> str:
    headers = list(rows[0])
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body: list[str] = []
    for row in rows:
        cells: list[str] = []
        for header in headers:
            value = row[header]
            if header == "Result":
                cells.append(f'<td class="result">{_policy_mark(value)}</td>')
            else:
                cells.append(f"<td>{html.escape(value)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<table class="p2p-policy">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _drop_blank_columns(
    rows: list[dict[str, str]],
    *,
    keep: tuple[str, ...],
) -> list[dict[str, str]]:
    if not rows:
        return rows
    filled = {
        key
        for row in rows
        for key, value in row.items()
        if key not in keep and value.strip() != ""
    }
    order = [key for key in rows[0] if key in keep or key in filled]
    return [{key: row[key] for key in order} for row in rows]


def _render_cost(cost: object) -> None:
    with st.container(border=True):
        st.markdown("**Synthetic cost**")
        st.caption("Local catalog only. An unknown SKU blocks approval. This is not a cloud bill.")
        _render_cost_body(cost)


def _render_cost_body(cost: object) -> None:
    if cost_is_successful(cost) and isinstance(cost, dict):
        monthly_total = cost["monthly_total"]
        st.markdown(f"Monthly total: {format_usd(monthly_total)}")
        st.caption(f"Exact monthly total: {monthly_total}")
        rows: list[dict[str, object]] = []
        for item in cost["line_items"]:
            amount = item["amount"]
            rows.append(
                {
                    "Resource": item.get("resource_name"),
                    "SKU": item.get("sku"),
                    "Qty": item.get("quantity"),
                    "Unit price": item.get("unit_price"),
                    "Exact": amount,
                    "USD": format_usd(amount),
                }
            )
        if rows:
            st.table(rows)
        return
    st.text("Cost unavailable")
    messages: list[str] = []
    if isinstance(cost, dict):
        raw_errors = cost.get("pricing_errors")
        if isinstance(raw_errors, list):
            messages.extend(item for item in raw_errors if isinstance(item, str) and item)
    if cost is None:
        st.text("Pricing did not run.")
    elif messages:
        for message in messages:
            st.text(f"Pricing error: {message}")
    else:
        st.text("Pricing did not succeed.")


def _exact_code(value: str) -> str:
    """Preserve the caller's text in ``st.code``.

    Streamlit removes one leading newline and one trailing newline before
    display. Pad those edges so the code block matches the original string.
    """
    padded = value
    if padded.startswith("\n"):
        padded = "\n" + padded
    if padded.endswith("\n"):
        padded = padded + "\n"
    return padded


def _render_artifact(artifact: object) -> None:
    st.markdown("**Dry-run artifact**")
    if isinstance(artifact, str) and artifact != "":
        st.download_button(
            "Download main.tf",
            data=artifact,
            file_name="main.tf",
            mime="text/plain",
            key="download_main_tf",
            use_container_width=True,
        )
        st.code(_exact_code(artifact), language="hcl")
        return
    st.text("No artifact has been generated.")


def _render_readme() -> None:
    st.title("Read me")
    st.markdown(
        """
This prototype turns a plain-language infrastructure request into a plan a
person can review. It does not connect to a cloud account, and it does not
deploy anything.

### What happens to a request

1. Streamlit sends the text to the API over HTTP. The page does not plan, price, or approve on its own.
2. The built-in planner returns one JSON string from a phrase list. OpenAI (`gpt-5`) or Anthropic (`claude-sonnet-5-5`) can interpret the sentence instead. The API key is sent with that request and is not stored. The returned string is untrusted. The planner does not validate it, price it, or repair it.
3. The API parses the JSON against a strict schema. Invalid JSON is stored as a draft and still returned. Missing fields are not filled in.
4. A valid plan is checked by six policies and priced from a local synthetic catalog. The proposal is then hashed. The hash is written once.
5. A reviewer approves or rejects. The author changes the request to make a new plan; Reject records that this plan must not proceed. Approve resubmits the stored hash, and the API recomputes the hash to confirm the proposal has not changed. Reject sends only the plan id. Approved and rejected plans stay in the decision log.
6. After approval, the API can render a dry-run Terraform-style file. The resources are fictional `demo_*` blocks. Terraform is not executed.

### Architecture

One FastAPI process owns the rules. One Streamlit process is the client. Plan state is a dictionary in the API process. Restarting the API drops every plan. The API writes a log line to its own stderr for each create, approval, rejection, artifact, and refusal. That line names the plan and the outcome. It does not include the request body or an API key.

| Piece | Role |
|---|---|
| `ui/app.py` | HTTP client. Shows the plan and sends approve, reject, and artifact calls. |
| `app/main.py` | Routes. They validate the HTTP body and call the service. |
| `app/planner.py` | Untrusted proposal. Returns a JSON string only. |
| `app/validation.py` | Parses JSON and checks the schema. Does not change invalid input. |
| `app/policies.py` | Reports pass, warning, or error. Does not edit the plan. |
| `app/pricing.py` | Synthetic monthly estimate, using `Decimal`. An unknown SKU fails the estimate. |
| `app/hashing.py` | Canonical SHA-256 of the proposal. |
| `app/artifacts.py` | Renders HCL only when the service has already approved the plan. |
| `app/store.py` | In-memory get and put. |

The planner may mention tags that happen to satisfy policy. Policy code still evaluates them. A prompt that matches no known resource, such as “build a rocket,” becomes one small web container because unrecognized words are ignored.

Data moves in one direction. The page posts the sentence. The planner returns a string. Schema, policy, and price decide. The hash is stored once. Approve and the artifact both recompute that hash and refuse the plan if it no longer matches. The Prices and Policies pages read the same JSON catalogs the checks use.

### Plan states

`draft` can be rejected. `evaluated` can be approved or rejected. `approved` can be rendered. `rejected` is finished. `artifact_generated` keeps the HCL on the record. Generating the file again is refused. Read it from the stored plan.

Prices are synthetic estimates. Nothing was deployed.
        """
    )


def _render_prices() -> None:
    st.title("Prices")
    st.caption(
        "Synthetic monthly rates from the local catalog. "
        "An unknown SKU has no row here and blocks approval. This is not a cloud bill."
    )
    try:
        payload = fetch_catalog("/v1/catalog/prices")
    except ApiClientError as exc:
        st.error(str(exc))
        return
    rows: list[dict[str, str]] = []
    for type_name, rates in payload.items():
        if not isinstance(rates, dict):
            continue
        unit = "per GB" if type_name == "object_storage" else "per instance"
        for sku, price in rates.items():
            shown = f"USD {price}" if isinstance(price, str) else str(price)
            rows.append(
                {
                    "Type": str(type_name),
                    "SKU": str(sku),
                    "Unit": unit,
                    "Monthly price": shown,
                }
            )
    if rows:
        st.table(rows)
    else:
        st.text("The price catalog is empty.")


def _render_policy_catalog() -> None:
    st.title("Policies")
    st.caption(
        "These settings are the allow-lists. The check functions report a result "
        "and do not change the plan. A warning does not block approval."
    )
    try:
        payload = fetch_catalog("/v1/catalog/policy")
    except ApiClientError as exc:
        st.error(str(exc))
        return
    st.table(_policy_catalog_rows(payload))


def _policy_catalog_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    regions = payload.get("allowed_regions")
    if isinstance(regions, list):
        rows.append(
            {
                "Rule": "Allowed regions",
                "Setting": ", ".join(str(item) for item in regions),
                "If it fails": "error",
            }
        )
    tags = payload.get("required_tags")
    if isinstance(tags, list):
        rows.append(
            {
                "Rule": "Required tags",
                "Setting": ", ".join(str(item) for item in tags),
                "If it fails": "error",
            }
        )
    limits = payload.get("quantity_limits")
    if isinstance(limits, dict):
        setting = ", ".join(f"{name} ≤ {limit}" for name, limit in limits.items())
        rows.append({"Rule": "Quantity limits", "Setting": setting, "If it fails": "error"})
    storage = payload.get("object_storage_capacity_gb")
    if storage is not None:
        rows.append(
            {
                "Rule": "Object storage capacity",
                "Setting": f"≤ {storage} GB",
                "If it fails": "error",
            }
        )
    rows.append(
        {
            "Rule": "Public object storage",
            "Setting": "public_access must be false",
            "If it fails": "error",
        }
    )
    allowed = payload.get("dev_allowed_skus")
    if isinstance(allowed, dict):
        rows.append(
            {
                "Rule": "Development SKUs",
                "Setting": _sku_setting(allowed),
                "If it fails": "error",
            }
        )
    warnings = payload.get("dev_warning_skus")
    if isinstance(warnings, dict):
        rows.append(
            {
                "Rule": "Higher-cost development SKUs",
                "Setting": _sku_setting(warnings),
                "If it fails": "warning",
            }
        )
    return rows


def _sku_setting(groups: dict[str, Any]) -> str:
    parts: list[str] = []
    for type_name, skus in groups.items():
        if isinstance(skus, list):
            parts.append(f"{type_name}: {', '.join(str(sku) for sku in skus)}")
    return "; ".join(parts)


def _render_workflow() -> None:
    st.title("Workflow")
    st.caption(
        "Click a button to watch that request move through the system. "
        "This diagram does not call the API."
    )
    if not _WORKFLOW_HTML.is_file():
        st.error("The workflow diagram is missing.")
        return
    components.html(
        _WORKFLOW_HTML.read_text(encoding="utf-8"),
        height=920,
        scrolling=True,
    )


def _render_work() -> None:
    _ensure_state()
    record = st.session_state.get(PLAN_RECORD_KEY)
    _render_intro(record)
    _render_error()
    _render_request(record)
    _render_review(record)
    _render_decision(record)
    _render_artifact_step(record)
    _render_decision_log()


def main() -> None:
    st.set_page_config(
        page_title="Prompt-to-Provisioning Planner",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_style()
    selected = st.navigation(
        [
            st.Page(_render_readme, title="Read me", url_path="readme", default=False),
            st.Page(_render_workflow, title="Workflow", url_path="workflow", default=False),
            st.Page(_render_prices, title="Prices", url_path="prices", default=False),
            st.Page(_render_policy_catalog, title="Policies", url_path="policies", default=False),
            st.Page(_render_work, title="Work", url_path="work", default=True),
        ],
        position="top",
    )
    selected.run()


if __name__ == "__main__":
    main()
