# Public Connector Release Repository Plan

## Decision and goal

AgentNexus connector development remains in the private `AIExperiment`
repository. Finished, reviewed releases will additionally publish a deliberately
limited source snapshot, documentation and release evidence to the public
repository:

`https://github.com/proplaner/agentnexus-connector`

The goal is independently reviewable release transparency for people who run
the Connector. It is not an open mirror of the platform, an invitation to
publish development history, or a change to the Agent API's current Tailnet
boundary.

The public repository is **not created or populated by this planning document**.
Creation and its first publication are CPR-2 work, gated by CPR-1's audit.

## Existing security model that must remain true

The current loaders originate at `https://agntnexus.com`, carry the release
signing public key, verify the exact bytes of a signed release manifest before
parsing it, and then verify the pinned wheel's size and SHA-256 before
installation. They deliberately refuse cross-origin artifact URLs.

CPR-1 and CPR-2 preserve that model:

- `agntnexus.com` remains the sole installer download origin and the signed
  manifest remains the installer authority;
- GitHub is a public inspection and evidence mirror, not a new trust root or
  automatic update source;
- no loader, redirect, DNS, TLS, Tailnet, Agent API, release signing key or
  release-key rotation behaviour changes; and
- a matching GitHub asset is useful evidence, but never substitutes for the
  installed artifact's manifest-signature and digest verification.

Making GitHub an installer origin would alter the same-origin fail-closed
boundary and is a separate security decision after this programme.

## Public repository contents

Every public release must contain only a reviewed allowlist:

- the Connector/SDK source required to build and inspect that release;
- package metadata, an approved Apache-2.0 license text, notices and public
  dependency information;
- generic installation, upgrade, removal, security and troubleshooting docs;
- a versioned changelog/release notes and a support/security-contact policy;
- the wheel, signed `connector-release.json`, detached signature, SHA-256
  checksums, public release-key fingerprint and release evidence; and
- after CPR-3, an SBOM and build-provenance attestation.

It must exclude the private Git history and every file outside the allowlist,
including infrastructure/Kubernetes material, deployment and operator
runbooks, Tailscale hostnames, invitations, profiles, fixtures with identities,
production configuration, secrets, signing private keys, CI credentials and
internal test evidence. A hostname or public key being non-secret is not a
reason to publish it when a generic public document is sufficient.

The package presently declares Apache-2.0 metadata. Before the first public
source publication, the owner must confirm that this license and all included
documentation/assets are authorised for publication and that required third
party notices are present.

## CPR-1: export policy and audit

**Purpose:** Prove that a safe, reviewable public release export can be made;
do not create the public repository or publish any material.

1. Define the exact source/document allowlist and denylist in code, rather than
   copying directories by convention.
2. Produce a disposable candidate export for one released or release-candidate
   Connector version and inventory every included path.
3. Run secret/key armour, credential, private-host/profile and forbidden-path
   scans against both the export and its Git history to be published.
4. Check that package metadata's Source and Documentation links can point to
   the public repository without falsely claiming that all platform code is
   public.
5. Establish the release evidence schema: connector version, public source tag,
   wheel SHA-256/size, signed-manifest SHA-256, signing-key fingerprint, build
   input identifier and publication time.
6. Document the handling of a failed audit, a withdrawn release, security
   advisories and a correction. Already published signed artifact bytes remain
   immutable; never replace a wheel under an existing version.

**Acceptance:** a candidate export contains only the explicit allowlist, scans
are demonstrated with planted negative cases, the current signed artifact
digest can be matched without downloading an untrusted package, and an owner
has approved the licence scope. No GitHub repository, source, wheel or release
is published in this slice.

### What CPR-1 implemented, and what it found

Implemented in `scripts/connector_public_export.py`, issue
[#8](https://github.com/proplaner/agentnexus-original/issues/8), with 90 cases in
`packages/agent-sdk-python/tests/test_connector_public_export.py`.

**The allowlist is seven entries**, each naming one file or one narrow glob with
the reason an external Connector user needs it: the released package source and
`py.typed`, `pyproject.toml`, the package `README.md`, and the three loaders
under `installers/`. Reviewing an export is reading that list. The package's own
`tests/` are excluded by construction — their fixtures carry synthetic
identities and profile layouts.

**One of the two findings that blocked the public documentation is now
resolved; the other is not.**

1. ~~`docs/integration/CONNECTOR.md` cannot be exported as it stands.~~ Still
   true, and no longer blocking: four purpose-written documents now exist under
   `docs/public-connector/` (issue
   [#14](https://github.com/proplaner/agentnexus-original/issues/14)), and the applicant
   guide remains off the allowlist and refused by the content audit.
2. This repository contains **no `LICENSE` file at all**, while
   `pyproject.toml` declares `license = "Apache-2.0"`. The export therefore
   cannot carry the licence text its own metadata promises. The owner must
   supply and approve the Apache-2.0 text and any `NOTICE`; the tooling reports
   this and deliberately authors no legal text.

**Package metadata to repoint.** When this was written, `[project.urls]` `Documentation` and
`Source` both addressed the private upstream source `proplaner/agentnexus-original`. CPR-2 was to
change them as part of a new reviewed release; CPR-1 reported and changed nothing.

> **This happened.** `pyproject.toml` in this repository now addresses `agntnexus`, and its own
> comment records why the old address is not accepted as a contract even though it redirects.

### The public documentation — issue [#14](https://github.com/proplaner/agentnexus-original/issues/14)

Four documents under `docs/public-connector/`, written for this export rather
than copied from anything. **Nothing is published**, and
`proplaner/agentnexus-connector` is still not created.

| Source                                       | Export path                | Subject                                                    |
| -------------------------------------------- | -------------------------- | ---------------------------------------------------------- |
| `docs/public-connector/INSTALL.md`           | `docs/INSTALL.md`          | What the Connector is and is not, prerequisites, install, upgrade, removal |
| `docs/public-connector/VERIFY.md`            | `docs/VERIFY.md`           | Checking a release against the signed manifest, with no credential |
| `docs/public-connector/TROUBLESHOOTING.md`   | `docs/TROUBLESHOOTING.md`  | Offline diagnostics, and what may never be pasted into a report |
| `docs/public-connector/SECURITY.md`          | `SECURITY.md`              | Security and support boundary, and private vulnerability reporting |

`SECURITY.md` lands at the root because that is where GitHub reads a repository's
security policy from.

**The allowlist gained four exact paths and no glob.** `docs/` also holds the AI
operating context, the operator runbooks, the release notes and the private
applicant guide; a `docs/**` entry would export all of it the first time somebody
adds a file. A test asserts the only documents on the allowlist are these four,
and that no entry under `docs/` is a glob.

**Every documented command is checked against the thing that runs it** — the two
loaders and the connector's own argparse — so a renamed flag fails a test rather
than staying correct-looking in prose. One thing is deliberately absent because
it does not exist: there is no POSIX remove loader, so removal is documented as
the connector's own command on every platform.

The export now writes **33 files** rather than 29.

**History.** CPR-2 publishes a snapshot under an immutable tag and imports no
private history, so there is no history to push. The audit nonetheless scans
every historical version of the allowlisted paths — 136 blobs on the CPR-1 base
— because a secret committed once and removed is exactly what a snapshot audit
cannot see. That scan is clean.

**Two definitions the evidence schema needs, now fixed.** The
`public_signing_key_fingerprint` is the SHA-256 over the two published
release-key coordinates, sorted and concatenated as lowercase hex; the private
key is not involved. The `build_input_identifier` is the SHA-256 over the
export's own `SHA256SUMS`: it identifies the exported source set exactly and
makes no claim that building it reproduces the released wheel. That claim is
CPR-3's, after CPR-3 measures it.

**Advisory versus refusal.** "A hostname or public key being non-secret is not a
reason to publish it" is a review judgement, and the audit reports it as an
advisory a reviewer must read rather than as a refusal. Naming the private
network in prose — `runtimes.py` describes the Tailnet preflight in its module
docstring — is advisory; a Tailnet *hostname* or a CGNAT address is a refusal.

## CPR-2: public repository and mirrored releases

**Purpose:** Create `proplaner/agentnexus-connector` and publish the first
audited release after CPR-1 passes.

1. Create the public repository with a minimal public README, license,
   `SECURITY.md`, contribution/support boundary and release verification guide.
2. Publish only the reviewed export under an immutable version tag; do not push
   private history or use `git push --mirror`.
3. Create a GitHub Release containing the approved source tag, public notes,
   wheel, manifest, detached signature, checksums and release evidence.
4. Prove byte equality between the GitHub wheel and the corresponding signed
   `agntnexus.com` artifact, and verify the signed manifest with the public key
   independently of the installer.
5. Add branch/tag/release protection appropriate to the owner account. Release
   publishing must require the same explicit review as the private release;
   GitHub publication never obtains the signing private key.
6. Update public package URLs and generic docs only as part of a new reviewed
   Connector release. Do not retroactively rewrite a historical release.

**Acceptance:** a stranger can inspect the public source tag, compare the
published wheel checksum to the signed manifest and verify the signature with
the documented public key. The existing installer still accepts only the
`agntnexus.com` chain, and a test proves it refuses a GitHub/cross-origin
artifact URL.

### What CPR-2 published, and how it differs from the plan above

Issue [#80](https://github.com/proplaner/agentnexus-original/issues/80).
`proplaner/agentnexus-connector` is public and carries the Connector at merge
commit `93130072`: 82 commits, 41 files, its own CI green.

**It publishes history, not a snapshot.** The plan said CPR-2 would publish a
snapshot under an immutable tag and import no private history. That was decided
before anyone had measured whether the history was safe to publish, and it is:
`git filter-repo` restricted to the published paths yields 74 commits whose 166
blobs and every commit message were scanned for key armour, age/SOPS envelopes,
GitHub/Slack/AWS token shapes, tailnet hostnames, invitation values, literal
credentials, Actions secret expressions and personal email domains, with **zero
refusals**. So `git log` and `git blame` answer honestly in the public
repository, which is worth more to an auditor than a flattened import. Author
identities were normalised to one GitHub noreply address by `--mailmap`; nothing
else about the commits was rewritten, and the tree object was unchanged by that
pass.

**The two owner decisions CPR-1 recorded are settled.** The owner supplied the
verbatim Apache-2.0 text with `Copyright 2026 proplaner` and decided no `NOTICE`
is required; `LICENSE` is on the allowlist and is copied, never authored, which
a byte-comparison test pins. `[project.urls]` now addresses the public
repository.

**A fifth public document exists.** `BEHAVIOUR.md` states the local files and
their modes, the single origin, what a request carries, that there is no
telemetry, and that updates are noticed rather than applied — the §4 requirement
that was previously spread through an install guide.

**No release, and no tag.** CPR-2's steps 3 and 4 — a GitHub Release carrying the
wheel, manifest, signature and checksums, and the byte-equality proof against the
`agntnexus.com` artifact — are **not done**. Publishing source is separable from
publishing a release, and the release half needs its own reviewed slice. Nothing
was signed, tagged or uploaded.

**Two things remain open.** The package's own test suite is still unpublishable:
its fixtures carry tailnet hostname patterns, PEM `PRIVATE KEY` armour and
invitation-shaped values, so the public repository has no test suite and a
stranger can build, install, import and type-check but not run tests. And the
merge commit GitHub created for the publishing pull request carries the account's
private address rather than the noreply one, because a web merge uses the
account's primary email; correcting it needs a force-push of a public `main`,
which is an owner decision.

### The frozen path back to canonical

`agentnexus-original` stays canonical. The public repository's `docs/MIRROR.md`
names the mirrored set as frozen and names its five repository-local files —
`.github/**`, `.gitignore`, `constraints-ci.txt`, `ruff.toml` and the mirror page
itself — and its CI refuses a tracked file that is in neither list. There is no
automated byte comparison against this repository, because that needs to read a
private repository and therefore a cross-repository credential; the comparison
stays here, in the export audit, which checks every published path and every
historical version of it before anything leaves.

## CPR-3: reproducible public build and provenance

**Purpose:** Strengthen the relationship between the public source and wheel;
do not assume byte reproducibility before measuring it.

1. Build the public source tag in a pinned, documented environment and compare
   the wheel to the signed release wheel.
2. If byte reproducibility is not achieved, publish the precise differences and
   fix only understood sources of nondeterminism before claiming it.
3. Generate an SBOM and a GitHub build-provenance attestation for the public
   build, then document a consumer verification command.
4. Keep the existing release-manifest signature as the installation authority;
   provenance is an additional audit signal, not a replacement.

## Non-goals

- No open sourcing of the platform, infrastructure, Admin plane or private
  development history.
- No public Agent API, public write path, new credential, token, payment flow
  or telemetry.
- No GitHub Packages/PyPI publication or package-index installation path.
- No release signing private key in GitHub, Actions secrets, repository files or
  the public repository.
- No claim that an asset is reproducibly built until CPR-3 measures and proves
  it.
