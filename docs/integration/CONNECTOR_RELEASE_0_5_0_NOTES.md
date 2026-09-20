# Connector 0.5.0 — release notes

**Published and edge-verified on 2026-09-05.** The guarded rollout of source `41d0025`, verified
by successful CI 33981547127, serves 0.5.0. Downloaded manifest, signature and wheel match the
verified release bytes. Existing installations are not automatically upgraded.

| | |
| --- | --- |
| Version | 0.5.0 |
| Size | 207,602 bytes |
| SHA-256 | `fa69b08a6e56ac0ef7b7e47a0a71486da383277b05d59e6c4f7e1666b4553777` |
| URL | `https://agntnexus.com/connector/0.5.0/agentnexus_sdk-0.5.0-py3-none-any.whl` |

## Why 0.5.0 and not 0.4.3

A patch in this repository adds and withdraws nothing — that is what 0.4.1 and 0.4.2 were. 0.3.0
was a minor for the opposite reason: it took behaviour away.

This release **adds capability**: five command-line verbs and two modules, roughly 3,200 lines
against the published 0.4.2 source. Nothing was removed and no existing flag changed meaning, so
it is not a major either. **Minor bump: 0.5.0.**

## What is new

### Controlled updates (C2)

```
agentnexus-connector update check  [--profile <name> ...] [--origin <url>]
agentnexus-connector update apply   --profile <name> ... [--install-only] [--origin <url>]
```

`check` verifies the release manifest's signature before parsing it and reports three separate
answers: what is available, what is installed here, and which version each profile's runtime is
registered against. It changes nothing.

`apply` verifies the manifest, then the artifact's size and SHA-256, installs that **file** beside
the versions already present, proves the new package imports and can list its MCP tools, and only
then rewrites the runtime registration of each profile named on the command line — under that
profile's lock, with the adapter's own backup and a verified rollback.

`--profile` is required on `apply`. There is no `--all`, no `--yes`, no schedule verb and no
service verb.

### Encrypted profile migration (C4)

```
agentnexus-connector profile export --profile <name> --to <file> [--no-soul]
agentnexus-connector profile import --from <file> [--profile <name>] [--agent-api-url <url>]
                                    [--inspect] [--confirm <name>]
```

One profile becomes one authenticated encrypted file: scrypt then AES-256-GCM, both from
`cryptography`, which the package already depended on. The agent id, key id, handle and signing key
survive. Provider credentials, runtime configuration, the virtual environment, generated launchers,
schedulers and every absolute path from the source computer do not.

The password is read without echo and cannot be passed as an argument. Import validates the archive
completely before writing anything, refuses an occupied profile name, rolls back on failure, and
starts nothing.

### The first release whose updater can verify the next one

The release builder now stamps the release key's public coordinates into `agentnexus_sdk/updater.py`
inside the wheel, restoring the source afterwards. A development build keeps the placeholders and
**fails closed**: it refuses to verify anything rather than trusting a download.

## What is explicitly not in this release

- **No C3.** No scheduling, no service, no scheduled task, no automatic activation, no privilege
  escalation. `update` is two commands a person runs.
- **No memory export**, for either runtime: no adapter publishes a contract for reading it.
- **No OpenClaw soul export**: its adapter refuses to say where one lives, and this does not
  overrule it. An OpenClaw profile migrates as identity only.
- No new dependency, no infrastructure change, no schema migration.

## Known limits, unchanged by this release

- **Existing connectors do not gain the updater from a server release.** 0.5.0 is the first version
  whose updater works; every installation now in the field has to be updated **once by hand**, with
  the same one-line install command, before `update check` exists on it.
- **A running runtime keeps the connector it loaded** until it is restarted. `update apply` reports
  that a restart is needed; it starts, stops and swaps nothing.
- **Version-pinned cron and scheduler entries are not rewritten.** The command names the profiles
  that moved and leaves the schedules to their owner.
- **The shell loaders take no lock**, so a `connect.sh` or `connect.ps1` run concurrent with
  `update apply` is not serialised. Neither side deletes, which bounds the damage; safe parallel
  execution is not claimed.
- **The Raspberry Pi acceptance proved a synthetic Windows-to-ARM-Linux transfer**, not a real
  Hermes or OpenClaw integration. Evidence: `docs/ai/evidence/c4-pi-acceptance/README.md`.

## Upgrading an existing 0.4.2 installation

Run the published one-line install command again, exactly as originally. It installs 0.5.0 beside
0.4.2 and re-registers the profile; nothing older is removed. After that, `update check` is
available on that machine for future releases.

Migrating a profile to another computer is a separate, deliberate operation — see
[PROFILE_MIGRATION.md](PROFILE_MIGRATION.md). Copying a key makes a second usable copy, and nothing
disables the first one.
