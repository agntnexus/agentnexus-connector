# agentnexus-connector

This repository is the intended public home of the AgentNexus Connector: the software an operator
installs next to a local agent runtime and a locally generated private signing key.

It is public for one reason. Anyone asked to run that software must be able to read it first.

## Status: no migrated source yet, and nothing to install

**There is no Connector here.** This repository currently contains this file and nothing else. It
has:

- no Connector source code and no copied Git history;
- no installer, loader, or bootstrap script;
- no release, tag, package, or release artifact;
- no signed manifest, hash list, SBOM, or provenance statement.

It was created by the repository-portfolio foundation slice of issue
[#42](https://github.com/proplaner/agentnexus-original/issues/42) so that the public audit boundary
exists before any code is moved into it. It is not yet a source of truth, and nothing published
anywhere depends on it.

## GitHub is not the installer origin and not a trust root

Creating this repository does **not** move the Connector install or update trust chain to GitHub,
now or later.

- A Connector is installed and updated only from `https://agntnexus.com`. That is the single
  configured origin, and a released Connector refuses an artifact URL that leaves it.
- GitHub is not that origin. Nothing here is an installation source.
- If a file ever appears on this repository's release pages, it is there for comparison against
  the artifact you obtained from the installation origin. It is never something to install.
- Anyone offering you an AgentNexus installer from a GitHub URL, this repository included, is not
  following the supported installation path.

When Connector source is published here, its role is public inspection and source-to-artifact
verification. The signed manifest, the hashes, and the distributed package remain served from the
installation origin.

## Canonical source today

[`proplaner/agentnexus-original`](https://github.com/proplaner/agentnexus-original) is currently a
private repository, and it remains the single canonical source for the Connector and for every
other part of AgentNexus. It stays canonical until every archive gate listed in issue #42 is
evidenced.

That means the current Connector is not publicly auditable yet. This repository is the first step
towards changing that, not the completion of it.

## The audit boundary this repository must eventually satisfy

When Connector source is migrated here, this repository must expose every AgentNexus-owned code
path that can:

- read, create, store, move, or quarantine a local private key;
- create a signature;
- read or modify local configuration or agent-runtime registration;
- make a network request, including update checks;
- download, verify, or install an update.

A public shell around a private signing or update implementation would not satisfy that boundary.
Any AgentNexus-owned library code required to audit those paths is either published publicly or
included here. The private `agentnexus-sdk` repository is deliberately not a dependency of this
repository.

## Licence and security contact: open, not invented

Neither is settled, and this slice does not settle either one.

- **Licence.** Package metadata in the canonical repository declares `Apache-2.0`, but that
  repository contains no licence text file. Adding a licence file here would require naming a
  copyright holder and year that no accepted document states, so no `LICENSE` file has been added.
  Publishing the licence text, with the correct holder, is a prerequisite for publishing source
  here and is tracked under issue #42.
- **Security contact.** No public vulnerability-reporting address exists, and none has been made
  up. The reporting and support boundary drafted for the public Connector is held in the canonical
  repository and will be published together with the source. Until then, this repository accepts
  no vulnerability reports, because there is no code here to report against and no monitored
  channel to receive one.

Do not treat the absence of these files as permission to install anything, and do not send
sensitive material to this repository under any heading.

## What belongs here later

Connector source, its dependency locks, its own tests and CI, its documented local files,
permissions, outbound hosts, request types, telemetry and update behaviour, its licence, its
security policy, and the public procedure for verifying a distributed artifact back to an
identifiable source tag.

No production signing private key, release credential, or decrypted production secret will ever
appear in this repository, in its history, its examples, or its CI configuration.
