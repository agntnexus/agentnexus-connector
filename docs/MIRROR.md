# Where this code comes from, and what may change here

This repository is public so that the Connector can be read before it is run. It is **not** where
the Connector is developed.

## The canonical source

`proplaner/agentnexus-original` is private and remains the single canonical source for everything
mirrored here, until an explicit later cutover. The flow is one-directional:

```
agentnexus-original  ──────►  agentnexus-connector
   (canonical)                  (public, frozen)
```

Nothing flows back. A change committed here to a mirrored path is **not adopted upstream** — it is
overwritten the next time the source is published, and in the meantime it makes the public copy
disagree with the software people actually install. That is the failure this page exists to
prevent, so the rule is stated plainly rather than implied by convention.

## What is frozen here

| Path | What it is |
| --- | --- |
| `src/agentnexus_sdk/**` | The released Connector's own source |
| `installers/connect.ps1`, `installers/connect.sh`, `installers/remove.ps1` | The loaders an operator is asked to run |
| `pyproject.toml` | The metadata that builds the distribution |
| `README.md` | The package's description |
| `LICENSE` | Apache-2.0, as the metadata declares |
| `SECURITY.md` | The security and support policy |
| `docs/BEHAVIOUR.md`, `docs/INSTALL.md`, `docs/VERIFY.md`, `docs/TROUBLESHOOTING.md` | The public documents |

Found a bug, a wrong statement or a security issue in any of them? `SECURITY.md` says how to report
it. A pull request against a frozen path will be declined however good the change is, because
merging it would create a second version of a file that already has an owner.

## What belongs to this repository

Five things, and they are not mirrored from anywhere:

| Path | Why it is local |
| --- | --- |
| `.github/**` | This repository's own CI. The canonical repository's workflows are for a different repository and are not published. |
| `constraints-ci.txt` | Pins the toolchain *this* CI resolves. The package's own dependency ranges stay deliberately loose for consumers. |
| `ruff.toml` | The lint and format settings that apply to this source. The canonical file is monorepo-wide and most of it is exceptions for code that is not here; this reproduces only the part that governs `src/agentnexus_sdk/**`. If the two disagree, the canonical one is right. |
| `.gitignore` | Keeps build output out of a public repository. A committed wheel here would look like something to install. |
| `docs/MIRROR.md` | This page. |

CI refuses a tracked file that is in neither list, so another kind cannot appear quietly.

## Why not an automated comparison

The obvious check — compare these files against the canonical repository on every run — needs to
read a private repository, which needs a cross-repository credential. None exists here, and putting
one in a public repository's CI to prove the public copy is faithful would be a poor trade. The
comparison is instead performed where the credential already lives: the canonical repository's
export audits every published path and every historical version of it, byte for byte, and refuses
rather than publishing a mismatch.

## What the history here is

The real one, filtered. The commits that produced these files were carried over rather than
flattened into a single import, so `git log` and `git blame` answer honestly. What was removed is
everything outside the published set: the platform, the infrastructure, the operator runbooks and
the package's own test fixtures were never part of this history.

Author identities were normalised to a single GitHub noreply address. Nothing else about the
commits was rewritten.

## What is not here, and will not be

No production signing private key, release credential or decrypted production secret, in the tree,
the history, the examples or the CI configuration. No installation source: `https://agntnexus.com`
is the only origin the loaders accept, and a file on this repository's release pages — if one ever
appears — is there to compare against what you obtained from that origin, never to install.
