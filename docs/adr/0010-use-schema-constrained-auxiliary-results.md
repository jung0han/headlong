# Use schema-constrained results for bounded auxiliary analysis

Auxiliary analyzers that previously prompted a model to return JSON use the
strongest structured-output capability available through the configured model
route, preferring a strict JSON Schema. Every result is independently validated
against the owning local schema before it can be persisted or acted upon;
malformed output is discarded and recorded as an analysis failure. This change
does not replace HeadLong's native shell actor or make structured output the
memory-learning interface.

The model route owns capability detection so callers do not encode
provider-specific behavior. A route that offers only JSON object mode may use
it with local schema validation and a bounded retry, but the system never falls
back to accepting unvalidated prompt-shaped JSON.
