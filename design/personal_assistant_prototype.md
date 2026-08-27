# Personal assistant prototype

Status: accepted on 2026-08-27. This document defines the first bet and its
authority boundary; it does not describe code that already exists.

## Bet

Use HeadLong as the persistent runtime for one Personal Assistant. The first
prototype observes authorized development activity and public web References,
maintains evidence-backed scoped memory, and produces reviewable work and
Observer improvement proposals. It does not modify project repositories, create
Work Items, install behavior-changing hooks, or otherwise change external
state.

Keep HeadLong's Linux/systemd deployment, persistent monolith, shell actor,
trajectory, tiered recap, flat memory, dashboard, and identity structure until
observed evidence shows that one of them is a problem. Do not mount project
repositories into the prototype actor.

## Authority allocation

| Owner | Responsibilities |
| --- | --- |
| Deterministic software | Source registration, cursors, replay, deduplication, Evidence Locators, scope enforcement, exploration limits, immutable Reference revisions, promotion checks, and evaluation metrics |
| DeepSeek through LiteLLM | Session interpretation, Provisional Analysis, Final Consolidation, Memory Candidates, Improvement Proposals, bounded web exploration, Reference selection, pattern detection, and open-loop detection |
| User | Register projects and recurring sources, approve global scope and inferred memory, review proposals, authorize future external writes, and enable any future rule-delivery adapter |

Raw Codex Session content may be sent to DeepSeek. The source session remains
authoritative and is not copied wholesale into the HeadLong trajectory.

## Sources

The first source set is:

1. Codex Sessions associated with explicitly Registered Projects;
2. explicitly registered URLs, RSS feeds, and documentation sites; and
3. Hacker News.

Registered Projects and Registered Web Sources use explicit add, list, and
remove operations. Git and Linear initially serve as evidence adapters for
observed development work rather than independent activity collectors.
Calendar, mail, browser history, Home Assistant, and authenticated or
private-network web sources are outside the first prototype.

The Web Source Bridge may perform interest- or open-loop-driven public web
search and follow discovered public links within per-run page, depth, time, and
storage limits. Discovering a site does not register it for recurring
exploration.

## Records and projections

The Activity Ledger is implemented on HeadLong's append-only trajectory and is
canonical for observation, evidence, authorization, promotion, rejection, and
supersession events.

- Active Memory is a rebuildable flat projection containing only explicit or
  user-accepted decisions, preferences, and constraints.
- Model-inferred facts, patterns, and open loops remain Memory Candidates.
- Reference Store revisions contain selected, sanitized document text, source
  identity, fetch time, content digest, and Evidence Locator. A changed document
  creates a revision rather than overwriting history.
- Rejected References retain only identity, digest, and judgment evidence.
- Proposal Inbox is a rebuildable projection with `pending`, `accepted`,
  `rejected`, and `dismissed` review states. Acceptance grants no execution
  authority.
- Personal Wiki remains authoritative for long-form knowledge deliberately
  authored or curated by the user.

The initial Reference Store is file-based. Add SQLite/FTS, embeddings, or a
different retrieval layer only after observed misses or scale problems justify
the change.

## Analysis and evidence policy

An active Codex Session receives Provisional Analysis after five minutes of
inactivity and Final Consolidation after thirty minutes of inactivity or
archival. Later activity supersedes earlier conclusions rather than deleting
them. Collection is at least once, with idempotent deduplication by stable
locator.

One explicit user correction, test failure, tool failure, or reviewer finding
may justify an Improvement Proposal. Inferred repetition, cost, noise, or
structural problems require evidence from three distinct sessions. Agent
proposals and unsupported self-evaluation are not evidence.

Only explicit user statements and accepted decisions, preferences, and
constraints become Active Memory automatically. Project scope is the default;
global scope requires explicit user language or acceptance. Repetition across
projects never promotes a memory globally by itself.

## External references

Web content is untrusted data, never instructions. Exploration is bounded and
idempotent. The model may save a selected Reference without approval because a
Reference is neither a verified fact nor Active Memory. The prototype does not
port the `personal-assistant-brain` Discovery state model; that repository is
design and regression evidence only. Additional Discovery rules are introduced
only when observed failures justify them.

Ballast is likewise a source of governance semantics, not a runtime dependency.
Decision supersession, delivery-versus-obedience audit, and evidence-backed
skill proposals use native HeadLong events. Codex or Claude rules hooks remain
disabled until the Shadow Gate passes.

## Delivery sequence

1. Verify LiteLLM and DeepSeek through HeadLong's OpenAI-compatible paths.
2. Add the Codex Session bridge with durable cursor and replay behavior.
3. Add Observation, memory, and proposal events plus rebuildable projections.
4. Add the Web Source Bridge and file-based Reference Store.
5. Add Proposal Inbox review to the dashboard.
6. Run the identity continuously under systemd.
7. Pass the technical and shadow gates before considering more authority.

## Acceptance

The technical gate requires restart-safe collection without observable loss or
duplicate projections, resolvable Evidence Locators, zero external mutations,
and passing authority-aware memory tests.

The Shadow Gate runs until either seven days or twenty Final Consolidations are
observed. It requires zero incorrectly promoted Active Memories and at least
80% manually judged useful and accurate Observations. Only after both gates pass
may the project consider repository worktrees, external writes, or a
host-specific rule-delivery adapter.
