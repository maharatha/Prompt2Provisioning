"""Streamlit HTTP client for the prompt-to-provisioning planner.

The UI sends prompts and review actions to the API and renders the returned
plan record. It does not validate plans, apply policy, price resources, hash
proposals, or render Terraform itself.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import requests
import streamlit as st

DEFAULT_API_BASE_URL = "http://localhost:8000"
REQUEST_TIMEOUT = (5, 30)
EXAMPLE_PROMPT = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)
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
            payload = response.json()
        except ValueError as exc:
            raise ApiClientError(
                "The API returned invalid JSON. The request was not retried."
            ) from exc
        return parse_plan_record(payload)
    if response.status_code in (404, 409, 422):
        raise ApiClientError(
            _format_http_error(response),
            status_code=response.status_code,
        )
    raise ApiClientError(
        f"The API returned unexpected HTTP {response.status_code}. The request was not retried.",
        status_code=response.status_code,
    )


def create_plan(prompt: str) -> dict[str, Any]:
    return request_json("POST", "/v1/plans", json_body={"prompt": prompt})


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


def _remember_success(record: Mapping[str, Any]) -> None:
    st.session_state[PLAN_RECORD_KEY] = copy.deepcopy(dict(record))
    st.session_state[API_ERROR_KEY] = None


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
    prompt = submitted_prompt(text, scenario)
    _call(lambda: create_plan(prompt))


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


def _render_intro() -> None:
    st.title("Prompt-to-Provisioning Planner")
    st.info(
        "This is a prototype. Prices are synthetic estimates. Nothing was deployed."
    )
    st.caption(f"API base URL: {api_base_url()}")


def _render_form(record: object) -> None:
    st.text_area(
        "Infrastructure request",
        value=EXAMPLE_PROMPT,
        height=140,
        key="prompt_text",
    )
    st.selectbox(
        "Demo scenario (optional)",
        options=(SCENARIO_NONE, *SCENARIO_TOKENS),
        key="scenario",
    )
    st.caption(
        "Choose a scenario to send SCENARIO:<name> with this prompt. "
        "None uses the text above."
    )
    actions = (
        ("Generate plan", "generate", False, _on_generate),
        ("Approve", "approve", not can_approve(record), _on_approve),
        ("Reject", "reject", not can_reject(record), _on_reject),
        (
            "Generate artifact",
            "artifact",
            not can_generate_artifact(record),
            _on_artifact,
        ),
        ("Refresh", "refresh", _plan_id(record) is None, _on_refresh),
    )
    for column, (label, key, disabled, callback) in zip(
        st.columns(len(actions)),
        actions,
        strict=True,
    ):
        with column:
            st.button(label, key=key, disabled=disabled, on_click=callback)


def _render_error() -> None:
    error = st.session_state.get(API_ERROR_KEY)
    if not isinstance(error, dict):
        return
    message = error.get("message")
    if isinstance(message, str) and message:
        st.error(message)
    if error.get("status_code") == 409 and _plan_id(st.session_state.get(PLAN_RECORD_KEY)):
        st.info(_CONFLICT_HINT)


def _render_record(record: object) -> None:
    if record is None:
        st.text("Generate a plan to review the proposal.")
        return
    if not isinstance(record, dict):
        st.error("The saved plan could not be displayed.")
        return

    st.subheader("Plan")
    st.text(f"Status: {record.get('status')}")
    st.text(f"Plan ID: {record.get('id')}")
    plan_hash = record.get("plan_hash")
    if isinstance(plan_hash, str) and plan_hash:
        st.text(f"Plan hash: {plan_hash}")
    else:
        st.text("Plan hash: none")
    prompt = record.get("prompt")
    if isinstance(prompt, str):
        st.text(f"Submitted prompt: {prompt}")

    st.subheader("Raw planner output")
    raw_output = record.get("raw_output")
    if isinstance(raw_output, str):
        st.code(_exact_code(raw_output), language="json")
    else:
        st.text("Raw planner output is missing.")

    _render_proposal(record.get("proposed"))
    _render_validation(record.get("validation_errors"))
    _render_policies(record.get("policy_checks"))
    _render_cost(record.get("cost"))
    _render_artifact(record.get("artifact"))


def _render_proposal(proposed: object) -> None:
    st.subheader("Validated proposal")
    if not isinstance(proposed, dict):
        st.text("Validated proposal: none")
        return
    st.text(f"Region: {proposed.get('region')}")
    st.text(f"Environment: {proposed.get('environment')}")
    tags = proposed.get("tags")
    tag_rows: list[dict[str, str]] = []
    if isinstance(tags, dict):
        tag_rows = [{"key": str(key), "value": str(value)} for key, value in tags.items()]
    if tag_rows:
        st.text("Tags")
        st.table(tag_rows)
    else:
        st.text("Tags: none")
    resources = proposed.get("resources")
    rows: list[dict[str, object]] = []
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            capacity = resource.get("capacity_gb")
            rows.append(
                {
                    "name": resource.get("name"),
                    "type": resource.get("type"),
                    "sku": resource.get("sku"),
                    "quantity": resource.get("quantity"),
                    "capacity_gb": "" if capacity is None else capacity,
                    "public_access": resource.get("public_access"),
                }
            )
    if rows:
        st.text("Resources")
        st.table(rows)
    else:
        st.text("Resources: none")


def _render_validation(errors: object) -> None:
    st.subheader("Validation errors")
    if not isinstance(errors, list) or not errors:
        st.text("No validation errors.")
        return
    for issue in errors:
        if not isinstance(issue, dict):
            continue
        field_path = issue.get("field_path")
        location = f" at {field_path}" if isinstance(field_path, str) and field_path else ""
        st.text(f"Validation error: {issue.get('code')}{location}: {issue.get('message')}")


def _render_policies(checks: object) -> None:
    st.subheader("Policy results")
    if not isinstance(checks, list) or not checks:
        st.text("No policy results.")
        return
    rows: list[dict[str, str]] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        resource_name = check.get("resource_name")
        field_path = check.get("field_path")
        rows.append(
            {
                "policy_id": str(check.get("policy_id", "")),
                "status": str(check.get("status", "")),
                "message": str(check.get("message", "")),
                "resource_name": resource_name if isinstance(resource_name, str) else "",
                "field_path": field_path if isinstance(field_path, str) else "",
            }
        )
    if rows:
        st.table(rows)
    else:
        st.text("No policy results.")


def _render_cost(cost: object) -> None:
    st.subheader("Synthetic cost estimate")
    if cost_is_successful(cost) and isinstance(cost, dict):
        monthly_total = cost["monthly_total"]
        st.text(f"Monthly total: {format_usd(monthly_total)}")
        st.text(f"Exact monthly total: {monthly_total}")
        rows: list[dict[str, object]] = []
        for item in cost["line_items"]:
            amount = item["amount"]
            rows.append(
                {
                    "resource_name": item.get("resource_name"),
                    "sku": item.get("sku"),
                    "quantity": item.get("quantity"),
                    "unit_price": item.get("unit_price"),
                    "amount": amount,
                    "amount_usd": format_usd(amount),
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
    st.subheader("Dry-run artifact")
    if isinstance(artifact, str) and artifact != "":
        st.code(_exact_code(artifact), language="hcl")
        st.download_button(
            "Download main.tf",
            data=artifact,
            file_name="main.tf",
            mime="text/plain",
            key="download_main_tf",
        )
        return
    st.text("No artifact has been generated.")


def main() -> None:
    st.set_page_config(page_title="Prompt-to-Provisioning Planner", layout="centered")
    _ensure_state()
    _render_intro()
    _render_form(st.session_state.get(PLAN_RECORD_KEY))
    _render_error()
    _render_record(st.session_state.get(PLAN_RECORD_KEY))


if __name__ == "__main__":
    main()
