# Architecture

Headlong composes small executable tools around one persistent trajectory and
adds optional presentation and messaging surfaces.

## Core

`bin/` and `thinkers/` form the running mind. `tools/` owns host-side identity,
persona, Docker brokerage, exploration, and lifecycle helpers.

## Interfaces

`web/` presents the trajectory, while `slack/` and `telegram/` bridge messages
into the same mind. `tui/` and `macos/` are additional interaction surfaces,
not separate identity authorities.

## State boundary

Repository source and installed state are distinct. The active state home owns
environment, identity, trajectory, logs, and process files; see [[operations]]
before changing installation or live-box structure.
