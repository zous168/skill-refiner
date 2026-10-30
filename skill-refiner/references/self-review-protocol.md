# Bounded Skill self-review protocol

Every Skill created or refined by Skill Refiner must include a short self-review gate for its consequential
workflow stages. Its canonical record is constrained by the packaged
[`agent-skill.self-review-record/v1` JSON Schema](../schemas/v1/self-review-record.schema.json). Self-review checks
the frozen request and evidence; it does not authorize new requirements, replace independent review, or start an
open-ended test loop.

## Freeze the checklist

During planning, give each check a stable ID and link it to requirement, acceptance-criterion, and scenario IDs.
Each check states the expected result and the evidence that would prove it. Keep this checklist unchanged during
execution. A user-approved scope change is recorded as a new requirement and a new plan revision.

## Review at stage boundaries

Before an irreversible action, handoff, or final answer, the Skill compares its actual output and actions with the
frozen checklist. It records only the check result, evidence reference, any gap, and the next action; it does not
record hidden chain-of-thought.

Each check has one of these results:

- `pass`: the expected result is supported by observable evidence;
- `fail`: evidence shows a correctable gap;
- `not_applicable`: the condition does not apply, with a scope-based reason;
- `blocked`: required input, permission, runtime capability, or evidence is unavailable.

The overall result is `pass`, `revise_once`, or `blocked`. A pass requires every applicable check to pass. Missing
evidence is not a pass. A blocked result names the concrete blocker and the smallest next action.

## Bound correction and stop behavior

1. On `fail`, make one targeted correction tied to the failed check, then rerun that check and the frozen final
   controls required by the plan.
2. If the check still fails after that correction, stop with `blocked` and report the remaining evidence gap.
3. Do not add new checklist items while reviewing, repeat a passed check without new evidence or a material change,
   recursively review the review, or extend test rounds beyond the frozen budget.
4. If a correction changes behavior, rerun the complete frozen regression suite once, as required by the plan.
5. Keep independent acceptance separate. Self-review is the executing Skill's own evidence check and cannot mark
   an independent review as passed.

## Record shape

An ephemeral or persisted self-review record follows this structure:

```json
{
  "schema_version": "agent-skill.self-review-record/v1",
  "stage_id": "STAGE-01",
  "overall_result": "pass",
  "review_round": 0,
  "checks": [
    {
      "check_id": "SR-01",
      "requirement_ids": ["R1"],
      "acceptance_criterion_ids": ["R1.A1"],
      "scenario_ids": ["SC-NORMAL-01"],
      "result": "pass",
      "evidence_refs": ["output/result.json#R1"]
    }
  ],
  "open_gaps": []
}
```

If the Skill persists this record or passes it to another stage, declare the Schema and validator in
`stage_contracts`, package the Schema under `schemas/vN/` with a path matching its version, and validate the record
at generation, handoff, resume, and final verification. If the host cannot persist files, keep the same structured
record in the active response and report only the concise result and evidence.
