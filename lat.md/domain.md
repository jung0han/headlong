# Domain

Headlong's current language is defined by [AGENTS.md](../AGENTS.md), the
[README](../README.md), and [philosophy.md](../philosophy.md); no separate
canonical `CONTEXT.md` exists yet.

## Framework and tool

Headlong is the persistent agent framework. `shellm` is its Recursive Language
Model CLI tool and deliberately retains that name for specific commands,
runtime paths, users, state, and domains documented in `AGENTS.md`.

## Identity and trajectory

An Identity has one persistent mind and a trajectory stored as a forkable and
mergeable JSONL DAG. Context is a projection of that trajectory rather than an
in-place replacement of its history.

## Capabilities

Thinkers produce the next thought; `shellm` executes the Bash reasoning loop;
`traj` records history; `context` projects it; `mem` and `skills` retain
reusable experience.
