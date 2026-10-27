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

Every plan case requires nonblank `scenario` and `expected_outcome` fields separate
from `prompt`. `scenario` describes the user's situation, preconditions, input/state
and trigger; `expected_outcome` states observable success. `prompt` contains only the
executor's task wording.

Plan reviews include exactly one `case_contracts` entry per case with `case_id`,
`installed_source_unchanged`, `fixture_contract_consistent`,
`response_contract_consistent`, `trace_checks_observable`, `scenario_consistent`,
`requirement_trace_consistent`, `expected_outcome_consistent`,
`machine_checks_consistent` and a concrete `basis`. The last four flags assess the
scenario, linked requirement IDs and sources, expected outcome, and every check's
expected value as one traceable contract. Approval requires every flag to be true.
Missing, duplicate, unknown or contradictory case assessments are rejected before
the suite is frozen. Review executable helpers through their full assertion and
evidence-write path, including working-directory and relative/absolute path
semantics; a correct target must not fail a helper's contradictory assertion.

Repair input includes `failure_evidence`: bounded excerpts of actual development
execution artifacts with original byte counts, SHA256, source path/archive member,
and truncation or omission notices. Normal repair requests omit acceptance cases.
After a qualified candidate fails acceptance, a repair request may include only the
failed acceptance cases and evidence from those executed case directories. Archived
nested workflow plans can explain why an executed case failed; neighboring cases and
frozen plan files are never loaded as repair evidence.
`previous_unresolved` keeps remaining required work separate from standards
findings. Proven partial improvements can be retained, but unresolved work still
blocks final qualification and publishing a final candidate.

The repair request normally omits held-out acceptance cases. If a qualified
candidate fails final acceptance, the controller exposes only the failed cases
and their execution evidence to the next repair request. That patch must pass the
full development regression suite and a fresh run of the complete acceptance
suite before delivery; frozen cases and graders remain protected.

`--engine-config engine.json` accepts the native skill-up `engine` object. Its
custom local/http engine contract is upstream's SessionInput/SessionResult.
The `tests/fixtures/` implementations are deterministic integration test doubles;
they must not be represented as live LLM evidence.

# Output checks

Response checks: `contains`, `not_contains`, `regex`, `text_equal`, `json_equal`,
`json_sequence_equal` (array of expected values against an unwrapped JSON stream),
`json_path_equal` (RFC6901 `pointer`), `json_path_regex` (regex against the decoded string at
`pointer`), and `json_path_matches_file` (response `pointer`,
workspace-relative evidence `path`, and `value.file_pointer` to compare a dynamic response
value with the actual JSON evidence; `value.pointer` is accepted as a concise alias. File checks: `file_exists`, `file_contains`,
`file_json_equal`, `file_json_path_equal` (workspace-relative `path`, plus `pointer`
for a JSON field), and `file_text_equal` (exact UTF-8 content, preserving whitespace).
All specify `value`.

For exact `json_path_equal` checks on a `status` or `state` field, the expected enum
must appear in the case prompt or be defined by the documented Skill contract. A generated
`good_response` is only a checker control, not evidence that the target uses that label.
Implementation literals alone may describe a CLI result rather than the evaluated agent's response.
If a conversational task introduces its own JSON response schema, state any required enum
values in the task prompt.

`json_path_regex` requires the selected JSON pointer to resolve to a string. State that field's
string type in the task prompt before using it. If the expected value is an array (for example,
several clarification questions), prompt for that array shape and use an exact JSON check for a
fully specified array or a whole-response `regex` check for the required semantic content; do not
apply `json_path_regex` to the array itself. `good_response` is only a grading control and does
not define the Skill's output schema.

User-facing text oracles should cover ordinary paraphrases of the required meaning. For
conversational cases, include at least two distinct `good_responses` in addition to `good_response`;
the validator requires every positive control to pass every response check and the bad control to
contradict the requirement. This helps expose regexes that only accept one synthetic wording. For example,
`生成段数` can ask for a missing video quantity, and saying that the supplied reference URL will
not be used as a reference clearly describes an image-to-video fallback even when it does not repeat
the word “image.”

Do not invent a response format solely to simplify grading when the Skill contract asks for ordinary
conversation. If the contract leaves a collection shape open, accept reasonable string or array
forms and check semantic content across the complete response; reserve exact field types and array
contents for requirements that specify them. For command-line Skills, inspect each documented input
channel. If a request can arrive from both a file and stdin, use byte-level fixtures where needed to
check malformed encoding, documented exit results and rejection before external side effects. Do not
force strict decoding with `PYTHONIOENCODING`, `PYTHONUTF8` or locale overrides; that masks a target
defect. Remove inherited overrides when needed and test ordinary runtime defaults.

`tool_command_absent` applies a regex to actual tool-call commands and passes only when
no command matches. Supply `control_command` as a concrete matching example; plan validation
checks that the forbidden-command assertion rejects it. Use this for explicit no-submit gates,
alongside a substantive clarification or permission assertion. Like `tool_command`, it observes
direct commands only; helper-based API calls must be checked through the helper's captured request evidence.

`tool_call_absent` applies a regex to actual recorded tool-call names and passes only when no
name matches. Supply `control_tool_name` as a concrete matching example; plan validation checks
that the assertion rejects that positive control. Use a non-empty-name pattern such as `.+` when
the case requires zero tool calls of any kind, and state that restriction in the task prompt.
This observes the adapter's recorded tool-call trace; it does not prove that an allowed shell
command made no network request or that the host prevented unobserved side effects.

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
for grader controls. It pairs real calls/results by ID and checks final JSON equality
(or stripped text equality) with the tool's actual output. By default it requires a
successful result. Set `expected_exit_code` to a documented nonzero status when an
error response is the expected result; the observed exit code must match as well.
Do not set `expected_exit_code: 0`; a successful tool-result status already establishes
success. Some skill-up adapters preserve only success/failed in the judge transcript,
without the numeric exit code. When the exact nonzero code is required through such an
adapter, use a helper that invokes the installed script, records actual argv, exit code
and complete stdout, and emits that observation as a successful helper result; assert the
captured code and output from the helper evidence.
Use it when an Agent must not manually alter a script response. Controls prove that
a correct final answer with wrong tool output is rejected.

File existence alone and command execution alone are insufficient task-completion
checks. Prefer exact parsed values or verifiable content; use response controls. Make `bad_response`
contradict a required behavior, and ensure every entity-to-property relationship is tested together:
for example, an available alias must be paired with its own operation rather than merely mentioned
near a phrase about a different alias. If a negative control still passes, revise the oracles before
freezing the plan.
The output format is internal and compiled into native skill-up cases with script
judges. JSON-serialized configurations are valid YAML and preserve literal prompts.

