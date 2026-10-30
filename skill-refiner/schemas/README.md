# Skill Refiner schema bundle

Schema definitions are grouped by format version. Keep the logical filename stable and put the version in the
directory, for example `schemas/v1/analysis.schema.json` and `schemas/v2/analysis.schema.json`. A run records the
schema ID, version, package-relative path, validator ID and SHA-256 used. Runtime instances and validation evidence
live in the run directory.

```text
schemas/
├── v1/
│   ├── analysis.schema.json
│   ├── analysis-review.schema.json
│   ├── artifact-record.schema.json
│   ├── binary-content.schema.json
│   ├── execution-plan.schema.json
│   ├── json-object.schema.json
│   ├── markdown.schema.json
│   ├── repair.schema.json
│   ├── skill-stage-contracts.schema.json
│   ├── stage-artifact-contract.schema.json
│   ├── unified-diff.schema.json
│   └── utf8-text.schema.json
├── v2/
│   ├── analysis.schema.json
│   ├── analysis-review.schema.json
│   ├── plan.schema.json
│   ├── plan-review.schema.json
│   └── stages.schema.json
├── v3/
│   ├── plan.schema.json
│   └── plan-review.schema.json
├── v4/
│   ├── plan.schema.json
│   └── plan-review.schema.json
├── v5/
│   ├── plan.schema.json
│   └── plan-review.schema.json
└── README.md
```

## Schema roles

- `v2/stages.schema.json` validates each Refiner `stages.json` manifest.
- The artifact-record and content schemas validate persisted artifact indexes and byte-level formats.
- `v2/plan.schema.json` through `v5/plan.schema.json` retain the historical plan contracts needed to resume old
  Refiner runs; v4 added reusable cross-Skill `stage_contracts`, and v5 added the executable work breakdown.
- `v1/execution-plan.schema.json` constrains the structured execution plan.
- The plan-review schemas validate independent plan review evidence at each supported plan generation.
- `v1/analysis.schema.json` and `v1/analysis-review.schema.json` remain available to read legacy runs. New
  creation uses the v2 analysis and review schemas for context, actors, scope, traceable requirements, workflow,
  risk applicability, assumptions, dependencies, blocking decisions and scenario links.
- `v1/skill-stage-contracts.schema.json` defines the reusable cross-Skill stage and artifact contract.

The registry and content validators live in `scripts/refiner/artifact_validation.py`. It now emits the versioned
directory path for new runs and accepts an old flat path only when the saved schema identity, version, validator
and hash still match. A changed schema hash still blocks resume. The package tests compare Python runtime
contracts with the corresponding JSON Schema files.

For generated Skills, the frozen stage contract declares one active version per logical Schema ID. The final
package gate requires every target `schemas/**/*.schema.json` file to be declared as runtime-needed. Refiner's
private historical schemas stay inside this controller to preserve old-run compatibility and are not copied to
generated Skills.
