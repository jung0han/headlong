# HeadLong Personal Assistant

A persistent personal assistant that observes authorized activity, explores
external references, maintains evidence-backed memory, and proposes
improvements without changing external state during its first prototype.

## Language

**Personal Assistant**:
The HeadLong system that observes authorized activity, maintains scoped
evidence-backed memory, explores external References, and proposes improvements
for one user.
_Avoid_: Codex observer, Discovery tool

**Observer Identity**:
The single persistent HeadLong identity through which the Personal Assistant
observes one user's activities and keeps knowledge separated by scope.
_Avoid_: Project agent, session bot

**Activity Source**:
A user-authorized source of evidence about work, interests, or ongoing activity.
_Avoid_: Memory, tool

**Activity Ledger**:
The append-only history of observations, evidence, user authority, promotion,
rejection, and supersession from which current projections are rebuilt.
_Avoid_: Active Memory, transcript

**Registered Project**:
A development project explicitly included in the Observer's monitoring scope.
_Avoid_: Workspace, watched folder

**Codex Session**:
A stream of Codex activity associated with a Registered Project and retained as
the authoritative source record.
_Avoid_: Conversation, trajectory

**Web Source Bridge**:
A bounded adapter that presents external web References to HeadLong's native
observation flow without defining a separate assistant runtime.
_Avoid_: Discovery platform, crawler agent

**Registered Web Source**:
A web site, feed, or document collection explicitly authorized for recurring
exploration.
_Avoid_: Search result, visited site

**Reference**:
An external web document or knowledge object observed through a Web Source
Bridge.
_Avoid_: Active Memory, Observation

**Reference Revision**:
An immutable saved version of a selected Reference with its source identity,
observation time, and content digest.
_Avoid_: Memory version, cached page

**Reference Store**:
The durable collection of saved References and their revisions, kept distinct
from Active Memory.
_Avoid_: Personal memory, trajectory

**Personal Wiki**:
The Canonical Authority for long-form knowledge authored or deliberately
curated by the user.
_Avoid_: Active Memory, Reference Store

**Observation**:
A compact account of a meaningful change or signal found in an Activity Source.
_Avoid_: Summary, memory

**Evidence Locator**:
A stable reference from an Observation or Improvement Proposal to its source
event or Reference Revision.
_Avoid_: Citation, line number

**Improvement Proposal**:
An evidence-backed recommendation produced by the Observer without modifying
code, Work Items, or other external state.
_Avoid_: Improvement, fix, action

**Work Improvement Proposal**:
An Improvement Proposal about a Registered Project or the user's development
workflow.
_Avoid_: Observer improvement, project fix

**Observer Improvement Proposal**:
An Improvement Proposal about the Observer's own code, prompts, skills, or
operating configuration.
_Avoid_: Work improvement, self-modification

**Knowledge Scope**:
The boundary within which an Observation or memory is valid: either global to
the user or limited to one Registered Project.
_Avoid_: Namespace, visibility

**Memory Candidate**:
A possible fact, pattern, or open loop inferred from observations but not yet
treated as authoritative knowledge.
_Avoid_: Memory, insight

**Active Memory**:
A current decision, preference, or constraint that the user stated explicitly
or accepted and that remains linked to its evidence and prior versions.
_Avoid_: Memory Candidate, note

**Provisional Analysis**:
A revisable interpretation produced after a Codex Session has been idle for
five minutes.
_Avoid_: Final result, draft memory

**Final Consolidation**:
The reconciliation of a Codex Session's observations after thirty minutes of
inactivity or archival.
_Avoid_: Summary, compaction

**Proposal Inbox**:
The Observer-owned collection of Improvement Proposals awaiting human review.
_Avoid_: Backlog, Linear project

**Proposal Review State**:
The user's disposition of an Improvement Proposal: `pending`, `accepted`,
`rejected`, or `dismissed`. Acceptance recognizes value but grants no execution
authority.
_Avoid_: Execution status, Work Item status

**Shadow Gate**:
The evidence threshold the proposal-only prototype must pass before any broader
authority or behavior-changing rule delivery is considered.
_Avoid_: Launch date, feature flag
