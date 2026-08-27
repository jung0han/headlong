# Migrating a live box

Playbook for changing something structural on a box that is running a mind
(renaming systemd units, moving paths, changing users). Written after the
2026-08-19 `shellm-*` -> `shelly-*` unit rename, and used again for
the 2026-08 `shelly-*` -> `headlong-*` rename (which deliberately skipped
the compat step — see the header of deploy/migrate-units.sh for what that
trades away).

Read this before designing the change, not after.

Adding the opt-in `headlong-assistant@<identity>.target` and its source bridge
units does not rename or replace an existing unit. Routine `update.sh` installs
the templates and restarts only already-running bridge instances; it does not
start a new Observer or restart `headlong-thinkers@`. Enable the target
explicitly after the Observer Identity and host source configuration exist.

## The one principle

**Make the code accept both the old and the new name, then flip the world
in a separate step.**

Every silent-failure risk in a rename comes from code that assumes exactly
one name. If the guard, the wrapper, and the status scripts all accept
either spelling, then the order of operations stops mattering, a
half-finished migration is not a broken state, and rollback is cheap. The
2026-08-19 rename shipped a guard matching BOTH `shelly-thinkers@` and
`shellm-thinkers@` for exactly this reason.

Do the compat work in the same commit as the rename. Retire it later, in
its own commit, after a soak.

## Loud failures vs silent failures

Loud failures are cheap: the unit will not start, the deploy aborts, you
notice in seconds. `systemd-analyze verify` and "does the ExecStart binary
exist" catch nearly all of them.

Silent failures are what actually hurt. Budget your review time here:

| Coupling | If you miss it | How to catch it |
|---|---|---|
| `bin/thinkers` cgroup self-stop guard matches the unit name | Guard stops recognizing its own cgroup. Self-stop protection is GONE and nothing says so. This is the 2026-08-12 self-kill failure mode. | Read `/proc/<dispatcher-pid>/cgroup` after the change and confirm it matches what the guard greps for. `migrate-units.sh` does this automatically. |
| Unit name in the systemd drop-in dir (`<unit>.service.d/`) | systemd ignores an orphaned drop-in dir without complaint. Box-local config (CORS origin, read-only mode) silently stops applying. | `systemctl show <unit> -p Environment` and look for the drop-in's values. |
| Command names quoted in prompts the agent reads (`bin/thinkers` wake-note, stop-refusal) | The mind is told to run a command that no longer exists, and only finds out mid-incident. | `grep` the prompt strings for command names; keep both wrapper names installed. |
| Console-script entry points in pyproject vs unit `ExecStart` | Unit cannot start, but only AFTER you have deleted the old units. | Check the venv: `ls <proj>/.venv/bin/ \| grep <name>` BEFORE cutting over. |
| Sudoers path vs wrapper path | The control plane silently loses the ability to start/stop dispatchers. | `sudo -n -l` as the service user. |
| Ops scripts (`deploy/scripts/status`, `watch`) | Report a healthy service as missing, or tail a unit that does not exist and just print nothing — reads as "quiet", not "wrong name". | Run them against the box on BOTH sides of the cutover. |

The pattern: anything that *greps*, *matches a string*, or *names a path in
a comment the agent reads* fails silently. Anything systemd parses fails
loudly.

## Phase structure

1. **Get the box current first, on the old world.** Deploy whatever is
   already on `main` before introducing the migration. This runs `uv sync`
   and the frontend rebuild, so the cutover changes exactly one variable.
   In 2026-08-19 this step is what installed the new renamed console
   scripts; without it the new units would have had no binary to exec.
2. **Land the code without applying it.** `git pull --ff-only` only. Nothing
   restarts. Confirm services are still up.
3. **Rehearse on the real box.** `--dry-run` against production state, not
   just a scratch layout. See "Rehearse where it counts" below.
4. **Cut over**, in its own supervised command.
5. **Finish the deploy** (`update.sh`) so the frontend rebuilds against any
   changed viewer code.
6. **Verify**, then clean up phantom state.

## Keep the dangerous step out of update.sh

`deploy/update.sh` runs unattended — the dash's build-stamp menu has a
"Pull latest & restart" button. Any step that stops a dispatcher must NOT
be reachable from there. Put it in its own script and make `update.sh`
hard-fail with instructions when it detects the un-migrated state.

A migration script should have:

- `--dry-run` that works **without root** (a rehearsal you cannot run is not
  a rehearsal) and that exercises the real code path — beware tests like
  `[[ -f "$SYSD/$unit" ]]` that are false during a dry run and silently skip
  the phase you most wanted to see.
- `--rollback` restoring from a backup the forward path wrote.
- Overridable paths (`SYSD`, `SUDOERS_D`, `AUDIT_D`, `LOCAL_BIN`,
  `BACKUP_DIR`) so it can be rehearsed against a scratch layout.
- Idempotence, and an early exit when there is nothing to do.

## Rehearse where it counts

The 2026-08-19 dry run passed locally against a simulated layout and still
found a real bug when run on the box: `systemctl is-enabled` prints
`disabled` **and exits non-zero**, so `$(systemctl is-enabled "$u" || echo
unknown)` produced a two-line value, embedding a newline in the backup
manifest and splitting one record into two. Harmless there (all units were
enabled) but it would have broken `--rollback` for a disabled-but-running
unit.

Local fakes encode what you *think* the tool does. Run the rehearsal
against production state.

## Back up before, at two depths

- **Cheap and targeted:** tar the identity dir, excluding `run/logs`
  (that is 465M of dispatcher logs; the state you care about is
  `trajectories/`, `memories/`, `mem/`, `notes/`, `skills/`, `thinkers/`).
  Use `nice`, and `--warning=no-file-changed` — the mind log is append-only,
  so a torn tail is still a valid prefix.
- **Deep:** an EBS snapshot. Its point-in-time is frozen at `StartTime` even
  while it shows `pending`, but it is not restorable until `completed`, and
  the first snapshot of a volume is a full copy (slow).

Be honest about what each protects. A unit rename never touches
`.identities/`, so these guard against an unclean dispatcher stop, not
against the rename.

## Verification checklist

- Every unit `active`; zero units of the old name remain.
- `systemctl --failed` is EMPTY. Deleting a unit file leaves a stale
  `not-found/failed` entry under the old name — the first thing incident
  triage reads. `systemctl reset-failed <old-name>` clears it; do templates
  and instances.
- Dispatcher cgroup matches what the guard greps for.
- Dispatcher log shows a **clean** stop (`caught SIGTERM from a deliberate
  stop — exiting clean`). An unclean exit means `Restart=on-failure` and
  the Slack death alert are about to fire.
- No alert fallback log (`/var/tmp/headlong-thinkers-alert.log`) — its
  presence means a Slack post failed.
- Trajectory has a fresh step with a timestamp after the restart.
- Drop-in values still applied.
- Health endpoint ok, deployed commit is what you expect.

## Tell the mind what happened

A restart shorter than `THINKERS_WAKE_NOTE_MIN_GAP` (300s) appends **no**
wake-up note, so the agent has no idea it was restarted. On 2026-08-19 the
gap was ~224s and Audel was never told. If the downtime matters, say so in
chat.

## Gotchas that cost time

- `export SHELLM_TF_STACK=terraform-slack` before any `deploy/scripts/*`
  command. Without it `lib.sh` defaults to the `deploy/terraform` stack — a
  different instance — and you get `InvalidInstanceId`.
- Run `deploy/scripts/*` from the repo root or use absolute paths.
- Reconcile the box's own commits before deploying; the box repo carries
  the agent's unreviewed work (`deploy/scripts/audel-commits reconcile`).
  Check for collisions between incoming files and the box's dirty paths —
  a `--ff-only` pull only fails if they overlap.
- Never read the mind log whole; tail only (800M+).
