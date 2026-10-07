# Prompt-to-Provisioning Planner

This prototype turns a plain-language infrastructure request into a reviewable deployment plan. The API proposes a plan with a deterministic mock planner, then validates, prices, and stores it. Approval is bound to a canonical hash. An approved plan can be rendered as a dry-run Terraform-style document. Nothing is deployed.

**Safety:** this project does not access a real cloud, does not deploy infrastructure, and uses synthetic data only.

The Streamlit UI and Docker Compose packaging are later phases.

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
├── tests/
│   ├── test_api.py
│   └── test_health.py
├── requirements.txt
├── requirements-dev.txt
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

Use one worker. Do not pass `--workers` greater than 1. The in-memory store lives in that process.

Then open `http://127.0.0.1:8000/health`.

Run tests:

```
python -m pytest
```

Planning, policy, pricing, approval, and dry-run artifact generation are available through the API above. Later phases add the UI and Docker Compose packaging.
