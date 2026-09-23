# What is here, who owns it, and what may change

This repository is public so that the Connector can be read before it is run. It is also where the
Connector is developed: a fix belongs here, in a branch of this repository, and nowhere else.

## Who owns this source

**This repository is canonical for `src/agentnexus_sdk/**`**, for the loaders in `installers/`, for
the package metadata, and for the public documents under `docs/`. A change to any of them is made
here, reviewed here, and released from here.

`proplaner/agentnexus-original` is **historical**: private, unchanged, and an archive of how the
product was built before it was decomposed. It is not the tracker, not the documentation authority
and **never an upstream** for anything in this tree. A dated record may cite it, and several under
`docs/ai/` do; nothing may send new work to it. This page said the opposite until the owner decision
under [agntnexus/agentnexus#5](https://github.com/agntnexus/agentnexus/issues/5) settled it.

Issues follow the same rule as the rest of the portfolio: see [`AGENTS.md`](../AGENTS.md).

## What this repository publishes

| Path | What it is |
| --- | --- |
| `src/agentnexus_sdk/**` | The released Connector's own source |
| `installers/connect.ps1`, `installers/connect.sh`, `installers/remove.ps1` | The loaders an operator is asked to run |
| `pyproject.toml` | The metadata that builds the distribution |
| `README.md` | The package's description |
| `LICENSE` | Apache-2.0, as the metadata declares |
| `SECURITY.md` | The security and support policy |
| `docs/BEHAVIOUR.md`, `docs/INSTALL.md`, `docs/VERIFY.md`, `docs/TROUBLESHOOTING.md` | The public documents |
| `connector-release/**` | The published release origin: the three stamped loaders, ten wheels from 0.1.0 to 0.6.1, the signed release manifest and its signature |

Found a bug, a wrong statement or a security issue in any of them? `SECURITY.md` says how to
report a security issue, and says why that one does not start in public. Anything else starts as an
Issue, and the change is made against this repository.

### `connector-release/` is what an installation actually downloads

It arrived with its own history — sixteen commits, back to the first release on 2026-08-31 — because
until then the Connector's *source* was public here while its *distribution* lived only in the
repository that is now the historical archive, so nothing in the portfolio owned the thing users
install. Decision D-079 puts
every AgentNexus-owned path that handles keys, signatures, configuration, network traffic or updates
inside this boundary. This is that path.

Nothing secret is in it, and CI proves that rather than promising it:

- the released loaders are `installers/` plus **two lines** — the public P-256 coordinates `X` and
  `Y`. `ci/verify_release.py` refuses any other difference;
- those coordinates verify the ECDSA signature over `connector-release.json`, and the manifest's
  declared size and SHA-256 must match the artifact's bytes;
- the 0.6.1 wheel was byte-identical to `src/agentnexus_sdk/` as that tree stood at its release,
  in 24 of its 25 modules; `updater.py` differed by the same two coordinate lines. The source has
  moved on since, which is what a version number is for.

The signature is a signature, not a key. **No private signing key is here, has ever been here, or
may ever be here** — see the last section of this page.

## What belongs to this repository

Eight things that serve this repository rather than the people who install the Connector:

| Path | Why it is local |
| --- | --- |
| `.github/**` | This repository's own CI: what a reader would otherwise have to run by hand before trusting what they are about to install. |
| `ci/**` | The checks this repository runs on itself. `verify_release.py` checks the release chain on every run; `test_governance.py` checks that the instructions still name the right tracker; `test_termux_support.py` and `test_release_verification.py` check the platform decision behind the Termux fix and that the release chain still refuses what it must; `test_build_release.py` checks that the release builder refuses every release it must not produce. They exist here rather than canonically because this repository is the one that now owns the release, and therefore owns the promise that it is checkable. |
| `scripts/build_release.py` | The release builder, and the only file here that ever holds a private signing key: in memory, passed as a path from outside every repository, never written or printed. `build` signs the checked-out commit's release and is run by an operator by hand; `reproduce` rebuilds the committed release from its recorded source commit with no key and is run by CI on every change. Local because this repository owns the release. |
| `constraints-ci.txt` | Pins the toolchain *this* CI resolves. The package's own dependency ranges stay deliberately loose for consumers. |
| `ruff.toml` | The lint and format settings that apply to this source. The canonical file is monorepo-wide and most of it is exceptions for code that is not here; this reproduces only the part that governs `src/agentnexus_sdk/**`. If the two disagree, the canonical one is right. |
| `.gitignore` | Keeps build output out of a public repository. A committed wheel here would look like something to install. |
| `docs/MIRROR.md` | This page. |
| `AGENTS.md` | Where this repository's issues live and how work starts here. It is local because the rule it carries is *this* repository's: a public repository whose CI must stay GitHub-hosted, because a public pull request can contain code nobody has reviewed. The canonical policy it points at is not published here. |

CI refuses a tracked file that is in neither list, so another kind cannot appear quietly.

## What CI checks instead of a comparison

There is no second copy of this source to compare against, so nothing here reads another repository
and no cross-repository credential exists — putting one in a public repository's CI would be a poor
trade for a check that now has nothing to check. What CI does instead is verify the release chain
from published data alone on every run (`ci/verify_release.py`), and refuse a tracked file that is
neither published nor declared repository-local.

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

`connector-release/` does not change that, and it is worth being exact about why. Those are the
bytes the origin serves, kept here so that the release has an owner and so that anyone can check a
download against them. They are **not a second origin**: the loaders resolve
`https://agntnexus.com/connector/…` and nothing else, and cloning this repository installs nothing.
If a wheel here ever disagreed with the one the origin serves, the signed manifest decides, and one
of the two is wrong.
