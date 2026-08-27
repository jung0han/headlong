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
  one restart-safe incremental collection poll over configured Codex roots.
- `shellm-docker-broker` is the host-side policy server for brokered
  Docker. It is never present in the mind's environment.
- `shellm-explore` visualizes run trees, `pr-committee` runs multi-model
  PR reviews, and `headlong-killall` stops every Headlong-related process.

The `shellm-*` symlinks are back-compat aliases from the 2026-08-19
rename to `headlong-*` names.
