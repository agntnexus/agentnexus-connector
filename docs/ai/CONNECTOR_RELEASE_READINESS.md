# Connector 0.6.2 is released, and it is not yet deployed

What the release is, what it contains, how anybody can check it, and the one thing it deliberately
did not do. Written as a readiness record; kept as the record of what happened.

> **Released on 2026-09-19 under [#114](https://github.com/proplaner/agentnexus-original/issues/114).**
> Tag `v0.6.2` on `daac97feed007b212754083515c4e0e283eda8af` in `agntnexus/agentnexus-connector`,
> with a GitHub release carrying `agentnexus_sdk-0.6.2-py3-none-any.whl` (256197 bytes, sha256
> `300b6485253282863830d78090cffb7ac00f852e13c20492b437e48dff2c8232`), the signed manifest and its
> detached signature.
>
> **The installation origin still serves 0.6.1.** The release assets are verification copies;
> reaching `agntnexus.com` is a separate, separately approved deployment that has not happened.

Issue [#106](https://github.com/proplaner/agentnexus-original/issues/106), continuing Phase D of
[#42](https://github.com/proplaner/agentnexus-original/issues/42) after
[#104](https://github.com/proplaner/agentnexus-original/issues/104). Updated by
[#108](https://github.com/proplaner/agentnexus-original/issues/108), which prepared the candidate
this document was written to describe.

## The candidate

| | |
| --- | --- |
| Repository | `agntnexus/agentnexus-connector`, public |
| Version it declares | **`0.6.2`**, since #108 — no longer a version already published |
| Artifact | `agentnexus_sdk-0.6.2-py3-none-any.whl` |
| Build | reproducible with a pinned `SOURCE_DATE_EPOCH`; a signing build refuses without one |
| Tags in the repository | **0** · Releases: **0** |

The version bump and the URL corrections were made canonically in this repository and published to
the public Connector through `scripts/connector_public_export.py`, the audited export. Three files
moved: `pyproject.toml`, `src/agentnexus_sdk/version.py` and `docs/BEHAVIOUR.md`. The export refused
nothing.

## Why 0.6.2, and why not "no release at all"

The code has not changed. At this revision `src/agentnexus_sdk/` is byte-identical to the published
0.6.1 wheel in 24 of its 25 modules, and the 25th — `updater.py` — differs only by the two public key
coordinates the builder stamps in for the length of the build. So a release cannot be justified by
behaviour.

It is justified by metadata, and both defects were user-visible:

| Where | What it said | Why it was wrong |
| --- | --- | --- |
| The **published 0.6.1 wheel** | `Source: …/proplaner/agentnexus-original/tree/main/packages/agent-sdk-python` | A **private** repository. Anyone who installs the Connector and follows its declared source gets a 404. |
| The **public source before #108** | `Source = "https://github.com/proplaner/agentnexus-connector"` | A **redirect**. The repository is `agntnexus/agentnexus-connector`, and this project's standing rule is that a redirect is not a contract. |

Neither named the repository that actually holds the code. Both now do, in `pyproject.toml`,
`version.py` and `docs/BEHAVIOUR.md`, and the installed wheel was checked from a consumer's side:
`Documentation` and `Source` both read `https://github.com/agntnexus/agentnexus-connector`.

Measured against the published 0.6.1 wheel rather than asserted: of its 24 modules, **22 are
byte-identical** to this source, `updater.py` differs by **exactly the two** release-key coordinate
lines the builder stamps in for the length of the build, and `version.py` carries the version string
and the note explaining it. **Nothing else differs.** A rebuild today also adds `licenses/LICENSE`
to the wheel, which the published one lacks.

## Reproducibility, now implemented rather than recommended

#106 measured that the wheel was not byte-reproducible and that one environment variable fixed it,
and left the change out of its own scope. #108 made it a property of the build rather than an
accident of the environment.

`scripts/build_connector_release.py` resolves `SOURCE_DATE_EPOCH` explicitly — from
`--source-date-epoch`, else from the environment, else not at all — assembles the child environment
itself rather than inheriting one, and reports the value it used and where it came from. A build
that pins nothing says so in the same breath: *no predictable digest may be claimed from it.* A
build signed with a real key **refuses** without one, because that build's whole purpose is a digest
other people check.

**The value a release must use is the committer date of the released revision.**
`--source-date-epoch git` resolves it from a clean `HEAD`, and refuses a tree with uncommitted or
untracked changes, because then `HEAD` is not what would be built. It was chosen because anyone
holding the revision can rederive it with `git log -1 --format=%ct <revision>` and nothing else,
which is exactly what a third party needs.

Measured across **two fresh clones of the same revision**, each with its own interpreter:

| | Same `SOURCE_DATE_EPOCH` | No epoch |
| --- | --- | --- |
| Identical SHA-256 | **yes** | no |
| Identical size | yes | yes |
| Identical entry order | yes | yes |
| Identical per-entry CRC | yes | **yes** |
| Identical per-entry timestamp | yes | no |

The unpinned pair is the interesting half: every entry's content is identical and only the
timestamps differ, so the build was always deterministic and the *container* was not. That is what
the epoch fixes.

### Why no digest is recorded here

The digests measured above come from an **unstamped** build — `updater.py` still carries its
placeholders, because stamping it needs the release key. A real release stamps two coordinate lines
into that module, so its wheel is a different file with a different digest.

**A release's SHA-256 is therefore an output of the authorised release build, and this document does
not predict one.** What is now true, and was not before, is that the digest that build produces is
one anybody can reproduce from the published revision and the published epoch.

## What was proved, from a fresh public clone

Everything below ran against `agntnexus/agentnexus-connector` alone. No credential, no access to
this repository, and the production origin was never contacted — the same files now live in the
public repository, so the documented checks ran against those copies.

| Check | Result |
| --- | --- |
| The documented `VERIFY.md` procedure | manifest `sha256=44ef5183…`, `size=251679`; artifact identical on both |
| Signature over the manifest | verifies against the coordinates stamped into the loaders |
| Loader stamps | all three are `installers/` plus exactly the two coordinate lines |
| Wheel provenance | **25 of 25 modules accounted for**: 24 byte-identical to `src/`, 1 identical but for the stamp, **0 unaccounted** |
| Independent user path | clean virtual environment, published wheel installed, **23 modules imported**, no credential |

### Every check was shown to fail

A check that has never refused proves nothing.

| Mutation | Result |
| --- | --- |
| One byte flipped in the artifact | refused: `sha256 389001ab… != 44ef5183…` |
| A loader re-stamped with an all-zero coordinate | refused: *"the coordinates stamped into the loaders are not a point on P-256"* |
| A loader re-stamped with a **valid but different** key | refused three times over: two loader stamps disagree **and** the signature does not verify |
| The manifest's size field altered | refused twice: broken signature and size mismatch |

The second of those found a real defect in the check itself, which crashed instead of refusing. It
was fixed in `agntnexus/agentnexus-connector` at `47e4713e`; the exit code had always been right, but
a traceback is a worse diagnostic than a sentence.

### One contract, three consumers

The loader, the updater and the observer all resolve the same path and verify the same signature:

- `installers/connect.sh` — `MANIFEST_PATH="/connector/connector-release.json"`, verified with
  `openssl dgst -sha256 -verify` against its embedded coordinates;
- `src/agentnexus_sdk/updater.py` — the same `MANIFEST_PATH`, the same embedded coordinates, and it
  refuses outright while they are still placeholders;
- `apps/observer-web/Dockerfile` — serves that directory as the installation origin, fetched from
  this same pinned revision.

### What an independent user still cannot check

The Connector implements `agentnexus-sig-v1` independently, on purpose, so that the public audit
boundary does not depend on a private repository. The **signing vectors that would let a third party
check that implementation are not public** — they live in `agntnexus/agentnexus-sdk`, which is
private. [#101](https://github.com/proplaner/agentnexus-original/issues/101) ran that comparison and
it passed 6 of 6, but only because it had access to both sides.

A user can verify a *release*. A user cannot verify the *protocol implementation*. Publishing the
vectors would close that gap and needs its own decision, because they are generated by the private
API.

## The audit of what a release would publish

Across the public repository's tree and all **101 commits**:

- **No private key, no age or SOPS material, no token, no personal data, no concrete tailnet host.**
- 201 author entries as the noreply address, plus one `GitHub <noreply@github.com>` merge committer.
- Four scanner hits, all the same string in four historical versions of `.github/workflows/ci.yml`:
  the credential scan's **own pattern list**. The workflow excludes itself from its own scan for
  exactly that reason.

## The release-readiness record

What a publication would consist of. **None of it has been done.**

| | |
| --- | --- |
| Tag | `v0.6.2` on `agntnexus/agentnexus-connector` — the repository's first tag |
| Release | a GitHub release on that tag, whose assets are *verification copies*, never an install source |
| Artifact | `agentnexus_sdk-0.6.2-py3-none-any.whl` |
| Size and SHA-256 | outputs of the build, recorded in the manifest; not predictable in advance (see above) |
| Manifest | `connector/connector-release.json` — `schema_version`, `connector_version`, `released_at`, and one artifact with `platform`, `filename`, `url`, `size`, `sha256` |
| Signature | `connector-release.json.sig`: ECDSA P-256 over SHA-256 of the canonical manifest bytes, stored as 128 hex characters, `r‖s` |
| Origin | `https://agntnexus.com/connector/…`, which the manifest's `url` must name |

### One more control the release build enforces

This connector ships with public agent writes **enabled** (`PUBLIC_AGENT_API_GATE.is_open`), so the
build refuses unless `--public-agent-endpoint-approved` is given. That is deliberate: opening the
gate would otherwise be a one-character edit reaching every applicant through the normal update
path, with the D-024 and PAI-4 owner decisions never having been made. It is a control, not a build
convenience, and the operator step recorded in `docs/releases/connector-release-state.json` now
names it — a declared step that would stop on its first line is not a plan.

One defect found while exercising it, **not fixed here**: the comment above that constant still
reads *"This build refuses"*, directly above `is_open=True`. It is a stale comment in source that
ships inside the published wheel. Correcting it would change a module, and the claim this candidate
rests on is that no module but `version.py` differs from what 0.6.1 shipped. It belongs in a
separate issue after the release.

### Who may publish, and the minimum it needs

**The signing key never enters CI.** `scripts/build_connector_release.py` refuses a key inside the
repository and reads the private half only to derive the two public coordinates and sign the
manifest. The release is therefore built and signed **by the owner on an operator workstation**, not
by a workflow.

That gives an unusually small permission story:

| Step | Minimum needed |
| --- | --- |
| Build and sign the release | The private key, on the operator's machine. **No CI secret, no GitHub App, no PAT, no deploy key.** |
| Commit the result to `connector-release/` | Ordinary write access to the public Connector repository |
| Tag and create the GitHub release | `contents: write` on that repository |
| Serve it at `agntnexus.com` | A deployment — the observer image already fetches the pinned revision, so this is the existing deployment gate, not a new one |

**Publishing a release adds no new credential surface.** That is worth protecting: it is why the
signing design puts the key on a workstation rather than in a workflow.

### The decision that was missing, and was given

> **Owner approval for a tag, a GitHub release, and artifact publication.**

Granted, and carried out under [#114](https://github.com/proplaner/agentnexus-original/issues/114).
What was published:

| | |
| --- | --- |
| Tag | `v0.6.2`, annotated, on `daac97feed007b212754083515c4e0e283eda8af` |
| Release | <https://github.com/agntnexus/agentnexus-connector/releases/tag/v0.6.2> |
| Artifact | `agentnexus_sdk-0.6.2-py3-none-any.whl`, 256197 bytes |
| SHA-256 | `300b6485253282863830d78090cffb7ac00f852e13c20492b437e48dff2c8232` |
| Epoch | `SOURCE_DATE_EPOCH=1789817905`, the committer date of the released revision |
| Key | the same P-256 key as every release since 0.1.0; **no rotation** |

The build also required `--public-agent-endpoint-approved`, the owner control that exists because
this connector ships with public agent writes enabled. It was given deliberately for this build.

### What the artifact is built from, and why it matters

The published wheel is built **from the public repository**, not from `packages/agent-sdk-python`
here. That is not a detail: the artifact must be reproducible from the tree whose address its own
metadata names, or the `Source` field is decoration.

The two are not interchangeable. The public repository carries a `LICENSE` at its root, so
setuptools ships `dist-info/licenses/LICENSE` and records `License-File` in `METADATA`; a build from
this repository does not, because there is no licence file beside the package. Every module is
identical either way — the licence text is the entire difference, accounted for entry by entry
before anything was signed. Shipping the Apache-2.0 text the package declares is the better
artifact, and the published 0.6.1 wheel lacked it.

That gap is now closed. [#116](https://github.com/proplaner/agentnexus-original/issues/116) added
`packages/agent-sdk-python/LICENSE` — the repository's own licence, copied byte for byte, the same
git blob and the same `Copyright 2026 proplaner` attribution the public Connector publishes. A build
from this repository at `v0.6.2` with `SOURCE_DATE_EPOCH=1789817905` is now **byte-identical** to the
published wheel:

| | before #116 | after #116 | published |
| --- | --- | --- | --- |
| Size | 251984 | 256197 | 256197 |
| SHA-256 | `379b7f54…` | `300b6485…` | `300b6485…` |
| Entries | 30 | 31 | 31 |

Verified from two independent fresh clones, and the published manifest and signature verify against
that rebuild **unchanged**. The operator step no longer supplies a wheel with `--wheel`.

**The published wheel was not altered, and could not be.** Its bytes are immutable at their address;
what changed is that this repository can now reproduce them.

One caveat, measured rather than assumed: the licence enters the wheel as the working tree holds it,
so the equality holds on a CRLF checkout — what `core.autocrlf` produces on the Windows operator
workstation, and what the published wheel embeds (201 CRLF pairs, no bare LF). Forcing the LF form
yields 256180 bytes and `df55cb3f…`, differing only in the licence entry and the `RECORD` digest
covering it. No line-ending normalisation was introduced, because normalising would change the bytes
against a wheel that is already published. Pinning it deliberately deserves its own decision.

### What was not done

**The release is not a deployment.** `connector-release/` is untouched, no observer image was built,
no digest was pinned and no rollout happened, so `https://agntnexus.com/connector/…` still serves
0.6.1 and every applicant still installs 0.6.1. `docs/releases/connector-release-state.json`
therefore still declares `published: 0.6.1, pending: 0.6.2`, and it must stay that way until the
origin actually serves 0.6.2.

## The deployment provenance contract, unchanged and still not implemented

Restated from [#104](https://github.com/proplaner/agentnexus-original/issues/104) with what #106 asks
for added.

`infra/hetzner/Deploy-TestStack.ps1` runs `gh run download` against upstream `proplaner/agentnexus-original`
and checks that each `<name>.digest` artifact equals the image reference tracked in the release
overlay, and that each tracked reference appears in the rendered manifests. That is a
publication-provenance check worth keeping.

**What `production-release-current/release.yaml` must gain.** It carries the run but not who ran it:

```yaml
source-commit: 40e61498be086f9e45f750d6f9ce2378daed20df
github-actions-run: '35207029021'
github-actions-repository: proplaner/agentnexus-original   # ← the missing field
```

**What the runner must change.** One line: `$ArtifactRepository` stops being a constant and is read
from the release record, refusing a record that does not declare one. The check keeps its full
strength and stops assuming a single publisher.

**What `AGENTS.md` requires before that change may be pushed**, in order:

1. the PowerShell parser over the changed runner;
2. `npm run format:check`;
3. focused tests for the runner — `apps/forum-api/tests/test_deploy_runner_release_candidate.py`
   and the release-record guards that read the same file;
4. **a real, strictly read-only transport preflight against the intended node**, over the actual
   Windows → WSL → SSH → shell → `kubectl` chain, creating, modifying, deleting, enabling, applying
   and deploying nothing, with its complete result in the handoff.

A parser, a mocked test and a static render are explicitly not substitutes for the fourth. This slice
has no production access, so it cannot be given, and neither half was implemented: a record field
nothing reads is decoration, and a runner that reads it needs the proof.

## What did not happen

No git tag, GitHub release, or wheel, package, container or registry publication. No signing key
created, read, changed or printed. No new GitHub App, PAT, deploy key or secret change. No visibility
change — the Connector is public, with GitHub-hosted CI only. No deployment, digest promotion or
production access; no Terraform, Ansible or Kubernetes apply; no Hetzner, cluster, SSH, DNS or TLS
contact. No change to `Deploy-TestStack.ps1` or any other operations runner. No new build or release
contract depends on this repository.
