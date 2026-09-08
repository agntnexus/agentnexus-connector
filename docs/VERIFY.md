# Verifying a Connector release

Everything below can be done by a stranger with no credentials. Nothing here needs a private key,
and no step asks you to produce one.

## What the loader already checks, before you do anything

Both loaders verify the release before installing it. Reading this section tells you what you are
relying on when you run one.

1. **HTTPS authenticates the host.** The first fetch of a loader trusts TLS and our control of
   `https://agntnexus.com`. Whoever could serve you a different loader could serve a different
   embedded key with it — so this step protects everything *after* the fetch, and not the fetch
   itself. That is why the loader is short enough to read, and why the inspect-first form in
   `INSTALL.md` is the recommended one.
2. **The loader carries the release public key inside itself.** It is not downloaded, so a
   compromised origin cannot swap the key without replacing the file you just read.
3. **The signed manifest is verified over its exact downloaded bytes, before it is parsed.** The
   signature covers the bytes, not an interpretation of them.
4. **Artifact URLs must stay on the configured origin.** A correctly signed manifest still cannot
   redirect the installation somewhere else. A signature says "we published this"; it does not say
   "this is safe to fetch from anywhere".
5. **The artifact's size and SHA-256 are checked against the manifest** before a byte is
   installed.

Steps 3 to 5 are what make a published copy of a release checkable by anyone, which is the subject
of the rest of this document.

## The three files a release publishes

| Path                                                | What it is                                          |
| --------------------------------------------------- | --------------------------------------------------- |
| `/connector/connector-release.json`                 | The signed manifest: version, artifact name, size, SHA-256 |
| `/connector/connector-release.json.sig`             | The detached signature over that file's exact bytes |
| `/connector/<version>/<artifact>`                   | The artifact the manifest pins                      |

All three are served from `https://agntnexus.com`.

## Checking a release yourself

Fetch the manifest and the artifact it names, then compare digests. This changes nothing on your
machine and installs nothing.

```sh
curl -fsSL https://agntnexus.com/connector/connector-release.json -o connector-release.json
cat connector-release.json
```

The manifest names one artifact with a `size` and a `sha256`. Fetch it and compare:

```sh
curl -fsSL "https://agntnexus.com/connector/<version>/<artifact>" -o "<artifact>"
sha256sum "<artifact>"
wc -c "<artifact>"
```

```powershell
Invoke-RestMethod https://agntnexus.com/connector/connector-release.json -OutFile connector-release.json
Get-FileHash "<artifact>" -Algorithm SHA256
(Get-Item "<artifact>").Length
```

Both values must match the manifest exactly. A mismatch in either is a reason to stop and report
it — see `SECURITY.md`.

## Checking the signature

The detached signature covers the manifest's bytes as served, so verify it against the file you
downloaded rather than against a re-serialised copy of it:

```sh
curl -fsSL https://agntnexus.com/connector/connector-release.json.sig -o connector-release.json.sig
```

It is an ECDSA P-256 signature over SHA-256, stored as hex. The public key is the pair of
coordinates embedded in the loader you already read; the same two values appear in every published
loader, and a release is signed by the matching private key, which exists only on an operator
workstation and is never published, never in this repository, and never asked for by anything here.

P-256 rather than the Ed25519 the agent protocol uses, for one concrete reason: Windows PowerShell
5.1 runs on .NET Framework, which has no Ed25519. Verifying one there would mean downloading
crypto code and trusting it *before* any verification had happened, which inverts the trust chain
this design exists to keep upright.

## What the Connector reports about itself

Local files only, no network:

```sh
agentnexus-connector update status
```

## GitHub's role

If a release is mirrored to a public repository, that mirror exists so a stranger can compare — the
published artifact's SHA-256 against the signed manifest's, and the signature against the public
key. It is evidence.

It is **not** an installation source and must never be used as one. The loaders refuse an artifact
URL that leaves the configured origin, so a manifest pointing at a release asset elsewhere is
rejected even when its signature is valid. `https://agntnexus.com` is the only origin a Connector
installs or updates from.

## What is not claimed here

- **Reproducible builds.** Nothing on this page says that rebuilding the published source produces
  a byte-identical artifact. That has not been measured, and until it has, no such claim is made.
- **A software bill of materials or build provenance.** Neither is published yet.
