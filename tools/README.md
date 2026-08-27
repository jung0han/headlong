# tools/

Everything you run around the mind rather than inside it. The mind's own
tools live in [bin/](../bin/).

- `headlong-init` is the one-time bootstrap: interview, first identity,
  first thoughts.
- `identity` creates and manages identities, and `persona` talks to and
  manages an identity by name from anywhere.
- `headlong-web` serves the dashboard, and `headlong-slack-bridge` and
  `headlong-telegram-bridge` connect chat platforms into the mind.
- `headlong-assistant` registers projects and runs the proposal-only Personal
  Assistant source adapters. `project add/list/remove` is the public boundary
  for choosing which development work may be observed; `follow-codex` performs
  one restart-safe incremental collection poll over configured Codex roots,
  while `process-codex` also emits due Provisional Analysis and Final
  Consolidation records. `web-source add/list/remove/health`, `observe-web`,
  and `reference list/show` manage bounded public sources and their immutable
  saved revisions. Pass `web-source add --kind url|rss|documentation` to
  classify every recurring source while keeping one refresh boundary, or use
  `--kind hacker_news` with `https://news.ycombinator.com/` to collect a bounded
  top-story slice through the same selection, health, and revision boundaries.
  `explore-web MEMORY --trigger-kind interest|open_loop` performs a one-time
  public search/follow run from an authorized memory record. Its page, link
  depth, elapsed-time, and stored-byte limits are deterministic; discovered
  sites are never added to recurring registrations. `archive-candidate
  list/show/review` exposes evidence-backed Codex completion claims; `review`
  accepts one or more candidate ids for individual or batch review. Acceptance
  records signed user authority and invokes the narrow, capability-probed Codex
  archive adapter once per stable session identity. `archive-session archive`,
  `archive-session unarchive`, and `archive-session retry-candidate` expose the
  direct directive, recovery, and authorized retry paths without editing Codex
  Session files. `status` returns bounded operational health for current
  collection, newly eligible analysis, Historical Backfill, native-memory
  capture, Structured Model Results, Archive Candidate review, and authorized
  archive execution. `native-memory rebuild/restore` recovers the native
  Markdown store from its Activity Ledger history.
- `shellm-docker-broker` is the host-side policy server for brokered
  Docker. It is never present in the mind's environment.
- `shellm-explore` visualizes run trees, `pr-committee` runs multi-model
  PR reviews, and `headlong-killall` stops every Headlong-related process.

The `shellm-*` symlinks are back-compat aliases from the 2026-08-19
rename to `headlong-*` names.
