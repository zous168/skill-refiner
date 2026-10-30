# Schema-backed Skill stage artifacts

This is the reusable authoring rule for every Skill created or refined by Skill Refiner. It also applies to
Skill Refiner itself. `schemas/` stores definitions; a run directory stores instances, results and hashes.
Do not place the only copy of a schema in a run directory.

## Freeze a stage contract before execution

The frozen plan uses `skill-refiner.skill-stage-contracts/v1`. For every stage that produces, persists or hands
off an artifact, declare:

- a stable stage ID, goal, linked requirement IDs and observable completion check;
- each input and output path, media type, whether it is required, and its schema ID and version;
- each schema's package-relative `schemas/vN/<name>.schema.json` path, validator ID and whether the delivered
  Skill needs that schema at runtime, plus the package-relative validator implementation path;
- the stage validation gate, failure states and resume policy;
- validation points after generation, before handoff, on resume and at final verification.

The stage contract itself is structured JSON and is validated against the Refiner's versioned stage-contract
schema before the plan gate can pass. Put the schema in `schemas/vN/` where `N` matches its declared version.
Give each logical schema a stable ID and declare exactly one active version
for that ID. Increment the version only when the persisted artifact format changes incompatibly, not for internal
edits to the Skill. Schema IDs and versions referenced by artifacts must be declared, schema paths must remain
inside the Skill package, and every declared schema must be used by an artifact.

## Package and execute the validators

Put each output-specific JSON Schema in the Skill's own `schemas/vN/` directory. Include only the active schema
definitions and validator implementations marked as needed at runtime in the delivered Skill. Keep historical
formats used only by the authoring controller's own old run records out of generated or refined target Skills.
Final verification inventories `schemas/**/*.schema.json` and rejects every target-package schema file not
listed as runtime-needed in the frozen stage contract, including old files omitted from the plan. Skill Refiner's
own private compatibility registry remains available for resuming its historical runs and is never copied into
other Skills.

Every consequential stage also uses the [bounded self-review protocol](self-review-protocol.md). Freeze its
requirement-linked checks in the plan, assess them against evidence before handoff, and limit correction to one
targeted round. The canonical check-record shape is the
[`agent-skill.self-review-record/v1` Schema](../schemas/v1/self-review-record.schema.json). If self-review data is
persisted or handed off, declare and package that Schema and a validator in `stage_contracts` like any other stage
artifact; validate it at generation, handoff, resume and final verification.
Do not copy Skill Refiner's private controller schemas into a target Skill; include only schemas that target
Skill actually uses.

Validate an artifact immediately after it is written. Before marking its stage complete or passing it to a
dependent stage, validate the artifact again and record the schema ID, version, package-relative path, schema
SHA-256, content SHA-256, byte count and validation result. On resume and final verification, reload the
packaged schema, verify its recorded hash and revalidate the content before reusing it. A missing or changed
schema, invalid artifact or mismatched hash blocks completion and reuse. Preserve the previous run evidence;
start a new run when its frozen schema or stage contract changes.

Every declared stage names at least one output, including a user-facing response when that is its result. A
purely transient internal action that creates no observable output should remain an action inside a stage, not
be represented as a separate artifact-producing stage. Inputs may be empty when a phase starts from the frozen
request, but do not invent placeholder files to fill the input list.

## Self-hosting rule

Skill Refiner's create and refine pipelines use the same package-relative schema registry and lifecycle
validator. Its `stages.json`, plans, analyses, reviews, repairs and other structured handoffs carry versioned
schema references. Completion, handoff, resume and final verification all invoke the validator. There is no
self-hosting exception.
