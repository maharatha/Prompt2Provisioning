# Prompt-to-Provisioning Planner — Cursor Prompt History

> These are condensed versions of the prompts mapped to modules, not verbatim transcripts. Exact prompts from the earlier linked chat could not be retrieved; those entries are explicitly marked. This record covers the mock-planner assessment build.

## Development approach

The prototype was developed incrementally using Cursor. Each implementation prompt had a bounded scope, required actual test execution, and prohibited starting later phases.

**AI proposes; deterministic software controls.**

The planner returns untrusted raw JSON. Application code owns schema validation, policies, pricing, hashing, approval, state transitions, and artifact generation.

## Prompt-to-module mapping

| # | Prompt / phase | Modules or files | Outcome |
|---|---|---|---|
| 1 | Initial architecture planning and revision | Cursor architecture plan | Design established; exact earlier prompt unavailable |
| 2 | Repository instructions | `AGENTS.md` | Scope and contracts established; exact earlier prompt unavailable |
| 3 | FastAPI scaffold | `app/main.py`, `tests/test_health.py` | Startup and health; exact earlier prompt unavailable |
| 4 | Models and persistence | `app/models.py`, `app/store.py`, tests | Models and deep-copy storage; exact earlier prompt unavailable |
| 5 | Review models/store | Models, store, tests | Strict-integer and naming issues identified |
| 6 | Fix review findings | Models, store, tests | Strict integers and consistent names; 34 tests passed |
| 7 | Architecture documentation | `ARCHITECTURE.md` | Boundaries, states, and safeguards documented |
| 8 | Mock planner | `app/planner.py`, `tests/test_planner.py` | Vocabulary and negative scenarios; 86 tests passed |
| 9 | Schema validation | `app/validation.py`, `tests/test_validation.py` | Complete proposal or errors; 117 tests passed |
| 10 | Policies | `app/policies.py`, `tests/test_policies.py` | Passed/warning/error checks; 203 tests passed |
| 11 | Pricing | `app/pricing.py`, `app/data/prices.json`, pricing tests | Decimal and fail-closed estimates; 227 tests passed |
| 12 | Hashing | `app/hashing.py`, `tests/test_hashing.py` | Canonical JSON/SHA-256; 243 tests passed |
| 13 | Creation/evaluation/retrieval | `app/services.py`, `tests/test_services.py` | Components connected; 259 tests passed |
| 14 | Approval/rejection | `app/services.py`, `tests/test_approval.py` | Hash-bound transitions; 286 tests passed |
| 15 | Rejection documentation | `AGENTS.md`, `ARCHITECTURE.md` | Draft/Evaluated rejection without hash |
| 16 | HCL renderer | `app/artifacts.py`, `app/templates/main.tf.j2`, artifact tests | Deterministic escaped HCL; 296 tests passed |
| 17 | Artifact workflow | `app/services.py`, `tests/test_artifact_generation.py` | Hash recheck before rendering; 312 tests passed |
| 18 | API endpoints | `app/main.py`, `app/planner.py`, `tests/test_api.py` | HTTP workflow; 359 tests passed |
| 19 | Streamlit | `ui/app.py`, `tests/test_ui.py` | HTTP-only review/download UI; 387 tests passed |
| 20 | Docker | Dockerfiles, Compose, ignore file, README | Eight live smoke checks passed |
| 21 | Final readiness review | Tests and README | Provided; completion not confirmed in this conversation |

Test counts are taken from Cursor execution reports shared during development.

## Implementation prompts

### 1–4. Earlier-chat foundation

The earlier conversation established these stages:

1. Generate and revise the architecture plan.
2. Create `AGENTS.md`.
3. Scaffold FastAPI and `/health`.
4. Implement Pydantic models and the in-memory store.

The exact original prompt text was not recovered. These descriptions record known scope rather than reconstructing historical wording.

### 5. Review models and store

```text
Review the completed Pydantic models and in-memory store phase.
Do not implement the next phase or modify files.

Read AGENTS.md, models, store, and tests. Check rejection of extra
fields, strict integer quantities/capacities, resource-specific rules,
workflow states, and store reference/copy behavior.

Run the existing tests. Report the exact command, actual output,
exit code, and findings with file references.
```

### 6. Fix model/store findings

```text
Fix only the Pydantic models and in-memory store findings.

Make quantity and capacity_gb strict integers, rejecting booleans,
floats, and numeric strings. Preserve existing ranges and capacity rules.

Resolve name, plan_hash, and store-class naming mismatches against
AGENTS.md with minimal changes. Do not add hashing or approval logic.

Add behavior tests and run the complete suite. Report actual output
and unresolved issues. Do not begin the planner phase.
```

### 7. Document the architecture

```text
Create a concise ARCHITECTURE.md. Documentation only; no application
changes or new implementation phase.

Read existing instructions, the architecture plan, and repository.
Distinguish implemented from planned components.

Document scope, technology choices, trust boundary, module
responsibilities, evaluation flow, states, hash-bound approval,
schema/policy/pricing separation, Decimal pricing, deterministic
HCL escaping, API/UI boundaries, storage limitations, and tests.

Include a compact Mermaid diagram. Run the baseline tests.
```

### 8. Implement the mock planner

```text
Implement only the deterministic mock planner and behavior tests.

Accept a prompt and return raw JSON. Do not validate, enforce policies,
calculate cost, hash, approve, access the store, or generate artifacts.

Support the limited resource, region, environment, size, and quantity
vocabulary. Bind quantities to their resource phrases. Document defaults
and ambiguity handling. Produce stable names and deterministic ordering.

Support malformed_json, missing_region, missing_tags,
unknown_resource_type, unsupported_sku, excessive_quantity, and
public_object_storage scenarios. Preserve their defects without repair.

Test the assignment example, mappings, quantities, defaults,
determinism, and every negative scenario. Run pytest.
```

### 9. Implement schema validation

```text
Implement only schema validation and its tests.

Use ProposedPlan as the authoritative schema. Accept raw planner JSON
and return either a complete validated proposal with no errors or no
proposal with structured errors.

Malformed JSON is an expected failure. Never repair input or return a
partial plan. Report field paths, error codes, and understandable messages.

Keep schema-valid policy/pricing defects available for later checks.
Reject unknown types, unexpected fields, missing required fields, and
invalid field types.

Test normal and negative paths. Run pytest.
```

### 10. Implement policies

```text
Implement only infrastructure policy checks and behavior tests.

Accept a validated proposal and return deterministic PolicyCheck results
without changing it.

Check allowed regions, required nonblank tags, aggregate container and
database quantities, aggregate storage capacity multiplied by quantity,
dev type-specific small/medium SKUs, and private object storage.

Dev medium SKUs produce warnings. Use exact SKU membership.
Do not implement pricing or a policy framework.

Test boundaries, aggregate bypass attempts, multiple violations,
warnings, determinism, and unchanged input. Run pytest.
```

### 11. Implement pricing

```text
Implement only synthetic pricing, the local price catalog, and tests.

Store rates as strings and construct Decimal values from strings.
Containers/databases cost rate × quantity.
Storage costs rate × capacity_gb × quantity.

Return a complete estimate only when every resource is priced.
Unknown SKUs, type/SKU mismatches, missing capacity, and invalid catalog
rates fail closed with errors and no partial estimate.

Load the catalog relative to the module. Preserve exact arithmetic;
do not round individual lines to cents.

Test exact totals, fractional-cent precision, failures, determinism,
and unchanged input. Run pytest.
```

### 12. Implement canonical hashing

```text
Implement only canonical serialization and SHA-256 hashing.

Serialize the full ProposedPlan.model_dump(mode="json") with sorted keys,
compact separators, ensure_ascii=False, and allow_nan=False.

Hash UTF-8 bytes and return lowercase hexadecimal SHA-256.
Preserve resource-list order and include null fields.
Exclude record metadata, evaluation results, and raw planner output.

Test insertion-order independence, field changes, list-order changes,
Unicode, expected digest, and unchanged input. Run pytest.
```

### 13. Connect creation and evaluation

```text
Implement only plan creation, evaluation, and retrieval in PlanService.

Call the planner once and retain its exact output.
Schema failures persist as Draft with errors and no validated proposal,
hash, policies, or cost.

Valid proposals receive policy checks, pricing, and their canonical hash,
then persist as Evaluated—even when policy or pricing errors exist.

Use the existing store directly and preserve deep-copy behavior.
Do not expose re-evaluation or hash-refresh operations.

Test the happy path, negative paths, raw output, distinct IDs, retrieval,
copy isolation, and stored hashes. Run pytest.
```

### 14. Implement approval and rejection

```text
Implement only approval/rejection and behavior tests.

Approval requires Evaluated status, proposal/hash presence, submitted
hash matching the stored hash, recomputed proposal hash matching the
stored hash, no validation/policy errors, and successful pricing.

Use hmac.compare_digest for both comparisons. Handle malformed and
non-ASCII hashes cleanly. Warnings do not block approval.

Never refresh the stored hash, re-evaluate, or repair during approval.
Failures leave stored data unchanged.

Allow Draft/Evaluated → Rejected. Preserve evaluation data.
Refuse later approval of rejected plans and repeated terminal transitions.

Test all gates, persisted mutation, failures, transitions, and missing IDs.
Run pytest.
```

### 15. Reconcile rejection documentation

```text
Update only AGENTS.md and ARCHITECTURE.md.

Document Draft/Evaluated → Rejected using the plan ID without a hash.
Preserve proposal, hash, and evaluation results.

Approved, Rejected, and ArtifactGenerated cannot be rejected.
Rejected plans cannot approve or generate artifacts.
Keep approval safeguards unchanged.

Remove contradictions and update transitions and the reject API contract.
Run pytest.
```

### 16. Implement the HCL renderer

```text
Implement only the Jinja2 Terraform-style renderer and tests.

Use fictional demo_container, demo_postgres, and demo_object_storage.
Include a fixed prototype/dry-run/nothing-deployed header.

Sort resources using every field and tags by key without mutating input.
Use generated unique labels and count for quantity.
Load templates relative to the module and use StrictUndefined.

Safely quote user strings, including quotes, backslashes, control
characters, Unicode, and Terraform dollar-brace / percent-brace sequences.
Never insert user text into raw expressions, labels, types, or comments.

Test fields, determinism, ordering, duplicate names, escaping, injection,
and unchanged input. Never execute Terraform. Run pytest.
```

### 17. Connect approved artifact generation

```text
Implement only PlanService.generate_artifact and behavior tests.

Require Approved status, proposal/hash presence, a fresh hash comparison,
no validation/policy errors, and successful pricing.

Render first; only then persist artifact and ArtifactGenerated status.
Never refresh the hash or modify the proposal.
Failures leave the record unchanged. Repeat generation is refused.

Test approved generation, persisted mutation after approval, missing
fields, inconsistent results, renderer failure, rejected plans, and
artifact retrieval. Run pytest.
```

### 18. Implement API routes

```text
Implement only FastAPI plan endpoints and API tests.

Expose create, retrieve, approve, reject, artifact, schema, and health.
Delegate workflow decisions to PlanService.

Use strict request models that forbid extra fields, nonblank prompts,
UUID paths, a shared service/store, and isolated test dependencies.

Map service errors consistently. Invalid planner JSON creates a Draft
with HTTP 201. Serialize money as strings.

Use async handlers with synchronous service calls and no awaits during
transitions; document one API worker. Unknown scenarios return a specific
422 error.

Test the full workflow, errors, mutations, warnings, rejection,
serialization, request validation, schema, and health. Run pytest.
```

### 19. Implement Streamlit

```text
Implement only the Streamlit HTTP client and focused UI tests.

Do not import backend internals. Use API_BASE_URL and explicit HTTP
timeouts. Do not automatically retry mutation requests.

Display prompts, raw output, proposal, errors, policies, cost, status,
artifact, and download.

Approve sends the displayed hash and is disabled for blocking results.
Reject is available only for Draft/Evaluated; artifact generation only
for Approved.

Replace session state only after successful responses. Preserve current
records on failures and clear old artifacts when a new plan succeeds.

Parse money with Decimal. Failed pricing displays Cost unavailable.

Test controls, requests, failures, replacement, and artifact display.
Run pytest and compileall ui.
```

### 20. Implement Docker packaging

```text
Implement only Dockerfiles, Docker Compose, .dockerignore, and run docs.

Use Python 3.12 slim and runtime dependencies.
Run one API worker and one Streamlit service.
Include catalog/templates. Set UI API_BASE_URL=http://api:8000.

Bind host ports to localhost. Add API health checking and UI dependency
on a healthy API. No extra services, credentials, or persistence volumes.

Run pytest, Compose configuration, build, startup, health checks, and
live smoke tests for creation, approval, artifact retrieval, malformed
JSON, public-storage blocking, and Streamlit health.

Stop the stack afterward. Report actual failures and results.
```

### 21. Final readiness review — provided, execution unconfirmed

```text
Perform the final assessment-readiness review without adding features.

Map assignment requirements to implementation and tests.
Check scope exclusions and README accuracy.
Run pytest and fill only genuine missing negative-scenario API coverage.

If browser control exists, verify the live UI happy path, warning,
failures, rejection, replacement, and download.
Otherwise mark manual browser verification pending.

Add a short README demo script. Report coverage, actual results,
browser status, changed files, and remaining blockers.
```

## Additional review and diagnostic prompts

| Prompt | Modules | Purpose / status |
|---|---|---|
| Planner checkpoint review | Planner and tests | Verify regions, capacity/count separation, scenarios, and trust boundary; completed |
| Revised review without file dumps | Planner and tests | Concise review with actual test output; completed |
| Disabled Generate Artifact diagnosis | UI and tests | Inspect approval/session state/reruns; provided, execution unconfirmed |

### Planner checkpoint review

```text
Review the current mock planner. Do not modify files or begin another phase.
Read AGENTS.md, planner, and tests, but do not print full files.

Run pytest and report actual output and exit code.
Verify explicit region mappings, storage capacity versus quantity,
separate database/container counts, and negative scenarios without repair.
Confirm the planner returns raw JSON and performs no deterministic controls.
Report tested versus inspected behavior and documentation mismatches.
```

### Disabled Generate Artifact diagnosis

```text
Diagnose and fix only the disabled Generate Artifact control.

Read instructions, UI, and relevant tests. Preserve backend safeguards.
Verify approval sends the displayed hash, succeeds, and replaces session state.
Check for stale variables and reruns after successful actions.
Show API failures instead of ignoring them.

Make the smallest fix and test Create → Approve → enabled Generate Artifact
→ displayed artifact. Run pytest and report root cause and actual results.
```

This diagnostic was an option. Subsequent discussion indicated that approval was required before artifact generation; no confirmed UI fix was reported.

## Verification record

- Latest reported full suite: **387 passing tests**.
- Docker build and startup verified after local engine/port issues were resolved.
- Live container smoke tests: **eight checks passed**.
- Critical pricing, approval, and renderer implementations received source review.
- Final readiness-review completion was not reported in this conversation.
- Commit completion was not tracked consistently.

## Scope and provenance

This history covers the mock-planner assessment build documented in the conversation. Later extensions, including real model-provider integration or real cloud Terraform export, require their own entries.

Earlier-chat reference: https://chatgpt.com/c/6ab14668-db18-83e9-8e41-b4528420df9e

The earlier chat's exact foundation prompts were unavailable. All prompt blocks above are condensed summaries, not claims of verbatim recovery.

