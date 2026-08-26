# Ballast reference analysis for HeadLong Observer

Date: 2026-08-27

## Conclusion

Ballast should be treated as a source of governance patterns, not as a runtime
dependency or a replacement memory system. Its strongest contributions are:

1. user-confirmed decisions are distinct from model inference;
2. changes supersede prior decisions instead of erasing them;
3. a delivered rule is audited separately from whether it was obeyed;
4. reusable procedures require successful evidence before promotion.

Those ideas fit the Observer, but the Ballast file layouts and hook script do
not fit directly. The first Observer is proposal-only, spans multiple scoped
projects, and requires stable Evidence Locators. Ballast's canonical stores are
project-local Markdown/JSON, and its delivery log lacks session and source-event
identity. The recommended path is therefore **adopt the semantics, implement
them on HeadLong's append-only trajectory and scoped projections, and defer all
Codex behavior-changing hooks until the shadow gate passes**.

One especially important correction to the supplied analysis: Ballast's
`PreCompact` harness proves that its script emits a reminder; it does not prove
that Codex or current Claude Code accepts that output. At the pinned revisions,
the emitted shape is not a supported `PreCompact` context-injection shape, so it
must not be used as a durability boundary.

## Method and pinned sources

- Ballast was cloned and inspected at commit
  [`fea4b4afc93c2416a9bb37c27d94b2556b13eb08`](https://github.com/svy04/ballast/tree/fea4b4afc93c2416a9bb37c27d94b2556b13eb08)
  (2026-08-25). The first-party source, skill files, manifests, changelog, and
  verification harness were read. The harness was rerun locally: 20/20 cases
  passed.
- Codex hook behavior was checked against OpenAI's source at commit
  [`6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c`](https://github.com/openai/codex/tree/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c)
  (2026-08-26), including schemas, runtime dispatch, and integration tests.
- Claude Code behavior was checked against Anthropic's official current
  [Hooks reference](https://code.claude.com/docs/en/hooks).
- HeadLong was inspected locally at commit `244d9ee0b39e5a5a2854b39ad539134dc1fd14ee`,
  together with the uncommitted Observer context and ADRs that define the
  agreed model. Local references below point to those working files.

The supplied write-up was used only to identify hypotheses to test. Repository
popularity, maturity scores, and similar secondary judgments were not used as
evidence.

## Fit with the agreed Observer model

The Observer is defined as proposal-only, with a stable Evidence Locator on
every proposal, explicit global/project Knowledge Scope, model inference held
as a Memory Candidate, and only user-stated or user-accepted knowledge promoted
to Active Memory. See [CONTEXT.md](../../CONTEXT.md),
[ADR 0001](../adr/0001-authority-aware-memory-promotion.md), and
[ADR 0002](../adr/0002-one-observer-with-scoped-knowledge.md).

Ballast's decision ledger is unusually close to that contract: it records only
user-confirmed decisions, keeps unconfirmed readings elsewhere as `assumed`,
and represents a changed decision with a new entry plus bidirectional
supersession links rather than an edit
([decision-ledger](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/decision-ledger/SKILL.md#L18-L39)).
Ballast's `pin` also requires approval before writing a standing rule
([pin](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/pin/SKILL.md#L24-L50)).
These are direct semantic matches.

The storage model is not a match. HeadLong's current `mem` store permits edit
and deletion, and its semantic search concatenates every memory into one LLM
request ([local `bin/mem`](../../bin/mem)).
By contrast, HeadLong trajectories are append-only and give every step a stable
UUID; run and trigger links are explicit
([trajectory spec](../../design/trajectory_spec.md)).
The trajectory is therefore the suitable event source for promotion and
supersession, while `mem` can become a rebuildable current-state projection.

There is also a concrete incompatibility in the current thinker prompt: the
`learn` route tells the monolith to store a reusable lesson, skill, or fact
directly with `mem add`
([monolith prompt](../../thinkers/monolith/prompt.md)).
For the Observer identity, that route must create a Memory Candidate, not an
Active Memory, unless the source event contains explicit user authorization.

## Component decisions

| Ballast component | Decision | Reason and Observer adaptation |
| --- | --- | --- |
| Decision ledger and supersession | **Adopt semantics; adapt storage** | Preserve `user-confirmed only`, provisional readings, append-only events, and explicit supersession. Store scope, Evidence Locator, source actor, and promotion reason on every event. Derive the current Active Memory view instead of making one Markdown file canonical. |
| Pin | **Adapt to a proposal type** | A correction may yield a `Standing Rule Candidate`, including Ballast's useful incident classification (delivery regression, propagation miss, delegation leak, variant evasion, compression loss, substitute illusion). The Observer puts it in the Proposal Inbox; it does not write a project/user rule catalog. Acceptance may promote the underlying constraint to Active Memory, while materializing a hook rule remains a separate explicit action. |
| Rule hook | **Defer** | Prompt/tool interception changes another agent's behavior and therefore exceeds the prototype's proposal-only authority. If later enabled, make it a replaceable delivery adapter backed by accepted scoped constraints, not part of Observer memory. Ballast's case-insensitive substring/regex matcher and 12-rule/6,000-character cap are intentionally small and deterministic, but substring matches can overfire and are not semantic retrieval ([engine](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/hooks/scripts/ballast-rules.mjs#L52-L125)). |
| Delivery audit/report | **Adopt the measurement distinction; adapt schema** | Ballast logs rule ids, event kind, directory basename, size, and time, but not the source prompt/tool payload; its own report correctly says fire count measures delivery, not obedience ([logging and report](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/hooks/scripts/ballast-rules.mjs#L128-L166), [report skill](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/report/SKILL.md#L6-L24)). Observer audit rows additionally need Registered Project id, Codex session id, turn/tool id, rule revision, and Evidence Locator. Keep `delivered`, `acknowledged`, `followed`, and `violated` as different facts. |
| Verify gate | **Adapt, do not copy wholesale** | The labels `confirmed`, `observed`, `assumed`, `hearsay`, and `unknown`, refute-first behavior, dates, and limits are useful ([verify-gate](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/verify-gate/SKILL.md#L8-L30)). However, “two independent primary sources” and “n=1 can never be confirmed” are not universal: one deterministic test or one authoritative user decision can be sufficient for its narrowly stated claim. More importantly, epistemic confidence must remain separate from authority: a well-corroborated model inference is still only a Memory Candidate, while an explicit user preference can be Active Memory even if it is not an empirical fact. |
| Checkpoint | **Adopt snapshot shape; reject it as source of truth** | The five fields—story, decisions, waiting on user, next first action, tried—are a good compact operational view ([checkpoint](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/checkpoint/SKILL.md#L10-L42)). Generate such a view from the Codex session cursor and Observer events. Do not require the observed project to maintain `CHECKPOINT.md`, and do not treat single-use `HANDOFF.md` deletion as auditable state. The authoritative record remains the Codex Session; the snapshot is rebuildable. |
| PreCompact | **Reject as a capture/durability mechanism** | See the host-enforcement analysis below. The Observer's at-least-once session reader, five-minute Provisional Analysis, and thirty-minute/archive Final Consolidation already provide a stronger boundary. At most, use host-specific compact events as hints to advance a cursor or schedule a post-compact reconciliation. |
| Recall | **Reject full sweeps; adapt staged retrieval** | Ballast requires an index-level pass over five stores at the first substantive reply and every subject shift, even after a hit ([recall](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/recall/SKILL.md#L13-L42)). That is reasonable for a small project brain but conflicts with one Observer spanning many projects and strict Knowledge Scope. HeadLong already has a cheap word index and passive resurfacing path ([index](../../thinkers/retrieval/build-index.sh), [retrieval thinker](../../thinkers/retrieval/step)), while its own design explicitly notes that flat `mem search` remains O(n) and proposes progressive drill-down ([progressive-memory design](../../design/unified_progressive_resolution_memory.md)). Use scope/status filters, lexical shortlist, then evidence drill-down; add embeddings only if observed recall misses justify them. |
| Tiered episodic memory | **Reuse HeadLong, not Ballast** | HeadLong already treats the raw trajectory as testimony and rollups as an index whose summaries carry cited step ids; it explicitly warns that summary-of-summary is only a pointer ([tiered memory](../../design/tiered_memory.md)). That maps directly to Evidence Locators and is stronger than Ballast's flat knowledge sweep. |
| Skill forge | **Adapt to `Observer Improvement Proposal`** | Ballast requires both likely recurrence and a verified successful run, and records the success evidence ([skill-forge](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/skill-forge/SKILL.md#L10-L35)). Keep that threshold, but have the Observer propose a skill change rather than write one. Reuse HeadLong's existing skill author/validation mechanism only after explicit acceptance. |
| Goal engine | **Do not adopt as an Observer subsystem** | The Observer is not the user's task orchestrator. Retain only the `mobilize what is already held before researching` prompt rule and source-linked goal skeleton idea ([goal](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/goal/SKILL.md#L16-L45)). Work planning remains with Codex and the existing skill system. |
| Schedule | **Defer; reuse only the authority boundary** | Ballast says the agent notices, records, judges, and prepares while the human fires outward actions ([schedule](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/skills/schedule/SKILL.md#L6-L39)); that agrees with proposal-only authority. Its `memory/SCHEDULE.md` is not an adequate machine scheduler for five-/thirty-minute session deadlines. HeadLong's existing durable `wake_at`/dispatcher mechanism is the better implementation primitive ([monolith step](../../thinkers/monolith/step)). Revisit user-facing deadlines only after core observation works. |

## What the hooks actually enforce

### Ballast itself

The executable rule engine merges user and project catalogs, matches prompt or
selected tool fields, can block a prompt with exit code 2, can deny a tool call
with `permissionDecision: "deny"`, logs delivery, and deliberately fails open
on internal errors
([source](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/hooks/scripts/ballast-rules.mjs#L180-L210),
[prompt/tool paths](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/hooks/scripts/ballast-rules.mjs#L279-L374),
[fail-open](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/hooks/scripts/ballast-rules.mjs#L478-L512)).
Its 20-case harness checks the script's stdout, stderr, exit code, manifest
shape, and local log. It does not launch Claude or Codex, so a passing
`PreCompact` emission case is not a host-integration test
([harness](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/hooks/scripts/verify-hook.mjs#L1-L40),
[PreCompact case](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/hooks/scripts/verify-hook.mjs#L350-L363)).

Injection and enforcement are different. A `deny` result is host-enforced when
the host accepts the schema; `additionalContext` is only delivery to the model,
not obedience. Ballast itself makes the same distinction in its README and
report skill.

### Codex

Current Codex source declares `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
and `PreCompact` among its hook events
([event registry](https://github.com/openai/codex/blob/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c/codex-rs/hooks/src/lib.rs#L26-L56)).
Its `PreToolUse` path parses deny, additional context, and input rewriting, and
the runtime records additional context before continuing or returns a blocked
tool result
([parser/handler](https://github.com/openai/codex/blob/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c/codex-rs/hooks/src/events/pre_tool_use.rs#L29-L137),
[runtime](https://github.com/openai/codex/blob/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c/codex-rs/core/src/hook_runtime.rs#L176-L228)).
The repository has integration tests for blocking and adding context on shell,
`apply_patch`, and local function tools
([tests](https://github.com/openai/codex/blob/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c/codex-rs/core/tests/suite/hooks.rs#L3295-L4747)).

The Ballast `PreCompact` output is not compatible with this Codex revision.
Ballast prints `hookSpecificOutput.additionalContext` plus `systemMessage`
([Ballast source](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/hooks/scripts/ballast-rules.mjs#L243-L255)),
whereas Codex's `PreCompactCommandOutputWire` accepts only the universal fields
and denies unknown fields
([Codex schema type](https://github.com/openai/codex/blob/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c/codex-rs/hooks/src/schema.rs#L163-L176),
[generated schema](https://github.com/openai/codex/blob/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c/codex-rs/hooks/schema/generated/pre-compact.command.output.schema.json)).
Codex's compact runtime consumes only stop/continue and does not record an
additional context value
([compact event](https://github.com/openai/codex/blob/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c/codex-rs/hooks/src/events/compact.rs#L225-L327),
[runtime](https://github.com/openai/codex/blob/6e008417bb3e76fa715d3c8b22e8cc77ab0bb84c/codex-rs/core/src/hook_runtime.rs#L466-L498)).
Therefore the Ballast reminder should be expected to fail parsing, not to
reach the model.

Ballast's own Codex document is appropriately cautious: it reports live
observations only for `SessionStart` and `UserPromptSubmit`, and says
`PreToolUse`/`PreCompact` were not Codex-tested
([docs/CODEX.md](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/docs/CODEX.md#L5-L30)).
That document and `.codex-plugin/plugin.json` also remain at fifteen skills and
version 0.10.0 while the Claude manifest is 0.11.0, further supporting a
pin-and-test policy rather than tracking `main` implicitly
([Codex manifest](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/.codex-plugin/plugin.json#L1-L30),
[Claude manifest](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/.claude-plugin/plugin.json#L1-L23)).

### Claude Code

Anthropic documents `UserPromptSubmit` and `PreToolUse` context injection and
host-enforced PreToolUse denial; `PreToolUse.additionalContext` is placed beside
the tool result
([official context semantics](https://code.claude.com/docs/en/hooks#add-context-for-claude),
[official PreToolUse control](https://code.claude.com/docs/en/hooks#pretooluse-decision-control)).
However, current Claude Code documents `PreCompact` as a blocking/side-effect
event, explicitly discards `systemMessage` and `continue`, and does not list
`additionalContext` for this event
([official PreCompact reference](https://code.claude.com/docs/en/hooks#precompact)).
Thus Ballast's current `PreCompact` reminder is not a portable model-context
delivery mechanism on either host, even though the event itself exists.

## Recommended Observer integration

Use four separable layers:

1. **Source record:** existing Codex Session data remains authoritative. The
   ingestion cursor is at-least-once and every normalized event retains a
   source-session Evidence Locator.
2. **Append-only Observer events:** record observations, candidates, user
   accept/reject actions, activations, and supersessions as new trajectory
   events. Never rewrite the evidentiary history.
3. **Scoped projections:** build current Active Memory, open Memory Candidates,
   and Proposal Inbox views from those events. Every row has `global` or one
   Registered Project scope. Flat `mem` files may serve as projections during
   the prototype, but they are not canonical.
4. **Optional delivery adapters:** only after the shadow gate, export accepted
   constraints to a Codex/Claude-specific rules adapter. Audit delivery with
   host/session/turn/rule-revision locators. Do not call a delivered rule
   “followed” without separate behavioral evidence.

For epistemic state, keep three independent dimensions rather than one Ballast
label doing all jobs:

- `evidence_kind`: user statement, direct event/tool observation, primary
  external source, secondary source, or model inference;
- `verification`: unverified, observed, corroborated, refuted, or stale;
- `authority`: candidate, active, rejected/dismissed, or superseded.

This prevents a corroborated inference from silently gaining user authority and
prevents a user preference from being rejected merely because it has no sample
size.

## Prototype sequencing impact

Ballast does not justify broadening the first prototype. The high-value order is:

1. ingest only Registered Project sessions and prove cursor/deduplication plus
   Evidence Locator resolution;
2. produce Provisional Analysis and Final Consolidation;
3. implement the proposal inbox and authority-aware memory event/projection
   model;
4. add scoped lexical retrieval and evidence drill-down;
5. run the agreed shadow gate;
6. only then evaluate a rules delivery adapter and delivery audit;
7. defer goal/schedule orchestration and automatic skill materialization until
   observed demand supplies evidence.

No Ballast source code needs to be copied for steps 1–5. If the later hook
adapter reuses code, Ballast is MIT-licensed
([LICENSE](https://github.com/svy04/ballast/blob/fea4b4afc93c2416a9bb37c27d94b2556b13eb08/LICENSE)),
but the adapter should be pinned and host-tested independently rather than
vendoring the plugin unchanged.
