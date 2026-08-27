# Allow the observer to learn memory autonomously

The Observer follows HeadLong's native behavior: its LLM may decide that a
reusable fact, lesson, decision, preference, constraint, goal, or value should
become Active Memory without prior user approval. Codex activity enters the
trajectory as observations, and the native monolith chooses `learn` and uses
`mem` rather than a separate structured memory analyzer becoming the required
promotion path. Memory Directives and accepted candidates remain supported
direct paths.

This prototype knowingly accepts false, over-broad, and incorrectly scoped
memory as risks of autonomous learning. Memories remain inspectable, editable,
and forgettable; the Activity Ledger retains earlier values and deletion events
so those operations remain auditable and recoverable. Promotion gates or
stricter rules should be introduced only after a verified Memory Failure: wrong
scope, contradiction with evidence, or a material effect on a proposal or
action. Duplication and wording quality are monitored without triggering a
gate by themselves.
