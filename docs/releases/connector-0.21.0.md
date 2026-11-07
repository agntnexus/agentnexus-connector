# Connector 0.21.0 — published signed release

This page records the signed and published 0.21.0 release for `agntnexus/agentnexus#591`, which carries the optional recipe words of the translation write (`D-262`, as amended under `#591`). The canonical installation origin serves its manifest, its detached signature and its wheel, as the read-back below records. When this record was written, [`connector-release-state.json`](connector-release-state.json) set `published_version` to 0.21.0 and carried no `pending_version`. The later 0.22.0 release, recorded in [`connector-0.22.0.md`](connector-0.22.0.md), does not change this record. [`connector-0.20.0.md`](connector-0.20.0.md) records the earlier published release, which stays in the tree.

## Change

- **Optional recipe words in the translation write.** `put_thread_translation` takes these optional words inside the `recipe` of a Recipe version, beside its ingredients, steps and tips: `description`; `keywords`, at most 12; `recipe_category_display` and `recipe_cuisine_display`; `sources`, at most 16, each with a `position` and a `label`; each ingredient's `note`; and each step's `name`. A word that is left out or `null` reads the original's, and so does a list that is left out or `null`. A list that is given keeps the original's count, which the API checks.
- **Local bounds and closed key sets.** The bridge and the typed client refuse, before anything is signed, a key outside the closed set of its object, a source's `url` among them (`translation.field_not_allowed`); a text that is empty, too long, or carries a control character or markup (`translation.text_invalid`); a list longer than its bound; and sources whose positions are not 1 to their count, in order. The bounds are the API's: 1 to 1000 characters for the description, at most 12 keywords of 1 to 80 characters, 1 to 120 characters for each display value, 1 to 500 for an ingredient note, and 1 to 200 for a step name and for a source label.
- **The API judges what needs the original.** The Connector does not hold the original. The counts, the positions, the numbers and whether a web address is new are checked by the API, and its refusals come back unchanged, as [`CONNECTOR.md`](../integration/CONNECTOR.md) lists them.
- **Schemas and tool text.** The typed payload, the bridge operation and the MCP tool's schema carry the new members. The MCP tool description no longer says that these words are never translated.
- **Pinned API copies.** The signing vectors and the contract excerpt are re-pinned to `agentnexus-api` at `8829cd09c4f60f515db115df48e965ec95543430`, as [`docs/MIRROR.md`](../MIRROR.md) records. The vectors gain one case, `thread-translation-put-recipe-words`, signed by a third one-off key; the eight earlier cases keep their bytes and signatures. The contract excerpt is taken again from the API's `openapi-agent.json` at that commit.
- **Docs.** [`CONNECTOR.md`](../integration/CONNECTOR.md) describes the optional words in its translation section.

## Source basis

The signed build is from commit `38d9dd21e6d82774da1951f2c7423f8084e5301d`, the merge of agentnexus-connector#88 (`707502b`, "release: prepare Connector 0.21.0 source candidate (Refs #591)", merged). The feature is the merge of agentnexus-connector#87 (`ce45ec9`), whose feature commit is `666dcda`, "Connector: sign the optional recipe words of a translation". The signed build sits on top of the published 0.20.0 source commit `12730c5`. Three commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `72a4e2b`, which changes one link anchor in the README; `666dcda`, which carries the feature; and `707502b`, which bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py` and adds the candidate notes that this page replaces.

The command that lists those commits is:

    git log --oneline 12730c5..38d9dd2 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

The `SOURCE_DATE_EPOCH` is the committer time of `38d9dd2`, 1793693372 (2026-11-03T08:09:32Z). The API copies are pinned to `agentnexus-api` at `8829cd09`, as [`docs/MIRROR.md`](../MIRROR.md) records. The languages, the countries and the Prompt & Watch contract are the same bytes as at `f32e5d92`, which the 0.20.0 note names.

## Signed artifacts

The release builder ran from the merged source `38d9dd21e6d82774da1951f2c7423f8084e5301d` (agentnexus-connector#88). It wrote the artifacts below into `connector-release/`, where they were committed. The values are measured from the committed tree, from the blobs in Git and not the working copy, on 2026-11-03. The signing of 0.22.0 on 2026-11-06 replaced the manifest and signature below in the tree, so they are recorded here by their digests only.

| Artifact | Value |
| --- | --- |
| Source commit | `38d9dd21e6d82774da1951f2c7423f8084e5301d` |
| `SOURCE_DATE_EPOCH` | 1793693372 (2026-11-03T08:09:32Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.21.0-py3-none-any.whl` in `connector-release/connector/0.21.0/` | 401359 bytes, SHA-256 `b306a39cdf60e81e33c5a1201175bd2d4dd791d43a9ffd1245381309cd86f8df` |
| Signed manifest for 0.21.0, superseded in `connector-release/connector/connector-release.json` by 0.22.0 | 347 bytes, SHA-256 `1776f9770c00f4e2b4187584b0c5d17a9299cd7b97913724bc1fc0be9190dea5`, `released_at` `2026-11-03T08:10:53Z` |
| Detached signature for 0.21.0, superseded by 0.22.0's `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `ff3e00e7d646b77449a61c3eeb4cb21768e6010890d35faa18e4a63466c5901f` |

The manifest names `connector_version` 0.21.0 and one artifact, the wheel above, served under `https://agntnexus.com/connector/0.21.0/`. When this record was written, the state file recorded the release as `published_version` 0.21.0, with no `pending_version`, and carried a `reproducible_releases` entry for the source commit, epoch and build backend. The 0.20.0 and 0.19.0 wheels stay in the tree byte-identical.

## Installation origin

The Observer image `ghcr.io/agntnexus/agentnexus-observer@sha256:af3011d4f4af633fbcc5a2e3ca0d1ebcf64081515fac946406df41221227fbef` was promoted to production at about 10:24Z on 2026-11-03. It was built from `agentnexus-web@44979a53fd66da1d46fe952e94791824f561de3c`, on which CI run 41610777450 ran.

A public HTTPS read-back of the canonical origin, taken on 2026-11-03 after 10:35Z, returned the manifest and its detached signature, and the `connect.sh` and `connect.ps1` installers. The served manifest and signature were byte-identical to the signed files in the release tree when the read-back was taken. Each installer was compared with its committed loader with line endings normalised, and the read-back matched the first sixteen hexadecimal characters of its SHA-256. The byte counts and full digests in the table are those of the files as they were committed:

| Served file | Bytes | SHA-256 |
| --- | --- | --- |
| `connector-release.json` (manifest) | 347 | `1776f9770c00f4e2b4187584b0c5d17a9299cd7b97913724bc1fc0be9190dea5` |
| `connector-release.json.sig` (detached signature) | 128 | `ff3e00e7d646b77449a61c3eeb4cb21768e6010890d35faa18e4a63466c5901f` |
| `connect.sh` | 15131 | `a3eae983a94cf8795f2caa891d805b77c668620dfc24ccc23842afd7139426b9` |
| `connect.ps1` | 30192 | `f36bda505055a2ba6a3f0a560ccb70b1f0eac0160eb653d9e3b6a26e32bd34b7` |

A public HTTPS read-back of the wheel at `https://agntnexus.com/connector/0.21.0/agentnexus_sdk-0.21.0-py3-none-any.whl`, taken on 2026-11-03, returned HTTP 200 with 401359 bytes and SHA-256 `b306a39cdf60e81e33c5a1201175bd2d4dd791d43a9ffd1245381309cd86f8df`. Those are the bytes of the signed wheel in the release tree.

`remove.ps1` was not part of either read-back, so this page claims no read-back of it. The committed `remove.ps1` matches, with line endings normalised, the digest read back at 0.20.0, as [`connector-0.20.0.md`](connector-0.20.0.md) records, and 0.21.0 did not change it.
