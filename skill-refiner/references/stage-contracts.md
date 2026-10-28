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

For refinement runs, `run --resume-from RUN_DIRECTORY` reopens the same run directory.
Before continuing, it checks the target and frozen-input manifests, controller/backend
contract, every completed stage handoff, and cached result hashes. Completed agent
responses and evaluations are keyed by their exact request or by the target manifest,
frozen cases, executor settings and grader hashes. They are loaded without invoking
the agent or evaluator again. The active incomplete operation uses a new attempt
directory when its prior output is partial; engine `ERROR` results are retried. If a
required checkpoint is damaged or its contract changed, resume stops and asks for a
new run rather than trusting or overwriting that evidence. `--reuse-plan` remains a
new-run path and must not be combined with `--resume-from`.

Use `run --plan-only` for the visibility checkpoint before tests. It freezes and
validates both plans, writes `work-plan.md` and `test-scenarios.md`, then returns `status=planned` without
executing baseline, regression or acceptance. Read the plan and continue the same
workspace with `run --resume-from RUN_DIRECTORY`; plan calls are reused. Creation uses
the same checkpoint through `create --plan-only` and resumes with `create --resume-run`.
`work-plan.md` is the frozen, human-readable design and test checklist. Execution
state belongs in `state.json`, `stages.json` and immutable stage evidence. The CLI
emits structured JSON stage/phase/evaluation events, controller Agent stream events,
and periodic heartbeats. Each controller call is bounded by the remaining run-wide
wall-clock budget and, when configured, the smaller role-profile timeout. CLI process
timeouts terminate the process tree; OpenAI-compatible requests use a killable worker
so slow-drip HTTP responses are also bounded. Timeout/cancellation evidence is saved
and the phase becomes an error rather than a pass. Completed stages remain reusable;
resume retries only incomplete work under the validated run contract. Per-case executor
timeouts and round/evaluation limits also bound target testing. When the development
plan has passed local controls, its independent review overlaps acceptance-plan
generation under `--agent-parallelism 2`; the gate waits for both. `--agent-parallelism=1`
selects the sequential fallback. Same-executor skill-up cases remain serialized
because the judge uses shared temporary directories. When
`large-task-tracking` owns the task, its parent snapshot/runtime is the sole user-visible
progress record; it reads these internal files and events without copying long logs.
Final verification remains bound to stage artifacts and hashes.

Creation runs use `create --resume-run RUN_DIRECTORY` to continue the same workspace.
The creation contract freezes business inputs, backends, controller code and case
limits. Completed analysis and draft stages are validated and reused. If refinement
was interrupted, its nested `refinement/` run resumes from its own checkpoint. A
different contract or changed completed artifact is rejected; higher time, call,
evaluation, planning or round budgets can be supplied when continuing.

The human-readable `work-plan.md` presents the design/requirement map, acceptance
criteria and frozen case checklist before execution. Each case shows its scenario,
expected outcome, linked requirement sources, machine checks and expected values,
and oracle rationale. `test-scenarios.md` contains the corresponding executable
development and acceptance scenario inventory. These files have no live statuses
and are written when the relevant plan is frozen. The parent task manager reports progress
from structured events and internal checkpoints. One full regression run per repair
round includes affected and previously passing development cases, so there is no extra
focused pass before it.


Controller calls use `skill-refiner.agent-request/v1` JSON with `role`, `instruction`,
`input`, and `response_schema`. Each call directory also contains
`request-manifest.json` (`skill-refiner.agent-request-manifest/v1`) with the request
hash, exact transmitted UTF-8 byte count and hash, configured limit, outcome, and
response hash when one was returned, plus relative paths to its `request.json`,
`transmission.txt` and (on success) `response.json`. `transmission.txt` preserves the exact bytes
sent to the backend (the JSON request for command adapters, the rendered prompt for
managed model adapters). Structured outputs are validated against the role's JSON
schema before the next stage can consume them. CLI Agent events are forwarded as
structured `agent_stream` records while the call remains active; a terminal JSON
result is saved before the next role or stage starts. When a backend does not expose
partial output, it emits elapsed-time heartbeats and returns its final response. The
controller does not infer completion from a short wait or heartbeat count. Human-readable
reports use Markdown; patches use unified diff; logs and backend-native evidence retain
their source format.

The default maximum serialized prompt is 350,000 UTF-8 bytes. A controller role's
backend profile may set `max_request_bytes` from 16,384 to 10,000,000, based on a
verified limit for that backend/model. This is a byte guard, not a token counter or a
claim about a provider's exact tokenizer. A request over the limit is saved and
rejected before transmission. Reduce the stage handoff or set a tested limit; do not
remove required inputs, tests, or evidence just to fit. Full source and execution
evidence remain in the run directory even when a role receives a bounded excerpt.

## Human-readable phase plan and progress

The generated work-plan.md is the user-facing execution map, not only a list of test
cases. It must contain a numbered ordered phase sequence. Each step states its goal,
actions, required inputs, outputs/evidence, conclusion required to advance, and the
failure or stop action. Creation plans include business analysis and first-draft gates
before the shared refinement sequence; refinement plans begin with source and
requirement mapping. Both then show plan freezing, plan self-check and independent
review, the plan gate, baseline, bounded repair with full regression, independent
acceptance, and final verification/delivery.

For business workflow analysis, each user-facing workflow step also has an observable
completion_check. Render it beside the step's inputs, outputs, failure behavior and
requirement links so a reader can tell what success means.

Presenting the plan is a visibility checkpoint, not routine approval. Unless the user
explicitly requests plan-only, proceed autonomously in the same run. Pause for a
material unanswered decision, required permission/local action, explicit user stop,
failed gate, unavailable dependency, or exhausted configured budget. Do not reset the
budget, repeatedly run unchanged tests, or abandon a resumable stage. Resume the same
run and reuse completed evidence while hashes and contracts match. A changed frozen
input or controller contract requires preserving the old evidence and using a new run.

At each meaningful transition, report the step number/name, live status, concise
conclusion, evidence path, and next step. Populate these from structured events and
state.json/stages.json. The static plan states expected conclusions; it must not be
presented as evidence that execution already passed.

When planning, self-check or independent review ends in an error or cancellation, close
any pending/running plan gate as not_passed and persist the cause. Keep target
skill_evaluation at not_started. A terminal planning error must never leave the gate
displayed as running.

## Existing creation and refinement stages

| Stage | Required input artifacts | Output artifacts and formats | Gate before handoff |
|---|---|---|---|
| Creation: inputs | User description, supplied requirements, auto-discovered project standards, and optional additional standards | `inputs.json` (JSON), `inputs/*.md` (UTF-8 Markdown/text) | Inputs frozen and hashed in the business contract |
| Creation: analysis | Frozen business inputs | `analysis.json`, `analysis-review.json` (JSON), `business-analysis.md` (Markdown), `business.lock.json` (JSON) | Requirements cite user sources; workflow and normal/boundary/error applicability reviewed; material questions pause creation |
| Creation: first draft | Approved analysis and business lock | `drafts/` (Skill source files), `draft-manifest.json` (path-to-SHA-256 JSON) | Draft paths and static authoring checks pass |
| Refinement: planning | Immutable Skill snapshot, requirements, auto-discovered project standards, additional standards, analysis contract and the actual plan schema | `test-plan.json`, `acceptance-plan.json`, `plan-controls/{development,acceptance}.json`, `plan-reviews/{development,acceptance}.json`, `oracle.lock.json` (JSON) | Schema v2 validates every required field and enum; full positive/negative/auxiliary controls run; assets and requirement sources are audited; each suite has an independent review; only then can the plan gate pass |
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
