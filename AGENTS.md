# Agent instructions: Prompt-to-Provisioning Planner

Permanent engineering rules for this repository. Follow them for all implementation, review, and documentation work.

This prototype accepts a plain-language infrastructure request, uses a **deterministic mock planner** to propose JSON, then uses **deterministic application code** to validate, evaluate policies, estimate synthetic cost, bind approval to a plan hash, and emit a dry-run Terraform-style artifact. Nothing may connect to, modify, or deploy into a real cloud.

Keep the solution achievable in **6–8 hours**. Prefer small, explicit functions and a few Pydantic models over extra layers.

## Technology

Required:

- Python 3.12
- FastAPI (REST API)
- Pydantic v2 (authoritative schema)
- Streamlit (demo UI; HTTP client only)
- pytest
- Jinja2 (deterministic HCL generation)
- In-memory persistence (process dict; no database)
- Local synthetic price catalog only
- Deterministic mock planner by default
- Docker Compose for local API + UI

Do not require: a real LLM, paid APIs, cloud credentials, a cloud account, Terraform CLI, a database, authentication, Kubernetes, or a message broker.

Packaging: `requirements.txt` at the repo root. Do not introduce a `src/` layout, Alembic, or a multi-package workspace.

Approved dependencies (add others only with clear value): `fastapi`, `uvicorn`, `pydantic>=2`, `jinja2`, `streamlit`, `httpx`, `pytest`.

## Repository layout

```
README.md
AGENTS.md
requirements.txt
docker-compose.yml
Dockerfile.api
Dockerfile.ui
app/
  main.py              # FastAPI app and thin route handlers
  models.py            # Pydantic models and enums
  store.py             # InMemoryStore
  planner.py           # mock LLM; returns str only
  validation.py        # JSON parse + schema
  policies.py          # policy functions; never mutate the plan
  pricing.py           # Decimal estimates from local catalog
  hashing.py           # canonical SHA-256
  artifacts.py         # Jinja2 HCL render
  services.py          # create / evaluate / approve / reject / artifact
  templates/main.tf.j2
  data/prices.json
ui/
  app.py               # Streamlit; HTTP only
tests/
  test_planner.py
  test_validation.py
  test_policies.py
  test_pricing.py
  test_approval.py
  test_artifacts.py
  test_api.py
```

Do not add `core/domain/infrastructure` folders, repository interfaces, or a second service process.

## Architecture

- One FastAPI application. One Streamlit UI.
- Streamlit talks to the API **only over HTTP**. It must not import `app.services`, `app.store`, `app.policies`, or other domain modules.
- Business logic lives in `services.py` and the small modules it calls. Route handlers validate HTTP input, call the service, and map results to status codes.
- Persistence is a single in-memory dict keyed by plan id. A restart clears state; the UI must not assume ids survive process restart.
- Create and evaluate in **one** `POST /v1/plans`. No PATCH/edit API. Tests that need mutation write the store directly.
- Optional replaceable boundary: a ~5-line `Planner` Protocol with `generate(prompt: str) -> str`. Default implementation: `MockPlanner`. Do not implement a real LLM client in this prototype.
- Do not add repository interfaces, policy DSLs, queues, event buses, cloud SDKs, background jobs, or microservices.

### Module trust table

| Module | May do | Must not do |
|---|---|---|
| `planner.py` | Return a JSON **string** | Validate, score policy, price, approve, write artifacts, repair JSON |
| `validation.py` | `json.loads` + `ProposedPlan.model_validate` | Mutate or default invalid fields |
| `policies.py` | Return policy results | Change the plan |
| `pricing.py` | Sum catalog prices with Decimal | Approve or generate IaC; skip unpriced lines |
| `hashing.py` | Canonical SHA-256 | Change stored `plan_hash` except at evaluation |
| `services.py` | Own state transitions | Call a cloud SDK or Terraform CLI |
| `artifacts.py` | Render HCL after approval + hash re-check | Run if status is not `approved` or the hash drifted |
| `store.py` | Get/put records | Apply policy or pricing |

## AI trust boundary

The mock planner (and any future LLM) is an **untrusted proposal generator**.

- The planner may return **only** a raw JSON string.
- It must not validate schema, enforce policies, calculate authoritative cost, approve plans, or generate artifacts.
- Never silently repair, normalize, coerce, or default invalid generated output.
- Malformed JSON, extra fields, missing fields, and type errors become **explicit validation errors**.
- Unknown resource types, SKUs, regions, or required values **fail closed**.
- Deterministic code owns schema validation, policies, pricing, approval, hashing, and artifact generation.

Malformed generator output is a first-class demo path: persist as `draft` with `validation_errors` and HTTP **201** (not 400). Approval of that draft returns **409**.

## Schema

Pydantic v2 models in `app/models.py` are the authoritative deployment-plan schema.

Generated-plan models (`Resource`, `ProposedPlan`) must use `extra="forbid"` and strict field constraints.

Use `int` for `quantity` and `capacity_gb`. Do **not** use `float` for currency or infrastructure capacity. Currency uses `Decimal` only (see Pricing).

Require `capacity_gb` when `type` is `object_storage`. Policies and pricing must use `quantity` (and storage `capacity_gb`), not `len(resources)`.

**Resource types (only):** `container`, `postgres`, `object_storage`.

Each resource, where applicable: `type`, `name`, `sku`, `quantity`, `capacity_gb`, `public_access`.

**ProposedPlan (generator contract):** `region`, `environment` (`dev` | `test` | `prod`), `tags`, `resources` (min length 1).

**Generated plans must not contain:** `status`, cost, policy results, hashes, approval fields, or artifacts. Those exist only on `PlanRecord` after deterministic steps.

**Enums:**

- `ResourceType`: `container` | `postgres` | `object_storage`
- `PlanStatus`: `draft` | `evaluated` | `approved` | `rejected` | `artifact_generated`
- Policy result `status`: `passed` | `warning` | `error`

## Mock planner

Default planner is deterministic. It understands a small vocabulary:

- postgres / database
- container, web container, web application
- object storage
- dev, test, prod
- US East → `us-east-1`; US West → `us-west-2`; Azure East US → `eastus2`
- small, medium, low cost
- quantities one, two, three

If the prompt contains `SCENARIO:<name>`, skip vocabulary parsing and return a **fixed invalid or edge-case JSON string**. Never repair scenario payloads.

Required scenario names:

- `malformed`
- `missing_region`
- `missing_tags`
- `unknown_type`
- `unsupported_sku`
- `excessive_qty`
- `public_storage`

Happy-path example prompt: “A small PostgreSQL database and two web containers for a development team in US East, optimized for low cost.”

Happy-path mock output (illustrative): region `us-east-1`, environment `dev`, tags `environment`, `owner=dev-team`, `cost-center=engineering`, one `postgres` `db-small`, two `container` `container-small`. Tags are still verified by policy code; the mock must not “ensure compliance.”

## Policies

Policies are functions `ProposedPlan -> list[PolicyResult]`. They **report** violations and **never mutate** the proposed plan.

Each result includes:

- `policy_id`
- `status`: `passed` | `warning` | `error`
- `message`
- optional `resource_name`
- optional `field_path`

If a policy has no findings, emit one `passed` row for that `policy_id`.

Implement **only** these policies:

| policy_id | Rule | status on violation |
|---|---|---|
| `allowed_regions` | Region in `{us-east-1, us-west-2, eastus2}` | `error` |
| `required_tags` | Tags `environment`, `owner`, `cost-center` present and non-blank | `error` (per missing key) |
| `resource_limits` | Sum of container quantity ≤ 5; postgres quantity ≤ 2; object storage `capacity_gb` ≤ 500 | `error` |
| `dev_sku_tier` | If `environment==dev`, SKUs must be `*-small` or `*-medium` | `error` for `*-large` or other tiers |
| `storage_public` | `object_storage` with `public_access=true` | `error` |
| `dev_medium_cost` | If `environment==dev` and any `*-medium` SKU | `warning` (does not block) |

Unknown **type** is a schema failure (`ResourceType` enum), not a policy. Unknown **SKU** is a **pricing** fail-closed error. Do not add a `known_sku` policy.

Error-level results block approval. Warnings do not. Mixed AWS-style and Azure-style region names are intentional; do not remap `eastus2` to AWS in templates.

## Pricing

- Use `Decimal` for every unit price, line total, and monthly total.
- Construct `Decimal` values from **strings**, never from floats.
- Load prices from `app/data/prices.json` (synthetic only). Label all costs as **synthetic estimates**.
- Format monetary values to two decimal places.
- Price by `quantity` (and GB for storage). Never omit an unpriced resource and return a partial success.
- Unknown or missing type/SKU in the catalog: pricing error and **block approval**.

Synthetic catalog:

- `container-small`: `"18"` per instance
- `container-medium`: `"42"` per instance
- `db-small`: `"35"` per instance
- `db-medium`: `"95"` per instance
- `storage-standard`: `"0.025"` per GB

Happy-path cost: `2 * 18 + 35 = 71.00`. With 100 GB storage-standard: `73.50`.

Approval is blocked by schema errors, any policy `error`, or a pricing failure. The approve route enforces that through the service. `status == evaluated` alone does not mean approval will succeed. The plan response has no `approval_blocked` field.

## Canonical hashing

`canonical_hash(proposed)`:

1. `proposed.model_dump(mode="json")` — every proposed-plan field, including nested resources, tags, `public_access`, and `capacity_gb` (`null` when absent). No record id, status, timestamps, raw planner text, policies, or cost.
2. `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` — sort object keys only; keep resource list order.
3. Lowercase SHA-256 hex digest of the UTF-8 bytes.

Set `record.plan_hash = canonical_hash(proposed)` **once** at evaluation. Never overwrite it on approve, reject, or artifact generation.

Recomputing a hash is a **freshness check** for approval and artifact generation, not a chance to bless a mutated plan. Rejection does not compute a hash.

Use `hmac.compare_digest` for all hash equality checks.

On approval and artifact generation, if `record.proposed` is missing, treat `current_hash` as a mismatch.

## Approval, rejection, and state

`draft` may become `evaluated` or `rejected`. `evaluated` may become `approved` or `rejected`. `approved` may become `artifact_generated`. `rejected` is terminal for that record (new prompt → new `POST /v1/plans`).

| From | Allowed | Refused |
|---|---|---|
| `draft` | Reject | Approve, artifact |
| `evaluated` | Approve when every approval gate passes, or reject | Artifact |
| `approved` | Artifact when the current hash still matches | Approve again, reject |
| `rejected` | None | Approve, reject, artifact |
| `artifact_generated` | Read the stored record, including `artifact` | Approve again, reject, generate again |

Approval succeeds only when **all** of:

1. `record.status == evaluated`
2. `submitted_hash == record.plan_hash`
3. `current_hash == record.plan_hash`
4. `validation_errors` is empty
5. no policy result has `status=error`
6. pricing succeeded (`cost` present, no pricing error, monthly total present)

On approval, compute `current_hash = canonical_hash(record.proposed)` and **do not write it back**. Comparing only the client hash to the stored hash is **insufficient**. A mutated `proposed` with a stale stored hash must fail because `current_hash != record.plan_hash`.

Rejection requires the plan id only. It does not accept a submitted hash, compare hashes, or recompute `canonical_hash`. `draft` and `evaluated` may become `rejected`. Rejection preserves the proposal, `plan_hash`, validation errors, policy checks, and cost. It does not apply the approval gates. `approved`, `rejected`, and `artifact_generated` cannot be rejected. A rejected plan cannot be approved.

Invalid transitions return explicit business errors (HTTP **409** with a reason that names the failed gate). Missing records: **404**. Do not leak exception stack traces through the API.

## Artifacts

Generate Terraform-style HCL **only** after successful approval. A `draft`, `evaluated`, or `rejected` plan cannot generate an artifact.

Before generate: recompute `canonical_hash(record.proposed)` and require it equals `record.plan_hash`. Status must be `approved`. A plan that is already `artifact_generated` is refused; read the stored HCL with `GET /v1/plans/{plan_id}`. Do not write that recomputed hash back.

- Fictional `demo_*` resources only. Never `aws_*` or `azurerm_*`.
- Prominent comment: this is a prototype; **nothing was deployed**.
- Deterministic: sort resources and tags consistently; two renders of the same plan are identical.
- Escape all user-influenced strings in HCL.
- Never invoke `terraform init`, `plan`, or `apply`. Never call a cloud provider.

## API contracts

Base path `/v1`. JSON only. No authentication. Run one API worker. Handlers call the service synchronously and do not await during a transition. The in-memory store is not locked.

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/v1/plans` | `{ "prompt": str }` → mock generate, validate, evaluate if possible, persist. **201** `PlanRecord`, including drafts with validation errors. Unknown `SCENARIO:` names are **422** `{ "code": "unknown_scenario", "message" }` and are not stored. |
| `GET` | `/v1/plans/{plan_id}` | **200** `PlanRecord`, or **404** `{ "code": "not_found", "message" }`. |
| `POST` | `/v1/plans/{plan_id}/approve` | `{ "plan_hash": str }`. **200** updated `PlanRecord`. **409** `{ "code", "message" }` names the failed gate. The service checks hash format. |
| `POST` | `/v1/plans/{plan_id}/reject` | No body. The plan id is the only input. `draft` or `evaluated` → `rejected`, preserving the proposal, hash, and evaluation results. **200** updated `PlanRecord`. **409** for `approved`, `rejected`, or `artifact_generated`. |
| `POST` | `/v1/plans/{plan_id}/artifact` | No body. Approved plan whose current hash still matches → updated `PlanRecord` with the HCL in `artifact`. **409** when refused, including a rejected plan or a repeated generation. |
| `GET` | `/v1/schema/plan` | `ProposedPlan.model_json_schema()`. |
| `GET` | `/health` | `{ "status": "ok", "service": "prompt-to-provisioning-planner" }`. |

Plan routes return `PlanRecord`: `id`, `prompt`, `raw_output`, `status`, `proposed`, `plan_hash`, `validation_errors`, `policy_checks`, `cost`, `artifact`, `created_at`, `updated_at`. Include `raw_output` so the UI can show untrusted JSON. `artifact` is null until generation. Currency amounts are JSON strings from Pydantic, not floats. Request-body and UUID failures use FastAPI's **422** `detail` body. Service errors contain only `code` and `message`, with no stack trace.

## Streamlit

- Prompt text area, submit, display raw JSON, validated plan, policy table, synthetic cost.
- Approve sends the **stored** `plan_hash` from the last GET/POST response. Reject sends the plan id only.
- Disable approve when the record has validation errors, a policy `error`, or pricing that did not succeed.
- Generate artifact and download `main.tf` only after approval. The file body is the plan record's `artifact` field.
- Configure API base URL via environment variable `API_BASE_URL` (default `http://localhost:8000`; Compose: `API_BASE_URL=http://api:8000`).

## Testing

`pytest` + FastAPI `TestClient`. Inject an empty `InMemoryStore` via `dependency_overrides`. Tests do not require Docker.

Cover at least:

- Valid generated plan
- Malformed JSON
- Missing required fields
- Unexpected fields (`extra="forbid"`)
- Unsupported region
- Missing required tags
- Public object storage
- Excessive resource quantity
- Excessive storage capacity
- Unknown SKU
- Correct **Decimal** cost calculation
- Warning does not block approval
- Errors block approval
- Incorrect submitted hash
- Server-side mutation after evaluation (`proposed` changed, old `plan_hash` submitted → 409)
- Mutation after approval but before artifact generation
- Rejected plan cannot generate an artifact
- Deterministic artifact generation
- API happy path and failure responses

Also keep: health endpoint coverage.

Verify **behavior**, not private implementation details. Every defect fix must include a test that reproduces the defect.

Do not proceed past a failing test without explaining or correcting it. Do not claim tests passed unless they were actually executed.

## Docker and local run

Compose services: `api` (uvicorn `app.main:app --host 0.0.0.0 --port 8000`) and `ui` (Streamlit port 8501) with `API_BASE_URL=http://api:8000`.

Local without Docker:

```
python -m venv .venv
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
streamlit run ui/app.py
```

## Development workflow

After each implementation phase:

1. Review the files changed.
2. Run formatting or linting if configured.
3. Run the relevant tests.
4. Run the full suite when practical.
5. Report the commands executed and their results.
6. Do not claim tests passed unless they were executed.
7. Do not proceed past a failing test without explaining or correcting it.

If time is tight, cut Streamlit polish first, not tests or approval gates.

## Security and privacy

- No real cloud credentials. Do not read unrelated environment credentials.
- No paid APIs. No real cloud accounts. No infrastructure deployment.
- Do not log secrets. Do not expose exception stack traces through the API.
- Do not execute generated Terraform.
- Use only synthetic and mock data.

## Documentation

Keep `README.md` synchronized with the implementation.

The README must explain: AI trust boundary; architecture and request lifecycle; schema validation; policies; synthetic pricing; hash-bound approval; dry-run artifact generation; tests and failure scenarios; local and Docker execution; design tradeoffs and excluded production concerns; how Cursor was used, including representative prompts.

## Explicitly out of scope

Real LLM adapters, cloud SDKs, Terraform CLI, databases, auth, Kubernetes, queues, plan-editing UI, real `aws_*` / `azurerm_*` resources, Bicep, cost-optimization solvers, policy-as-code languages, durable persistence, webhooks, CI deploy pipelines.
