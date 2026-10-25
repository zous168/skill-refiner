# Stage contracts and handoff artifacts

The controller runs a requirement-driven sequence of small stages. A stage receives
only the contract and source material it needs, emits a typed artifact, and hands the
next stage file references with SHA-256 identities instead of replaying the full chat
history. Tests and standards findings stay frozen while repair proceeds. If one stage
still needs more context than its configured budget, the run stops with the full
request and a diagnostic manifest; it never silently truncates requirements or tests.

## Artifact formats

Every run writes `stages.json` using the `skill-refiner.stages/v1` contract. Each
stage declares its input and output paths, media types, current status, and discovered
artifacts, a named `validation_gate` tied to the implementing validator/controller
function, and the applicable `failure_states`. Existing artifacts are recorded with their UTF-8/file byte count and
SHA-256. Glob entries describe a set of files, such as regression output from each
repair round. Status is one of `pending`, `running`, `completed`, `blocked`, or
`skipped`. `stages.json` is an index and audit record; the referenced files remain
the canonical stage data. A stage cannot become `completed` until its required inputs
and outputs exist. The final verification gate checks the versioned stage sequence,
required artifacts and recorded input/output hashes; deleting or changing a handoff
after a stage completes invalidates verification. Reused plans, drafts and their call
transcripts may be optional in the new run, but present evidence is still hashed and
checked.

Controller calls use `skill-refiner.agent-request/v1` JSON with `role`, `instruction`,
`input`, and `response_schema`. Each call directory also contains
`request-manifest.json` (`skill-refiner.agent-request-manifest/v1`) with the request
hash, exact transmitted UTF-8 byte count and hash, configured limit, outcome, and
response hash when one was returned, plus relative paths to its `request.json`,
`transmission.txt` and (on success) `response.json`. `transmission.txt` preserves the exact bytes
sent to the backend (the JSON request for command adapters, the rendered prompt for
managed model adapters). Structured outputs are validated against the role's JSON
schema before the next stage can consume them. Human-readable reports use Markdown;
patches use unified diff; logs and backend-native evidence retain their source format.

The default maximum serialized prompt is 350,000 UTF-8 bytes. A controller role's
backend profile may set `max_request_bytes` from 16,384 to 10,000,000, based on a
verified limit for that backend/model. This is a byte guard, not a token counter or a
claim about a provider's exact tokenizer. A request over the limit is saved and
rejected before transmission. Reduce the stage handoff or set a tested limit; do not
remove required inputs, tests, or evidence just to fit. Full source and execution
evidence remain in the run directory even when a role receives a bounded excerpt.

## Existing creation and refinement stages

| Stage | Required input artifacts | Output artifacts and formats | Gate before handoff |
|---|---|---|---|
| Creation: inputs | User description, supplied requirements, auto-discovered project standards, and optional additional standards | `inputs.json` (JSON), `inputs/*.md` (UTF-8 Markdown/text) | Inputs frozen and hashed in the business contract |
| Creation: analysis | Frozen business inputs | `analysis.json`, `analysis-review.json` (JSON), `business-analysis.md` (Markdown), `business.lock.json` (JSON) | Requirements cite user sources; workflow and normal/boundary/error applicability reviewed; material questions pause creation |
| Creation: first draft | Approved analysis and business lock | `drafts/` (Skill source files), `draft-manifest.json` (path-to-SHA-256 JSON) | Draft paths and static authoring checks pass |
| Refinement: planning | Immutable Skill snapshot, requirements, auto-discovered project standards, additional standards, analysis contract | `test-plan.json`, `acceptance-plan.json`, `oracle.lock.json` (JSON) | Independent review approves executable coverage; acceptance uses fresh fixtures; plans and source hashes lock |
| Refinement: baseline | Locked development plan and original Skill snapshot | `baseline/summary.json`, `baseline/standards.json` (JSON), backend logs/evidence | All configured engines return observed results; environment errors stop repair |
| Repair | Failed-case results (including immutable per-attempt acceptance summaries after held-out failure), material standards findings, frozen requirements, prioritized source excerpts | `iterations/round-N/changes.json` (JSON), candidate Skill files, call request/response manifests | Each changed file cites a failed case or finding; protected tests and oracles cannot be edited |
| Regression | Candidate, same frozen development suite, original passing-case set | `iterations/round-N/regression/summary.json`, `standards.json`, engine comparisons (JSON/Markdown) | No previously passing case regresses; each engine is judged separately |
| Acceptance | Qualified candidate and separately frozen acceptance plan | `acceptance/target.json` (candidate path and hashes), `acceptance/summary.json`, immutable `acceptance/attempts/<attempt>/evaluation/summary.json` records and raw engine evidence | Every configured executor passes the independent cases against the delivered candidate |
| Delivery | Passing regression, acceptance, locked plans and source manifests | `final/<skill>/`, `final/evidence.json` (JSON), `final/changes.patch` (unified diff) | Candidate matches the acceptance target and evidence |
| Verification | Final evidence (plus the accepted child run for creation) | `verification.json` (JSON), final `report.md` (Markdown) | Hashes, frozen plans, all engines, stage contracts and final artifacts satisfy the completion gate |

The creation pipeline invokes the same refinement stages for the first draft, so a
new Skill receives the same executable planning, baseline, repair, regression and
independent acceptance gates as an existing Skill.

## Context and extension rules

Add a new stage only when it has a distinct responsibility or independent gate.
Define its stable ID, accepted input artifact types, output schema/media type,
validation rule, failure states, and provenance fields before adding the backend call.
Keep machine-readable contracts in JSON so validators and new engines can consume
them; keep Markdown as a generated view for people. Preserve raw evidence separately
from summaries so future graders can be added without rerunning old stages.

Do not pass prior conversation transcripts as implicit state. Pass a concise current
contract, source artifact references with hashes, and the minimum relevant excerpts.
For repair, the frozen requirements and cited findings are the governing contract;
large requirement/standard documents are recorded by source, size and hash rather
than copied wholesale into every repair request. The bounded source inventory
prioritizes `SKILL.md`, cited findings and local Markdown references. If an omitted
file is necessary, split the work into a smaller explicit stage or raise the budget
only after checking the configured engine's context limits. Omit acceptance cases
from repair until a qualified candidate fails acceptance; then expose only the failed
cases and their raw evidence, and rerun the complete suite after regression.
