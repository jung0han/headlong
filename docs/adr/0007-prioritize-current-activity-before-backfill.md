# Prioritize current activity before historical backfill

The Observer processes active session deltas first, then sessions that have
newly become idle or archived, and uses remaining capacity for newest-first
Historical Backfill. Backfill eventually covers all authorized history, but it
may never block current activity; an eligible current source should surface its
first memory result within five minutes. This keeps the assistant responsive
while preserving eventual historical coverage.
