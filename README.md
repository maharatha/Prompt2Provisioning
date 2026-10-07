# Prompt-to-Provisioning Planner

This prototype turns a plain-language infrastructure request into a reviewable deployment plan. The API proposes a plan with a deterministic mock planner, then validates, prices, and stores it. Approval is bound to a canonical hash. An approved plan can be rendered as a dry-run Terraform-style document. Nothing is deployed.

**Safety:** this project does not access a real cloud, does not deploy infrastructure, and uses synthetic data only.

The Streamlit client is `ui/app.py`. It calls the API over HTTP. Docker Compose runs both processes; see [Docker Compose](#docker-compose).

## API

JSON only. No authentication. Run one API worker. Plan state is an in-memory dict in that process and is not locked. Handlers are async and call the service synchronously, with no await during a create, approval, rejection, or artifact transition.

| Method | Path | Result |
|---|---|---|
| `POST` | `/v1/plans` | Body `{"prompt": "<nonblank string>"}`. **201** stored `PlanRecord`, including a draft when planner JSON is invalid. An unknown `SCENARIO:` name is **422** `{"code": "unknown_scenario", "message": "..."}` and is not stored. |
| `GET` | `/v1/plans/{plan_id}` | **200** `PlanRecord`, or **404** `{"code": "not_found", "message": "..."}`. |
| `POST` | `/v1/plans/{plan_id}/approve` | Body `{"plan_hash": "<string>"}`. **200** updated record. **409** `{"code", "message"}` names the failed gate. The service checks hash format. |
| `POST` | `/v1/plans/{plan_id}/reject` | No body. **200** for a draft or evaluated plan. **409** for `approved`, `rejected`, or `artifact_generated`. |
| `POST` | `/v1/plans/{plan_id}/artifact` | No body. **200** updated record with the HCL in `artifact`. **409** when generation is refused, including a repeated call. |
| `GET` | `/v1/schema/plan` | `ProposedPlan` JSON Schema. |
| `GET` | `/health` | `{"status": "ok", "service": "prompt-to-provisioning-planner"}`. |

`PlanRecord` fields are `id`, `prompt`, `raw_output`, `status`, `proposed`, `plan_hash`, `validation_errors`, `policy_checks`, `cost`, `artifact`, `created_at`, and `updated_at`. `artifact` is null until generation. Money fields (`monthly_total`, `unit_price`, `amount`) are JSON strings. The API does not convert them to floats and does not quantize them.

The prompt is a strict string. Blank or whitespace-only prompts, non-strings, and extra request fields are FastAPI **422** responses with `detail`. `plan_hash` is a strict string; an empty or malformed hash is **409** from the service. `plan_id` is a UUID.

Service errors are `{"code", "message"}` only. Responses do not include stack traces.

A plan that fails schema, policy, or pricing is still stored. Approve returns **409** in those cases. The response has no `approval_blocked` field. After generation, read the HCL from `artifact` on the stored record. Calling the artifact route again returns **409**.

## Repository structure

```
├── AGENTS.md
├── ARCHITECTURE.md
├── app/
│   ├── main.py
│   ├── models.py
│   ├── store.py
│   ├── planner.py
│   ├── validation.py
│   ├── policies.py
│   ├── pricing.py
│   ├── hashing.py
│   ├── artifacts.py
│   ├── services.py
│   ├── data/prices.json
│   └── templates/main.tf.j2
├── ui/
│   └── app.py
├── tests/
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile.api
├── Dockerfile.ui
├── docker-compose.yml
├── .dockerignore
└── README.md
```

## Prerequisite

Python 3.12

## Local commands

Create a virtual environment:

```
python -m venv .venv
```

Windows PowerShell:

```
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```
source .venv/bin/activate
```

Install dependencies:

```
python -m pip install -r requirements-dev.txt
```

Run the API:

```
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Use one worker. Do not pass `--workers` greater than 1. The in-memory store lives in that process. Stop the API or the UI with Ctrl+C in its terminal. Stopping or restarting the API process drops every plan. The UI must not reuse a plan id from before that restart.

Then open `http://127.0.0.1:8000/health`.

Run the UI in a second terminal:

```
python -m streamlit run ui/app.py
```

The UI reads `API_BASE_URL`. When that variable is unset, it uses `http://localhost:8000`.

Run tests:

```
python -m pytest
```

Planning, policy, pricing, approval, and dry-run artifact generation are available through the API above. The Streamlit client reviews those results and sends approve, reject, and artifact requests.

## Docker Compose

Images use Python 3.12 slim and install `requirements.txt` only. Test tools in `requirements-dev.txt` are not installed in the images. The API image includes `app/data/prices.json` and `app/templates/main.tf.j2`.

Build the images:

```
docker compose build
```

Start the API and UI:

```
docker compose up -d
```

Stop and remove the containers and network. This does not delete the images:

```
docker compose down
```

Published URLs, bound on `127.0.0.1` only:

- API health: `http://127.0.0.1:8000/health`
- UI: `http://127.0.0.1:8501`

The API container runs one uvicorn worker (`--workers 1`). The in-memory store lives in that process. Restarting the API container drops every plan. The UI must not assume a plan id survives an API restart.

Compose sets `API_BASE_URL=http://api:8000` on the UI. The UI starts only after the API health check succeeds. There is no database and no persisted volume.

## Planner limits

The mock planner returns a JSON string. It does not validate, price, approve, or render. It recognizes a small vocabulary: postgres or database, container or web container or web application, object storage, dev or test or prod, US East (`us-east-1`), US West (`us-west-2`), Azure East US (`eastus2`), small, medium, low cost, and quantities one, two, and three. Other wording is ignored. A prompt with no recognized resource still returns one small web container. `low cost` keeps the small default and does not replace an explicit medium.

`SCENARIO:<name>` skips that parsing and returns a fixed string. The string is not repaired. Names: `malformed`, `missing_region`, `missing_tags`, `unknown_type`, `unsupported_sku`, `excessive_qty`, `public_storage`. An unknown name is **422** `unknown_scenario` and is not stored.

Policy rules and the state machine are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Synthetic prices and artifacts

Prices come from `app/data/prices.json` and are synthetic estimates. The catalog is `container-small` 18, `container-medium` 42, `db-small` 35, `db-medium` 95, and `storage-standard` 0.025 per GB. The assignment example totals `71` (`2 * 18 + 35`). The API returns that decimal as the string `"71"`. The UI labels it `USD 71.00`. An unknown SKU fails the estimate and blocks approval.

An artifact is fictional `demo_*` HCL. It is not `aws_*` or `azurerm_*` configuration, and it says that nothing was deployed. The application does not run Terraform.

## Approval integrity

Approve sends the `plan_hash` stored on the plan record. Approval requires that submitted hash to equal the stored hash, and a fresh hash of the stored proposal to equal it as well. The fresh hash is not written back. If the stored proposal changes after evaluation, approval returns **409** `current_hash` even when the client sends the old hash. Artifact generation repeats that comparison. Reject sends the plan id only and does not check a hash.

## Demo

Start the API first. This script checks the happy path, a non-blocking warning, a schema failure, a policy failure, and a pricing failure:

```python
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
HAPPY = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)


def request(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    call = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(call) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def create(prompt):
    status, record = request("POST", "/v1/plans", {"prompt": prompt})
    assert status == 201, (status, record)
    return record


happy = create(HAPPY)
assert happy["status"] == "evaluated"
assert happy["cost"]["monthly_total"] == "71"
approved_status, approved = request(
    "POST",
    f"/v1/plans/{happy['id']}/approve",
    {"plan_hash": happy["plan_hash"]},
)
assert approved_status == 200 and approved["status"] == "approved"
artifact_status, generated = request("POST", f"/v1/plans/{happy['id']}/artifact")
assert artifact_status == 200 and "Nothing was deployed." in generated["artifact"]

warning = create("A medium PostgreSQL database for a development team in US East.")
assert any(
    item["policy_id"] == "dev_medium_cost" and item["status"] == "warning"
    for item in warning["policy_checks"]
)
warning_status, _warning_approved = request(
    "POST",
    f"/v1/plans/{warning['id']}/approve",
    {"plan_hash": warning["plan_hash"]},
)
assert warning_status == 200

schema = create("SCENARIO:malformed")
assert schema["status"] == "draft"
assert schema["validation_errors"][0]["code"] == "json_invalid"
schema_status, schema_error = request(
    "POST", f"/v1/plans/{schema['id']}/approve", {"plan_hash": "a" * 64}
)
assert schema_status == 409 and schema_error["code"] == "status"

policy = create("SCENARIO:public_storage")
assert any(item["status"] == "error" for item in policy["policy_checks"])
policy_status, policy_error = request(
    "POST",
    f"/v1/plans/{policy['id']}/approve",
    {"plan_hash": policy["plan_hash"]},
)
assert policy_status == 409 and policy_error["code"] == "policy_error"

pricing = create("SCENARIO:unsupported_sku")
assert pricing["cost"]["succeeded"] is False
pricing_status, pricing_error = request(
    "POST",
    f"/v1/plans/{pricing['id']}/approve",
    {"plan_hash": pricing["plan_hash"]},
)
assert pricing_status == 409 and pricing_error["code"] == "pricing"
print("demo checks passed")
```

The same prompts work in the UI. The assignment prompt shows `USD 71.00`, then Approve, Generate artifact, and Download `main.tf`. The medium development prompt keeps Approve enabled. Malformed output and public storage disable Approve. Unsupported SKU disables Approve because pricing did not succeed.
