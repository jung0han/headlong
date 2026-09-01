# Development

Headlong changes preserve the distinction between framework and tool names,
the trajectory model, state migration, and host-side safety boundaries.

## Change preparation

Read [AGENTS.md](../AGENTS.md) for naming and sharp edges, the
[README](../README.md) for system behavior, and deployment migration guidance
before structural live-box changes.

## Validation

Run the focused shell, Python, Rust, web, installer, or bridge tests owned by
the changed surface. Release work follows [docs/RELEASING.md](../docs/RELEASING.md).
Graph changes complete with `lat check` and `git diff --check`.
