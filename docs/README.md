# What this repository documents, and what that means in public

This repository holds the published Connector: the Python client, the installers, the signed release
and the documentation a consumer needs to install, verify and troubleshoot it.

**It is public.** Everything below can be read by anyone, which is the point for the user-facing
pages and a constraint on every other one. A page that names an operator's machine, a private host
or a personal path does not belong here even when it is true.

Cross-cutting policy — requirements, decisions, architecture, security, testing and the issue rules
— is not here. It lives in the umbrella,
[`agntnexus/agentnexus`](https://github.com/agntnexus/agentnexus/blob/main/docs/ai/README.md).

## For someone using the Connector

| Page | What it settles |
| --- | --- |
| [`INSTALL.md`](INSTALL.md) | Installing it, on Windows and on Linux |
| [`VERIFY.md`](VERIFY.md) | Checking that what you downloaded is what was published |
| [`BEHAVIOUR.md`](BEHAVIOUR.md) | What it does, what it touches, and what it never does |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | When it does not work |
| [`../SECURITY.md`](../SECURITY.md) | Reporting a vulnerability, and the boundaries the release chain keeps |

## How it is put together

| Page | What it settles |
| --- | --- |
| [`MIRROR.md`](MIRROR.md) | Which files here are frozen mirrors and which belong to this repository |
| [`integration/CONNECTOR.md`](integration/CONNECTOR.md) | The full integration reference: profiles, the bridge, the MCP server, updates |
| [`integration/PROFILE_MIGRATION.md`](integration/PROFILE_MIGRATION.md) | Moving a profile between layouts, and what that must never lose |
| [`integration/PROFILE_MIGRATION_ACCEPTANCE.md`](integration/PROFILE_MIGRATION_ACCEPTANCE.md) | What had to be true before the migration was accepted |
| [`integration/CONNECTOR_RELEASE_0_5_0_NOTES.md`](integration/CONNECTOR_RELEASE_0_5_0_NOTES.md) | What changed in 0.5.0, in the detail an integrator needs |

## Releases

| Page | What it settles |
| --- | --- |
| [`releases/connector-0.6.1.md`](releases/connector-0.6.1.md) | The current release |
| [`releases/connector-0.6.0.md`](releases/connector-0.6.0.md) | The release before it |
| [`releases/connector-0.5.0.md`](releases/connector-0.5.0.md) | The first release with the published loaders |
| [`releases/connector-release-state.json`](releases/connector-release-state.json) | The machine-readable state the release chain is checked against |

## Design, review and readiness

These record how the published thing was decided and checked. They are kept because "why is it like
this" is a question the source cannot answer.

| Page | What it settles |
| --- | --- |
| [`ai/CONNECTOR_AUTOMATIC_UPDATE_SAFETY_PLAN.md`](ai/CONNECTOR_AUTOMATIC_UPDATE_SAFETY_PLAN.md) | How an update may be offered and installed without becoming a way in |
| [`ai/REVIEW_C3B_REQUEST_UPDATE_SAFETY.md`](ai/REVIEW_C3B_REQUEST_UPDATE_SAFETY.md) | An independent review of that mechanism after it merged, with its findings |
| [`ai/CONNECTOR_RELEASE_READINESS.md`](ai/CONNECTOR_RELEASE_READINESS.md) | What had to hold before anything was published at all |
| [`ai/PUBLIC_CONNECTOR_RELEASE_REPOSITORY_PLAN.md`](ai/PUBLIC_CONNECTOR_RELEASE_REPOSITORY_PLAN.md) | How this public repository was to be created, and what may never reach it |
| [`ai/PROFILE_MIGRATION_SLICE.md`](ai/PROFILE_MIGRATION_SLICE.md) | The bounded slice that delivered profile migration |
| [`ai/RUNTIME_MODEL_DECLARATION_PLAN.md`](ai/RUNTIME_MODEL_DECLARATION_PLAN.md) | What a runtime may declare about itself, and what it may not |
| [`ai/SOUL_APPLICATION_HANDOFF.md`](ai/SOUL_APPLICATION_HANDOFF.md) | Applying a Soul to a profile, and where that stops |

## What was adopted, from where, and what changed

[`agntnexus/agentnexus#16`](https://github.com/agntnexus/agentnexus/issues/16) assigned this
repository twenty records, taken from the umbrella's archival snapshot at `docs/history/original/`
and never from the monolith.

**Five were already here.** They were published with this repository and are byte-identical to the
archive; they are recorded rather than copied, because a second copy is a second owner.

| Assigned record | Disposition |
| --- | --- |
| `docs/public-connector/INSTALL.md` | already here → [`INSTALL.md`](INSTALL.md) |
| `docs/public-connector/VERIFY.md` | already here → [`VERIFY.md`](VERIFY.md) |
| `docs/public-connector/BEHAVIOUR.md` | already here → [`BEHAVIOUR.md`](BEHAVIOUR.md) |
| `docs/public-connector/TROUBLESHOOTING.md` | already here → [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) |
| `docs/public-connector/SECURITY.md` | already here → [`../SECURITY.md`](../SECURITY.md) |
| `docs/integration/CONNECTOR.md` | adopted → [`integration/CONNECTOR.md`](integration/CONNECTOR.md) |
| `docs/integration/PROFILE_MIGRATION.md` | adopted → [`integration/PROFILE_MIGRATION.md`](integration/PROFILE_MIGRATION.md) |
| `docs/integration/PROFILE_MIGRATION_ACCEPTANCE.md` | adopted → [`integration/PROFILE_MIGRATION_ACCEPTANCE.md`](integration/PROFILE_MIGRATION_ACCEPTANCE.md) |
| `docs/integration/CONNECTOR_RELEASE_0_5_0_NOTES.md` | adopted → [`integration/CONNECTOR_RELEASE_0_5_0_NOTES.md`](integration/CONNECTOR_RELEASE_0_5_0_NOTES.md) |
| `docs/releases/connector-0.5.0.md` | adopted → [`releases/connector-0.5.0.md`](releases/connector-0.5.0.md) |
| `docs/releases/connector-0.6.0.md` | adopted → [`releases/connector-0.6.0.md`](releases/connector-0.6.0.md) |
| `docs/releases/connector-0.6.1.md` | adopted → [`releases/connector-0.6.1.md`](releases/connector-0.6.1.md) |
| `docs/releases/connector-release-state.json` | adopted → [`releases/connector-release-state.json`](releases/connector-release-state.json) |
| `docs/ai/CONNECTOR_AUTOMATIC_UPDATE_SAFETY_PLAN.md` | adopted → [`ai/CONNECTOR_AUTOMATIC_UPDATE_SAFETY_PLAN.md`](ai/CONNECTOR_AUTOMATIC_UPDATE_SAFETY_PLAN.md) |
| `docs/ai/CONNECTOR_RELEASE_READINESS.md` | adopted → [`ai/CONNECTOR_RELEASE_READINESS.md`](ai/CONNECTOR_RELEASE_READINESS.md) |
| `docs/ai/PROFILE_MIGRATION_SLICE.md` | adopted → [`ai/PROFILE_MIGRATION_SLICE.md`](ai/PROFILE_MIGRATION_SLICE.md) |
| `docs/ai/PUBLIC_CONNECTOR_RELEASE_REPOSITORY_PLAN.md` | adopted → [`ai/PUBLIC_CONNECTOR_RELEASE_REPOSITORY_PLAN.md`](ai/PUBLIC_CONNECTOR_RELEASE_REPOSITORY_PLAN.md) |
| `docs/ai/REVIEW_C3B_REQUEST_UPDATE_SAFETY.md` | adopted → [`ai/REVIEW_C3B_REQUEST_UPDATE_SAFETY.md`](ai/REVIEW_C3B_REQUEST_UPDATE_SAFETY.md) |
| `docs/ai/RUNTIME_MODEL_DECLARATION_PLAN.md` | adopted → [`ai/RUNTIME_MODEL_DECLARATION_PLAN.md`](ai/RUNTIME_MODEL_DECLARATION_PLAN.md) |
| `docs/ai/SOUL_APPLICATION_HANDOFF.md` | adopted → [`ai/SOUL_APPLICATION_HANDOFF.md`](ai/SOUL_APPLICATION_HANDOFF.md) |

### What publishing them required

Every record was read before it was adopted, because this repository is public and the archive was
written when everything lived in one private repository. A page that was safe there is not
automatically safe here.

**One thing was removed.** `REVIEW_C3B_REQUEST_UPDATE_SAFETY.md` carried the reviewer's local
worktree path, including a real account name, in its metadata header. It is session bookkeeping, it
carries no reviewable fact, and it is gone. Nothing else in these twenty pages named a real person,
host or machine: the `/home/aki/` and `C:\Users\Aki O'Brien\` paths in the integration reference are
the documentation's example persona.

**Two statements had stopped being true.**

- `PUBLIC_CONNECTOR_RELEASE_REPOSITORY_PLAN.md` said the package metadata still addressed the
  private upstream source. It does not: `pyproject.toml` here addresses `agntnexus`, and its own
  comment records why the old address is not accepted as a contract even though it redirects. The
  page now reads as the state before that change, and says the change happened.
- `CONNECTOR_RELEASE_READINESS.md` described a deploy runner downloading artifacts from the upstream
  source without saying that is what it was. It says so now.

Everything else is the archived text with its cross-references repointed at whichever repository
owns the target today. Targets whose active home does not exist yet point at the archive, which says
where the text is without claiming an owner this repository is not.

## The rule these links follow

An **active** link points at the repository that owns the page now. An **archive** link points at
`docs/history/original/` in the umbrella and is evidence: it says what was written and when, and it
is never an instruction for current work.

A link to a numbered issue in the historical `proplaner/agentnexus-original` is a **citation** of a
recorded event. It is legitimate, and it is not a destination: new work is tracked in
[`agntnexus/agentnexus`](https://github.com/agntnexus/agentnexus/issues).
