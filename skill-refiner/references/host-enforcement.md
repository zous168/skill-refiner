# Host enforcement boundary

Agent Skills describe when a capability applies and how to use it. The format has
no universal pre-tool hook, filesystem permission, mandatory-call declaration or
final-answer gate. A compatible host may still skip Skill discovery, ignore
instructions or allow an agent to edit a Skill directly. The optional
`agents/openai.yaml` provides Codex discovery metadata; the portable workflow is
defined by `SKILL.md` and its linked references, and this metadata does not enforce
execution in any host. Other hosts may use their own discovery metadata or install
the standard Skill directory.

This Skill makes compliance observable and fail-closed inside the controller:

- `create` and `run` return a nonzero status for incomplete workflows and withhold
  the final candidate or requested destination.
- `verify RUN_DIRECTORY` independently checks the frozen suite lock, baseline
  execution, passing full regression, fresh acceptance, candidate file manifest,
  and executor consistency. It exits nonzero for missing, edited or failed evidence.
- Only `status=verified` authorizes the Skill to report a completed run. Agent prose
  and an unverified draft are not substitutes for the gate.

For a host that must prevent bypass, its integration must make the controller the
only path to protected Skill writes or block publication unless `verify` succeeds.
That requires host-specific tool permissions, a before/after hook, or a restricted
tool adapter. Install the Skill and its runtime in each host, configure that host's
gate, then test the host adapter with both a complete run and an intentionally failed
acceptance. Without such a host integration, describe this as a fail-closed workflow
for agents that follow the Skill, not as a universal enforcement guarantee.
