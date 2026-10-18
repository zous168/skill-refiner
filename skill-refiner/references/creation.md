# Business request to verified Skill

`create` accepts a natural-language description, repeatable independent `--spec`
files, optional `--standard` files and optional `--name`. A name is otherwise chosen
during business analysis. From a source checkout:

```sh
.venv/bin/skill-refiner create 'Create a local business workflow with these rules ...' \
  --config /absolute/path/to/backends.json --output /absolute/path/to/new-workspace
```

In an installed Skill, use `scripts/launch.py create` to select its saved runtime
and matrix. Save prose containing shell syntax as a UTF-8 file and pass `--spec`
instead of interpolating it into executable shell text.

## Actual stages

1. Save original business inputs and standards. Analyze inputs, outputs, workflow
   order, decisions, failure behavior, requirements, dependencies and assumptions.
   Assess normal, boundary and error scenarios and map required behavior to them.
2. Independently review grounding against the original request. Reject invented
   material rules, omissions and unjustified category exclusions. Allow one bounded
   analysis revision. Material unanswered business decisions stop with questions.
3. Freeze the approved analysis and original inputs by hash. Create actual Skill
   instructions, references and working implementations. Reject unsafe/protected
   paths. Static errors allow one bounded regeneration. No author sees held-out tests.
4. Reuse the real refinement pipeline: independent test planning/review, original
   baseline, standards review, failure-driven repair, focused and full regression,
   independent acceptance. Both frozen suites must preserve approved requirement
   IDs/properties and cover every applicable normal/boundary/error category.
5. Publish only after all configured executors pass, optionally to a new
   `--destination`. Preserve drafts, rejected attempts, original inputs and results.

`agents.planning` handles analysis and tests, `agents.review` handles business/test/
standards reviews, `agents.creation` writes the first implementation (falling back
to repair, then default, then Codex), and `agents.repair` fixes observed failures.
They are independently configurable. Role-call and wall-clock budgets cover the
entire creation plus refinement workflow; no budget resets between stages.
Create defaults allow up to four plan/review attempts per suite, eight development
cases and six acceptance cases. `--planning-attempts`, `--max-cases` and
`--acceptance-cases` override these caps. Revisions receive the prior plan and
accumulated concrete review issues; rejected versions are preserved.

## Outputs and stops

- `analysis.json`, `business-analysis.md`, `business.lock.json`: business contract,
  workflow, scenario intents and input hash. Questions are saved before approval.
- `drafts/` and `draft-manifest.json`: preserved first implementations and findings.
- `refinement/`: frozen suites, per-engine results, repairs, traces and evidence.
  Use this subdirectory as a future `compare --suite` source.
- `test-scenarios.md`: actual development/acceptance prompts, input fixtures,
  independent checks and requirement mappings, with each scenario category.
- `final/<name>/`, `final/creation.patch`, `final/changes.patch`, `final/evidence.json`:
  accepted Skill, complete creation diff, draft-to-final repair diff and evidence.
  The repair diff can be empty if the first implementation already passes.

Exit 0 with `created` means the full workflow passed. `refinement_status` accurately
records `passed` or `unchanged`. Exit 2 means clarification, rejection, coverage gap,
execution error, exhausted budget or nonconvergence. Incomplete runs retain drafts
but do not publish a final Skill or populate the destination. Never relabel them.

`create --resume-from PREVIOUS_CREATION --output NEW_WORKSPACE` validates and reuses
its frozen approved business contract and original validated draft. If complete
frozen suites exist, their source/requirement hashes are checked before reuse.
Every baseline and candidate verdict is executed again. Source changes, changed
business inputs, missing approvals or tampered artifacts are rejected. Reports
record original artifact provenance and, when replaying after a controller upgrade,
the original checker hash alongside the new run's checker hash. Old verdicts and
locks remain untouched. A resumed invocation has its own explicit budget.

Category coverage is not exhaustive scenario coverage. Current executable checks
cover deterministic text/JSON, files and command traces. External services, visual
output and long-running stateful systems need real accessible task material or
additional judging support. An assumption does not verify a service, and fake
provider tests do not establish a real integration pass.

For Python scripts, the planner can request the controller-supplied frozen
`python_probe` helper. It actually executes the installed script and exports
stdout, business exit status, tracked input hashes and Python audit events.
Optional flags record and block tracked-input writes, sockets and subprocesses.
`file_json_path_equal` grades these evidence fields; `file_text_equal` verifies
exact UTF-8 content. This is Python-level observation, not OS isolation or a
guarantee about arbitrary native code. The observer's transport exit code is not
the business script's exit code. Its code hash is frozen with the suite.
For batches, `json_sequence_equal` compares independent expected JSON values with
the actual output stream while ignoring member order and whitespace. Separate
stdout checks still require forwarding actual complete outputs unchanged.
