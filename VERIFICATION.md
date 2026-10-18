# Verification record — skill-refiner 0.3

Current 0.3 verification: 2026-10-18, macOS ARM64, pinned skill-up v0.13.0. Historical 0.2 records are preserved below.

Repository readers: see the [portable verification summary](verification/README.md).
Full `.refiner-runs/` archives remain local and are excluded from Git; links into
those archives below describe the original verification workspace.

## Business creation, real model matrix and native repair (0.3, 2026-10-18)

The project and installed Codex Skill now support one-request business analysis,
independent contract review, complete first implementation, autonomous normal/
boundary/error test planning, standards-based repair and multi-engine acceptance.
Analysis and authoring roles are independently configurable; budgets cover the
whole invocation. Creation resume validates frozen business/draft/suite hashes
and reruns all verdicts. It does not reuse PASS results.

- **45 automated tests passed**, including real skill-up two-round repairs,
  engine matrix regression, failed-acceptance withholding, creation resume,
  original-baseline replay, observer tamper detection and stream/command controls.
  These automated checks use deterministic backend doubles; skill-up itself runs
  where applicable. Live model evidence is recorded separately below.
- **Real generated business Skill:** local inventory/order allocation, authored
  from a paragraph without an initial Skill or a manually supplied implementation.
  Codex planned/reviewed its contract and cases; a real native Codex child authored
  the complete Skill, reference contract and Python implementation.
- **Final replay:** Codex and native child each passed 5/5
  development cases and 6/6 independent acceptance cases,
  including batched invalid inputs, decoding errors and actual script exit status.
  22 fresh target evaluations; 11
  parent-linked native evaluation children; 27
  additional independent direct-code business probes passed.
- **Actual repair follow-up:** in a separate copy, a declared inventory defect
  was reproduced by Codex. The initial native execution also failed, but because
  it passed an extra argument, so that result is not evidence of the injected
  defect. The repairer distinguished those causes and left argument validation
  intact. A native Agent repaired the supporting script in
  1 code-changing round. The first full development
  matrix passed, but an unresolved execution concern still blocked qualification.
  A second native review cleared that concern with no source changes, followed
  by another full development matrix and then acceptance on both engines:
  34 actual evaluations across 2 loop rounds.
  The original accepted Skill and its frozen suites remained unchanged.
  This supplemental harness directly invokes the production Agent, protected
  patch application, standards review and Matrix components with the real frozen
  suites; it is separate from the complete CLI creation/recovery run.
- **Installation:** source and installed Skill match apart from generated local
  settings/build caches. Independent Python/tool runtime, implicit natural-language
  discovery, direct helper execution and out-of-project startup were verified.

This final creation run resumes the real analysis, authoring and frozen suites
from `live-create-6` after controller fixes. Its first business implementation
already passed after those fixes (`unchanged`); it must not be
described as an unattended first-attempt success or a spontaneous business-code
repair. Earlier failed/interrupted attempts and original verdicts are preserved.
The follow-up defect is explicitly injected test material. No old lock or verdict
was rewritten, and native repairs never received the held-out acceptance cases.

There are 118 preserved, deduplicated Python
execution observation records in the main run. Upstream exports diagnostic ZIPs
under two names; their aliases are not counted twice. These records are distinct
from the 22 model case evaluations.

The new frozen Python observer records actual output, exit status, input hashes
and Python audit events; it is not OS isolation. JSON-stream comparison preserves
values/order/count without invented serialization restrictions. A fixed shell
parser recognizes real newline-separated commands while preserving quoted data.
External services, authenticated Cursor execution and live custom-model endpoints
remain unverified; test doubles are kept distinct from real-model evidence.

[Complete machine-readable verification](.refiner-runs/create-feature-20261018/feature-verification.json) ·
[Creation report](.refiner-runs/create-feature-20261018/live-create-7-resumed/report.md) ·
[Case inventory](.refiner-runs/create-feature-20261018/live-create-7-resumed/test-scenarios.md) ·
[Native repair evidence](.refiner-runs/create-feature-20261018/live-fault-regression/verification.json).

## Historical 0.2 records

All remaining sections describe the earlier 0.2 implementation and verification. Current 0.3 results are above.

### Real Skill self-audit

Latest integrated run (2026-10-18): `unchanged`, independently verified. It completed 4 model-backed child-Skill refinement workflows through Codex and native child execution, including actual supporting-code repairs and independent acceptance. Thirty compatibility tests and fixed-auditor positive/negative controls pass. Native Agent-generated source changes and manual integration are explicitly distinguished; earlier not_converged and cancelled states remain preserved. See [the full self-audit](SELF-REFINEMENT.md) for matrices, raw archives, hashes and limits.

### Codex installation and natural-language entry point

Installed on 2026-10-18 under `~/.agents/skills/skill-refiner`, with a compatibility
symlink under `~/.codex/skills`. A separate local runtime contains a new Python
virtual environment with the previously tested dependency versions, an identical
skill-up binary and the saved Codex/native-child matrix configuration. No global
Codex configuration or authentication was changed. The installed version does not
depend on this project's working directory or external-volume source checkout.

Seven entry-point/dependency checks passed from a temporary directory with minimal
PATH and no PYTHONPATH/VIRTUAL_ENV. The real app-server `skills/list` returned one
enabled matching Skill for both the project and `/private/tmp`. A separate real
Codex call received a Chinese request without a Skill-name mention, selected and
read the installed Skill, and successfully ran its `launch.py doctor` and target
`check`. That behavioral check deliberately used read-only mode and did not start
the full refinement pipeline. It does not imply the user's desktop session is
read-only. Core refinement modules are unchanged from the complete self-audit;
the installation adds its launcher, natural-language instructions and UI policy.

See [installation verification](.refiner-runs/codex-install-20261018/verification.json)
and [natural-language events](.refiner-runs/codex-install-20261018/natural-language-events.jsonl).

### Final live refinement

[Final report](.refiner-runs/v2-release/report.md) · [state](.refiner-runs/v2-release/state.json) · [evidence](.refiner-runs/v2-release/final/evidence.json) · [patch](.refiner-runs/v2-release/final/changes.patch).

Configuration: [live-local.json](examples/backends/live-local.json). Planning/review use Codex; repair uses a verified native Codex child. Every case is executed independently by Codex and a native child, through real skill-up. The configured and observed Codex model was `gpt-6.1-sol`; CLI version was `0.163.0-alpha.10.2`. Other Codex models and explicit child-model overrides have not been live-verified.

| Executor | Original baseline | Candidate regression | Independent acceptance |
|---|---:|---:|---:|
| Codex | 0/3 | 3/3 | 3/3 |
| Native Codex child | 0/3 | 3/3 | 3/3 |

The run performed **18 target evaluations**, accepted one autonomous repair round, and made three controller-role calls after planning. Nine completed native evaluation children and one completed native repair child are linked to actual parent rollouts, with observed model names and real command records. Only `scripts/total.py` changed: the child replaced truncating float arithmetic with integer cents and fixed the empty-array result. Original Skill files were preserved.

The development and acceptance plans were autonomously generated and independently reviewed in four real role calls under [planning provenance](.refiner-runs/v2-final-live/agent-calls/). That QA run was interrupted after freezing its plans while executor/grader compatibility was corrected. The final run reused those unchanged locked plans, revalidated controls, and reran the baseline and every verdict. It did not reuse any PASS result. Acceptance inputs were omitted from repair requests.

After completion, original/candidate manifests, the frozen plan/input lock, checker hash, same-suite engine hashes and completed child records were independently checked. Elapsed time: 785.85 seconds. Small suites establish observed behavior, not statistical reliability.

### Other execution modes

| Mode | Evidence and current verification status |
|---|---|
| Claude Code | Additional real comparison: regression 3/3, acceptance 2/2; see [report](.refiner-runs/v2-claude-portable/report.md). Uses a separate earlier frozen suite and the CLI's local default model. |
| Cursor Agent | Installed CLI `2026.01.23-916f423` actually launched, then reported authentication required. Five attempted cases were ERROR. Successful task execution and real successful tool-event parsing remain **unverified**. |
| OpenAI-compatible model API | Working HTTP client and bounded shell/file tool loop implemented. A local HTTP provider double requested a command, received its real result, and returned the observed answer; this is protocol/tool-loop verification, **not a live-model test**. No live endpoint/credentials were supplied; the local demonstration endpoint refused connection. |
| Custom command adapters | Real skill-up integration tests execute the actual target script and require two repair rounds across two adapters. Planner and executor decisions are deterministic doubles, **not live LLM evidence**. |

[Unavailable-backend report](.refiner-runs/v2-unavailable/report.md) retains the Cursor authentication and local model endpoint errors. These engines were not silently skipped. As requested, implementation proceeded with available backends and keeps the remaining verification status explicit.

### Automated checks

`python -m unittest discover -s tests -v`: **23 tests passed** in 34.259 seconds. Tests cover independent role routing, real skill-up matrix repair, existing-suite preservation, cross-engine regression protection, acceptance failure withholding delivery, source-edit detection, API tool execution, actual native-child evidence requirements, judge identity mismatches, equivalent workspace paths versus literal matching, and nested-process timeout cleanup. Skill Creator quick validation and `skill-refiner check skill-refiner` also passed.

### Compatibility corrections found through execution

- Pinned skill-up script judges used a clock-derived temporary directory that collided during parallel cases; observed cases read another case's script/transcript. Runs now use effective case parallelism one plus a per-user process lock. Requested/effective parallelism are recorded, and mismatched judge case IDs become ERROR. This trades throughput for trustworthy local judging.
- Different CLIs emitted shell-list separators and absolute/relative forms for the same workspace files. Matching now recognizes proven existing in-workspace aliases while retaining raw commands/output. `literal_command: true` retains strict spelling checks. The Claude comparison above verifies the previously failing path cases through real execution.
- Evaluation adapters check the installed candidate's source before and after execution. An executor that repairs the Skill while being evaluated produces ERROR; legitimate task output files remain allowed.

No Docker isolation or OS-level read barrier is claimed. Current native child verification depends on the observed Codex rollout format. The [backend guide](skill-refiner/references/backends.md) documents supported contracts and prerequisites.
