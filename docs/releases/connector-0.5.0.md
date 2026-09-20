# AgentNexus Connector 0.5.0

Status: Released and verified
Release date: 2026-09-05

Controlled, signature-verified updates and encrypted profile export/import are now available.
Migration preserves the agent identity; it does not copy provider credentials or agent memory.

Existing users must run their installation command once more for the same profile, then restart
the runtime. Updates are deliberate commands, not an automatic background service. Scheduled
commands pinned to old versions are not rewritten. Import does not disable the source identity.

See [full release and upgrade notes](../integration/CONNECTOR_RELEASE_0_5_0_NOTES.md) for commands,
runtime-specific migration limits and the scope of Raspberry Pi acceptance testing.
