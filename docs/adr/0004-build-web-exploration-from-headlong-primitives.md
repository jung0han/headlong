# Build web exploration from HeadLong primitives

Web exploration starts as a Web Source Bridge and Reference Store composed with
HeadLong's trajectory, thinkers, and memory projections rather than by porting
the `personal-assistant-brain` Discovery state model. Preserve only minimum
correctness and safety boundaries—stable provenance, idempotent storage,
untrusted-content handling, bounded exploration, and no automatic Active Memory
promotion—and add further Discovery rules only when observed failures justify
them. The assistant may search and follow public links within per-run limits and
may save selected References without approval, but it may not automatically
register a recurring source or access authenticated or private-network content.
Selected content is stored as immutable Reference Revisions; rejected content
retains only enough identity and judgment evidence to avoid repeated work.
