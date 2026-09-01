# Operations

Headlong operations preserve installed identity state while distinguishing one
installer snapshot from live process evidence.

## Installation

The installer supports unattended configuration through environment variables
and writes `status.json` after each run. Live process truth remains in the PID
files named by that snapshot.

## State and migration

The current state root defaults to `~/.headlong` with documented legacy
fallbacks. Read [deploy/MIGRATIONS.md](../deploy/MIGRATIONS.md) before changing
live-box paths, users, units, domains, or retained `shellm` coordinates.

## Secrets

Pass provider keys through the installation environment or state-home
configuration. Keep their values outside Git, Lat, logs, examples, and test
fixtures.
