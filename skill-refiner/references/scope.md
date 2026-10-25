# Supported scope

- Local text/code Skills and fixtures; inspect text resources and supporting code.
- Deterministic frontmatter, relative-reference and Python syntax checks.
- Additional authoring standards reviewed by a fresh Agent context.
- Actual skill-up execution using Codex/Claude or a native custom engine.
- Independent planning/repair/review profiles, managed Cursor/Codex/native
  Codex-subagent/model API execution, and frozen-suite matrix comparison. See
  [backends](backends.md) for exact contracts, evidence and prerequisites.
- JSON/text/file assertions and recorded command invocation checks.
- Existing `evals/eval.yaml` in skill-up format, environment `none`; its cases and
  judges are preserved, while its execution engine is overridden consistently for
  baseline/candidate runs. Existing Anthropic `evals.json` requires explicit upstream
  import first; it is never silently ignored.
- Candidate copies, frozen oracle hashes, bounded repair rounds, retained per-case
  regression gates, independent acceptance inputs and JSON/Markdown/patch artifacts.

The current version does not implement Docker isolation, live production connector
operations, visual inspection, semantic output graders, automatic arbitrary Python
test generation or verified natural trigger selection. It tests ability with the
Skill explicitly requested. It never claims that these capability tests measure
trigger precision/recall. Binary or unread resources must be surfaced as coverage
gaps when they matter to required behavior.

Fresh role contexts reduce contamination but do not create an OS-level read barrier.
Native Codex subagent mode adds verified spawning and parent-linked child evidence;
ordinary role processes are not described as native subagents. Implementation and
protocol tests do not establish live model availability. See the project's
verification record for observed and blocked backends.
The repair prompt initially omits acceptance cases, and the acceptance set contains
new task inputs. If a qualified candidate fails acceptance, only the failed acceptance
cases and their raw execution evidence are exposed to repair; the full acceptance suite
is rerun after regression. Small sets establish observed results, not a statistical
confidence guarantee. Generated tests may themselves miss behaviors; inspect coverage artifacts.

Upstream reuse: skill-up's execution/judging/report machinery is used as an external
binary, pinned to v0.13.0 at initial implementation. Planning, patch control,
checks and iteration management are implemented here. Anthropic's grader and
skill-creator informed the review method; their source files are not vendored.

For legacy single-engine runs with recent Codex CLIs, the native upstream engine is invoked through a temporary,
per-process PATH shim adding `exec --ephemeral`. This avoids v0.13.0's incomplete
parsing of newer persisted `custom_tool_call` records: upstream then consumes its
supported JSONL event stream with command executions intact. No global executable,
model setting or authentication file is changed. Retained stdout artifacts remain
available in skill-up reports.
Managed matrix profiles use their own JSON-event adapter through skill-up's custom
local transport; they do not rely on this legacy compatibility shim.

