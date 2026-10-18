# Agent protocol

For managed Codex, Cursor, native subagent and OpenAI-compatible backends, use
`--config` and read [backend configuration](backends.md). The legacy flags below
remain supported for single-engine runs and cannot be mixed with `--config`.

`--agent-command` accepts a command expressed as a shell-style argument list.
The implementation executes the resulting argv directly, without a shell.
Use absolute paths for script arguments because each role has an empty working
directory and a fresh process.

The process receives one JSON object on stdin:

```json
{"role":"plan","instruction":"...","input":{},"response_schema":{}}
```

Return exactly one JSON object on stdout, conforming to `response_schema`.
Send diagnostics to stderr. Creation adds `analyze`, `analysis-review`, and `draft`
roles (schemas in `scripts/refiner/creation.py`); see [creation](creation.md).
Refinement roles are `plan`, `acceptance-plan`, `plan-review`,
`standards-review` and `repair`. Schemas are in `scripts/refiner/schemas.py`.
This is an adapter interface, not an offline substitute for actual LLM planning.

Plan reviews include `case_contracts`, one entry per case with `case_id`,
`installed_source_unchanged`, `fixture_contract_consistent`,
`response_contract_consistent`, `trace_checks_observable` and a concrete `basis`.
Approval requires every entry to agree with installed-source immutability and the
actual fixture instructions. The controller rejects incomplete or contradictory
approvals before freezing the suite.

Repair input includes `failure_evidence`: bounded excerpts of actual development
execution artifacts with original byte counts, SHA256, source path/archive member,
and truncation or omission notices. It never loads the controller's held-out suite.
Archived nested workflow plans can explain why a development case failed; those
are evidence from that executed case, not the controller's acceptance tests.
`previous_unresolved` keeps remaining required work separate from standards
findings. Proven partial improvements can be retained, but unresolved work still
blocks final qualification and publishing a final candidate.

`--engine-config engine.json` accepts the native skill-up `engine` object. Its
custom local/http engine contract is upstream's SessionInput/SessionResult.
The `tests/fixtures/` implementations are deterministic integration test doubles;
they must not be represented as live LLM evidence.

# Output checks

Response checks: `contains`, `not_contains`, `regex`, `text_equal`, `json_equal`,
`json_sequence_equal` (array of expected values against an unwrapped JSON stream),
`json_path_equal` (RFC6901 `pointer`). File checks: `file_exists`, `file_contains`,
`file_json_equal`, `file_json_path_equal` (workspace-relative `path`, plus `pointer`
for a JSON field), and `file_text_equal` (exact UTF-8 content, preserving whitespace).
All specify `value`.

Cases can request `helpers: ["python_probe"]`. The controller materializes the
frozen `refiner-python-probe.py` fixture, which cannot be replaced by `files`.
See [creation](creation.md) for its actual execution evidence and observation scope.

`tool_command` matches a regex against `command` arguments in actual `tool_call`
transcript records from skill-up. Missing transcript fails closed. It does not
match instructions or an assistant's claim that a command ran.
Matching recognizes shell list separators and equivalent absolute, relative and
`./` paths only for existing files inside that actual case workspace. Raw commands
and outputs remain unchanged in evidence. Set `literal_command: true` when the
contract requires literal command/path spelling rather than the same file.

`tool_output_matches` uses the same command regex plus a `control_command` literal
for grader controls. It pairs real calls/results by ID, requires a successful tool
result and checks final JSON equality (or stripped text equality) with the tool's
actual output. Use it when an Agent must not manually correct a defective script's
answer. Controls prove that a correct final answer with wrong tool output is rejected.

File existence alone and command execution alone are insufficient task-completion
checks. Prefer exact parsed values or verifiable content; use response controls.
The output format is internal and compiled into native skill-up cases with script
judges. JSON-serialized configurations are valid YAML and preserve literal prompts.

