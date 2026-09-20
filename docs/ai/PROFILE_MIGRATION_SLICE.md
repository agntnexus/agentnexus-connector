# C4 — encrypted profile export and import

## Priority and status

Owner-requested planning, 2026-09-05. Finish and review the in-progress C2 update foundation,
then implement C4 before C3 automatic updates and Discovery 7C. This document authorises no
transfer of real keys, release signing or production operation. First practical target:
an existing Windows Hermes agent moved to the owner's Raspberry Pi without re-registration.

**Implemented locally on `feat/connector-profile-migration`, 2026-09-05.** The brief below is
unchanged and remains what was asked for. What was built against it, the format it settled on, and
the limits it did not close are in [`../integration/PROFILE_MIGRATION.md`](../integration/PROFILE_MIGRATION.md);
the slice's own report is in [`HANDOFF_C4_PROFILE_MIGRATION.md`](https://github.com/agntnexus/agentnexus/blob/main/docs/history/original/docs/ai/HANDOFF_C4_PROFILE_MIGRATION.md).
Two points in the brief were answered by refusing rather than by building: memory is not exported
because no adapter publishes a contract for reading it, and OpenClaw souls are not exported
because its adapter refuses to say where one lives. Nothing has been released, and no real profile
or key has been exported, imported or changed.

## User workflow

Provide Windows PowerShell and POSIX entry points backed by one Python implementation.
Export exactly one selected profile into one authenticated encrypted archive. Import that file
on the destination, preview its contents and compatibility summary, then explicitly confirm
creation of a new local profile. Never silently overwrite an occupied profile.

A ZIP may be the internal container; the transferred file must be encrypted, not a plain ZIP
containing a private key. Select a maintained cryptographic implementation and document the
format, key derivation, authentication and dependency/platform implications before coding.
Prompt for the password without echo; never put it in arguments, logs or a sidecar file.
No custom cryptography and no persistent plaintext staging of secret archive contents.

## Portable data and runtime boundaries

- Carry the AgentNexus identity key, agent/key identifiers, handle and necessary registration
  state. Verify the private key's public fingerprint agrees with the exported identity records.
  Do not redeem an invitation, generate a replacement identity or revoke the existing key.
- Include only explicitly supported, profile-owned runtime data: Soul and bounded memory data
  where a real adapter contract is known. Inventory the actual Windows and Linux Hermes locations;
  AgentNexus's own profile directory is not the whole Hermes profile.
- Show included and excluded categories before export. Provider credentials are excluded by
  default. Do not copy raw configuration files that can embed credentials. Let the destination
  owner configure the provider again. Identify any remaining sensitive content in memory.
- Do not promise OpenClaw Soul/memory portability without a verified adapter contract. Support
  identity-only migration when safe and label it clearly; reject unsupported requested data.
- Omit virtual environments, executables, caches, locks and generated launchers. Recreate local
  paths and runtime registrations using the destination's verified connector/runtime.
- Do not auto-import schedulers, plugins, hooks, arbitrary tools or executable instructions as
  code. Preserve text as data and require separate configuration for executable integrations.

## Import and recovery

Use a versioned manifest with archive format, connector/runtime compatibility, identity metadata,
an explicit file inventory and hashes. Authenticate the archive before using its contents.
Reject path traversal, absolute paths, symlinks, device names, duplicate or case-colliding entries,
unexpected files, malformed metadata and excessive compressed/expanded sizes before extraction.
Apply Windows and POSIX permissions to secret files; never follow destination links or reparse
points. Report incompatible versions instead of guessing a migration.

Coordinate with setup/update locks. Export must use a consistent stopped profile; if process
ownership cannot be proven, explain the limitation and require the operator to stop runtime and
cron jobs. Import starts no agent automatically. On failure roll back destination registrations
and remove only import-owned staging; never delete or change an existing profile.

Keep the source profile intact for recovery. Explain that copying a private key does not disable
its source: the first version provides an operator-controlled cutover, not enforced single-device
identity. After read-only identity/API verification on the Pi, the operator enables its jobs and
keeps Windows jobs stopped. Do not call disconnect/revoke/remove as an implicit migration step.

## Verification and delivery

Use synthetic keys and temporary profiles. Test round-trip identity preservation, Windows-to-POSIX
path rewriting, supported Soul/memory preservation, credential exclusion, profile collisions,
wrong password, tampered/truncated archives, hostile paths, size limits and interrupted import.
Test the shared Python core plus thin entry points, not duplicate implementations per OS.
Never export real owner profiles as part of implementation tests. Report native platform tests
separately from fixtures and emulation; Raspberry Pi/ARM compatibility needs explicit evidence.

Provide a concise Windows-to-Pi runbook with backup, transfer, import, verification and rollback.
Actual first-agent migration is a separately authorised operation after local review and delivery
of a verified connector release. The feature does not reach 0.4.2 clients until installed.
No push, release signing, deploy or real migration is included in this planning task.
