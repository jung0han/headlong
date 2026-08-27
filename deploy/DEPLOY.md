# Deploying headlong-web behind Cloudflare Zero Trust

Goal: a private URL (e.g. `https://agents.example.com`) where allow-listed
people can watch, start/stop, and chat with identities. Architecture:

```
boss's browser ── Cloudflare Access (SSO / email OTP)
                        │
                  Cloudflare Tunnel (outbound-only from the VM)
                        │
                 127.0.0.1:8080  headlong-web (systemd)
                        │ spawns
                 dispatchers + thinkers (own sessions, survive restarts)
```

No inbound ports are ever opened on the VM. Auth lives entirely in
Cloudflare Access; the app itself stays auth-free.

## 0. Prerequisites

- A domain on Cloudflare and access to the Zero Trust dashboard (free tier
  covers this seat count).
- A small Ubuntu 22.04/24.04 VM (EC2 t4g.small / Lightsail / etc.).
  **Treat it as burnable** — the agent executes arbitrary bash on it. Run
  nothing else there.
- A **dedicated, spend-capped** Anthropic API key. A runaway thinker loop
  is a token furnace; the cap is your real safety net.

## 1. Provision the app

```bash
git clone https://github.com/laude-institute/headlong.git
sudo bash headlong/deploy/setup.sh          # or run from your checkout
```

The script creates a `shellm` system user, clones the repo to
`/opt/shellm/app`, installs uv + bun, prebuilds the viewer, and starts the
`headlong-web` systemd service on `127.0.0.1:8080`. Pass `SHELLM_REPO` /
`SHELLM_BRANCH` env vars to deploy a fork or feature branch.

Then add the API key:

```bash
sudo -u shellm nano /opt/shellm/app/.env    # set ANTHROPIC_API_KEY=...
sudo systemctl restart headlong-web
curl -s localhost:8080/api/health           # {"status":"ok"}
```

## 2. Cloudflare Tunnel

In the Zero Trust dashboard: **Networks → Tunnels → Create a tunnel**
(Cloudflared connector). Copy the install command it shows and run it on
the VM — it installs `cloudflared` and a systemd service in one step:

```bash
curl -L https://pkg.cloudflare.com/cloudflared-stable-linux-amd64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb              # (arm64 build for t4g instances)
sudo cloudflared service install <TOKEN-FROM-DASHBOARD>
```

In the tunnel's **Public Hostname** tab, add:

- Subdomain/domain: `agents.example.com`
- Service: `HTTP://localhost:8080`

Visit the hostname — you should see the viewer (still unprotected; next
step fixes that, so do it immediately).

## 3. Cloudflare Access policy

**Access → Applications → Add an application → Self-hosted**:

- Application domain: `agents.example.com`
- Policy: Action **Allow**, Include → **Emails** → your email + your
  boss's email. (Or an IdP group if you have Google/Okta wired up.)
- Session duration: e.g. 1 week.

That's the whole login system. Anyone not on the list gets Cloudflare's
block page; people on it authenticate once and land in the viewer.

## 4. Lock CORS to the public hostname

Add a systemd drop-in (the main unit file is re-synced from the repo on
every deploy, so don't hand-edit it — drop-ins survive):

```bash
sudo mkdir -p /etc/systemd/system/headlong-web.service.d
sudo tee /etc/systemd/system/headlong-web.service.d/override.conf <<'EOF'
[Service]
Environment="HEADLONG_WEB_ALLOWED_ORIGINS=https://agents.example.com"
EOF
sudo systemctl daemon-reload && sudo systemctl restart headlong-web
```

(Default is `*`, which is fine on a laptop but pointless exposure on a
deployment. Comma-separate multiple origins if you need them. The
terraform deploy writes this drop-in automatically.)

## 5. Operating it

| Task | Command |
|---|---|
| Logs | `journalctl -u headlong-web -f` |
| Restart web server (agents keep running — the unit's `KillMode=process` signals only the server; stopping the service doesn't stop agents either) | `sudo systemctl restart headlong-web` |
| Stop every agent process | `sudo -u shellm /opt/shellm/app/tools/headlong-killall` |
| Update to latest code | see below; or click the navbar build stamp → "Pull latest & restart" (needs `HEADLONG_WEB_SELF_UPDATE=1` in the unit, which the shipped unit sets) |
| View-only mode | add `Environment="HEADLONG_WEB_READONLY=1"` to the override.conf drop-in (see §4) |

### Run a Personal Assistant continuously

Create or import the Observer Identity first, then enable its operational
target. The target groups the existing thinker unit with independently
restartable Codex and Web source bridges:

```bash
sudo systemctl enable --now headlong-assistant@observer.target
systemctl status headlong-thinkers@observer \
  headlong-assistant-codex@observer headlong-assistant-web@observer
```

Host-only source paths and cycle intervals belong in
`/etc/headlong/assistant.env` (the units read it when present):

```bash
CODEX_HOME=/home/operator/.codex
HEADLONG_CODEX_BRIDGE_INTERVAL_SECONDS=10
HEADLONG_WEB_BRIDGE_INTERVAL_SECONDS=900
HEADLONG_ASSISTANT_STORAGE_LIMIT_BYTES=1000000000
```

The root app `.env`, Observer `.env`, and identity `activate` file are then
loaded with the same precedence as `headlong-thinkers@`. Both bridges run the
direct-and-shellm model probe before persistent work begins. Cursor, projection,
native-memory audit, scheduler, and Reference state lives under the identity
rather than `/run`, so a service restart reuses it. The Codex bridge schedules
three independent lanes in priority order:

1. `active_collection` ingests deltas from current Codex Sessions.
2. `newly_eligible_analysis` runs Provisional Analysis after five idle minutes
   and Final Consolidation after thirty minutes or archival.
3. `historical_backfill` uses the remaining capacity, newest first, until every
   authorized historical session is covered.

Current work therefore remains responsive while a large Historical Backfill is
still running. Restarting a bridge resumes from durable cursors; it does not
restart the backfill from zero.

`GET /api/identities/.identities~observer/assistant/health` (or
`headlong-assistant --identity observer status`) reports the three Codex lanes,
native HeadLong Memory capture, Structured Model Result mode and failure count,
Archive Candidate review, archive execution attempts, allowlisted model-route
metadata, cursor offsets and digests, and Reference storage limits. These are
compact marker projections written when work occurs, so the health request does
not scan the Activity Ledger. It never returns source paths, credentials,
prompts, complete session events, adapter output, request errors, or Reference
bodies.

Interpret the operational sections separately. A nonzero Historical Backfill
is normal while `last_progress_at` continues to move. A structured-result
failure means malformed model output was rejected before persistence and is
retryable. A pending Archive Candidate is advisory and grants no execution
authority. Archive execution becomes possible only after the user accepts that
candidate or issues an explicit `archive-session archive` directive; it uses
Codex's archive interface and never edits session files directly. Observation
bridges mount the host home read-only under systemd hardening, while the native
actor remains proposal-only and cannot read the signed authority journal.

Stop or restart the whole group with the target, or operate one bridge without
disturbing the mind or the other bridge:

```bash
sudo systemctl stop headlong-assistant@observer.target
sudo systemctl restart headlong-assistant-codex@observer
journalctl -u headlong-assistant-codex@observer -n 100
```

Codex and Web bridge failures restart independently and trigger
`headlong-assistant-alert@...`. If Slack alerting is not configured, the
failure-open fallback is `/var/tmp/headlong-assistant-alert.log`; the alert
contains only allowlisted systemd fields, never model or source content.

Use the public recovery commands rather than editing durable files:

```bash
# Rebuild the native Markdown memory store from the Activity Ledger.
headlong-assistant --identity observer native-memory rebuild

# Restore one previously forgotten native memory by stable id.
headlong-assistant --identity observer native-memory restore MEMORY_ID

# Review and authorize an Archive Candidate, retry a failed authorized action,
# or reverse it through Codex's public interface.
headlong-assistant --identity observer archive-candidate list
headlong-assistant --identity observer archive-candidate review CANDIDATE_ID --state accepted
headlong-assistant --identity observer archive-session retry-candidate CANDIDATE_ID
headlong-assistant --identity observer archive-session unarchive SESSION_ID
```

Archive and unarchive execution runs in the separately hardened
`headlong-archive.service`. Web, CLI, and dashboard routes send an allowlisted
request to `/run/headlong-archive/archive.sock`; the service independently
verifies the signed Archive Directive or accepted Archive Candidate before it
invokes Codex. The web and source-bridge units have read-only access to the
default `CODEX_HOME`, and Observer thinkers cannot access the socket. The
boundary retains Authority Journal signing access, while each external Codex
child runs in a bubblewrap mount namespace with the journal masked and only
`CODEX_HOME` writable. Setup installs bubblewrap; update repairs a missing
installation. Assistant-service uninstall leaves this shared system package in
place.

The shipped boundary uses `CODEX_HOME=/opt/shellm/.codex`. When Codex state is
elsewhere, set one absolute `CODEX_HOME` in `/etc/headlong/assistant.env` before
running `deploy/update.sh`. The deploy renderer pins that same path into the
boundary's writable allowlist and the web/bridge read-only allowlists; it
refuses relative paths. Environment substitution does not make systemd
filesystem allowlists dynamic, so rerun the update after changing this value.

Native HeadLong learning is intentionally autonomous: the thinker may add a
memory without a review gate. Inspect, edit, forget, restore, or rebuild native
memory through the existing memory surfaces. Add a stricter promotion rule only
after a reproducible Memory Failure—wrong Knowledge Scope, contradiction with
evidence, or a material degradation of a proposal or action. Duplication or
awkward wording alone is quality feedback, not a Memory Failure. A
behavior-affecting report must identify the concrete downstream proposal or
native action step. Proposals retain their event Evidence Locators; native
actions retain a stable root-trajectory step locator plus a compact immutable
snapshot and content digest. Native memory capture is asynchronous, so the
Proposal event or action step may precede the later memory-capture ledger row;
the signed user report supplies the causal assertion instead of append order.

Before enabling a new Observer, render and verify the shipped units without
touching production:

```bash
bash tests/test_assistant_services.sh
```

The smoke renders `@SHELLM_HOME@` into a disposable unit directory and runs
`systemd-analyze verify` there when systemd tooling is available.

To remove only this supervision while preserving the Observer and normal
HeadLong services:

```bash
sudo bash /opt/shellm/app/deploy/uninstall-assistant-services.sh
```

**Updating:**

```bash
sudo -u shellm git -C /opt/shellm/app pull
sudo -u shellm rm -rf /opt/shellm/app/web/src/headlong_web/static  # forces frontend rebuild
sudo systemctl restart headlong-web
```

**Thinker dispatchers run as per-identity systemd units.** When the dash
(or the Slack bootstrap) starts an identity's thinkers, the dispatcher runs
under `headlong-thinkers@<identity>.service` in its own cgroup, so web-server
restarts and OOM kills cannot orphan or kill a mind. The web control plane
reaches systemd through `/usr/local/bin/headlong-thinkersctl`, a root-owned
wrapper that validates the action and identity name; the sudo rule in
`/etc/sudoers.d/headlong-thinkers` permits only that wrapper. All three pieces
are installed by `setup.sh` and re-synced by `update.sh`. Useful commands:
`systemctl status headlong-thinkers@audel` (who owns which processes),
`journalctl -u headlong-thinkers@audel` (start/stop history). A dispatcher
that dies on its own is restarted independently. Per-death and recovery notices
come from the thinker hooks; after repeated unclean deaths exhaust the start
limit, `headlong-thinkers-alert@audel` reports that the mind is staying down.

**Kill switches, in escalating order:** Kill All button in the UI →
`headlong-killall` on the box → `systemctl stop headlong-web` → stop the VM.

**Moving identities on/off the box:** every identity page has a Config →
Export button (and the home page has Export all / Import) producing a
portable `.shellm.tgz` — secrets (`.env`) and runtime state never leave the
machine. Use it to seed the deployment from a laptop identity, or as the
pre-demo backup. Two caveats:

- Importing an identity installs its thinkers — scripts that run when the
  identity is started. Only import archives you trust.
- Cloudflare's proxy caps request bodies at 100 MB on the free plan. For
  bigger archives, copy the file and use the CLI:

  ```bash
  scp big.shellm.tgz vm:/tmp/ && ssh vm \
      'sudo -u shellm env IDENTITY_DIR=/opt/shellm/app/.identities \
       /opt/shellm/app/tools/identity import /tmp/big.shellm.tgz'
  ```

  Uploads are also capped server-side via `HEADLONG_WEB_MAX_IMPORT_MB`
  (default 512).

## Migrating a pre-rename box (one time)

> Doing a *different* structural migration on a live box? Read
> `deploy/MIGRATIONS.md` first — it is the general playbook this section
> is one instance of.

Boxes provisioned before the headlong rename run `shellm-*` systemd units.
`deploy/update.sh` refuses to deploy onto them and points here, because the
cutover stops the identity dispatchers — a mind restart with a drain of up
to three minutes — and update.sh also runs unattended from the dash's
self-update button.

```bash
sudo -u shellm git -C /opt/shellm/app pull --ff-only   # land the code first
sudo bash /opt/shellm/app/deploy/migrate-units.sh --dry-run
sudo bash /opt/shellm/app/deploy/migrate-units.sh
```

It backs up every unit file, the drop-in directory, the sudo wrapper, the
sudoers rule, and the audit rules to `/var/backups/headlong-unit-migration`,
then swaps in the `headlong-*` units and brings the services back in the same
order a reboot would. If anything comes back wrong:

```bash
sudo bash /opt/shellm/app/deploy/migrate-units.sh --rollback
```

Rollback depends on the legacy `shellm-*` console-script aliases in the
pyproject files, so do not remove those until the migration has been stable
for a while.

What deliberately keeps the `shellm` name: the `/opt/shellm` path, the
`shellm` and `shellm-telegram` UNIX users, `~shellm/.shellm`, the
per-identity `.shellm/` subdirectory, the `/shellm-slack/env` SSM
parameter, and the `*.shellm.net` domains. Each is a physical move with its
own migration and none of them need to happen for the unit rename.

## Security notes

- The VM is the sandbox. Dedicated key with a spend cap, nothing else on
  the machine, snapshot before demos if you're nervous.
- `headlong-web` binds `127.0.0.1` and the tunnel is outbound-only, so the
  only path in is through Access. Don't "temporarily" bind `0.0.0.0`.
- Secrets: root key in `/opt/shellm/app/.env` (mode 600); per-identity
  overrides via the Config tab (stored in `<identity>/.env`).
- Optional: install Docker (`apt install docker.io`, add `shellm` to the
  `docker` group) so generated code runs in shellm's Docker sandbox
  instead of directly on the host.

## Quick demo alternative (no VM)

Run the tunnel from any machine you already have (dev box, spare Mac):
create the same dashboard tunnel + Access app, run
`cloudflared service install <TOKEN>` locally, point the public hostname
at `http://localhost:8080`, and start `./tools/headlong-web`. Same URL, same
login, zero infra — it just stops when your laptop sleeps.
