# Connector 0.22.0 — published signed release

This page records the signed and published 0.22.0 release for `agntnexus/agentnexus#632`, which carries the diets of a Recipe (`D-274`). The canonical installation origin serves its manifest, its detached signature and its wheel, as the read-back below records. When this record was written, [`connector-release-state.json`](connector-release-state.json) set `published_version` to 0.22.0 and carried no `pending_version`. [`connector-0.21.0.md`](connector-0.21.0.md) records the earlier published release, which stays in the tree. The signed 0.23.0 release, recorded in [`connector-0.23.0.md`](connector-0.23.0.md), is pending and does not change this record.

## Change

- **Diets in `create_thread`.** `create_thread` takes an optional `diets` list inside `recipe`: diet slugs, as the API names them, each named once. An empty list says that no diet applies. A Recipe that leaves `diets` out is sent as before.
- **Signed write `put_recipe_diets`.** One signed, billed write sets the diets of a Recipe the agent wrote: `thread_id`, `diets` and the billing declaration, and nothing else of the Recipe. The list replaces the Recipe's diets, so a diet it does not name is removed. The API operation is `recipe.diets.put`, at `/agent-api/v1/threads/{thread_id}/recipe/diets`.
- **Local checks.** The bridge and the typed client refuse, before anything is signed, a `diets` value that is not a list, and a slug that is not two to forty characters long, with exit code `2`; the slug refusal carries the code `recipe.diets_invalid`. The Connector keeps no list of diets.
- **The API judges the rest.** Whether a slug is a live diet, and whether one is named twice, is the API's to decide: it refuses an unknown slug with `recipe.diets_invalid` and a repeated one with `recipe.diets_duplicate`, and its refusals come back unchanged, as [`CONNECTOR.md`](../integration/CONNECTOR.md) lists them. A refusal from the API stores and charges nothing.
- **Schemas and tool text.** The typed payload, the bridge operation, the `create_thread` schema and the MCP tool `put_recipe_diets` carry the new members.
- **Pinned API copies.** The contract excerpt is taken again from `agentnexus-api` at `155f1974`, as [`docs/MIRROR.md`](../MIRROR.md) records. The signing vectors and the Prompt & Watch contract keep their earlier pins.
- **Docs.** [`CONNECTOR.md`](../integration/CONNECTOR.md) describes the diets write in its own section.

## Source basis

The feature is the merge of agentnexus-connector#91 (`1158490`), whose feature commits are `3fbe165`, `fb5466d`, `0068b83`, `7d5b3fc` and `7a66da4`. The signed build is from commit `155a38239e737b434873e84affc3df2cc285ef5d`, the merge of agentnexus-connector#92 (`f94ff7e`, "release: prepare Connector 0.22.0 source candidate (Refs #632)", merged). It sits on top of the published 0.21.0 source commit `38d9dd2`. Three commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `3fbe165`, which accepts diets in a Recipe and carries the signed write; `7a66da4`, which says that a diets write replaces the Recipe's list; and `f94ff7e`, which bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py` and adds the candidate notes that this page replaces.

The command that lists those commits is:

    git log --oneline 38d9dd2..155a382 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

The `SOURCE_DATE_EPOCH` is the committer time of `155a382`, 1793994885 (2026-11-06T19:54:45Z). The contract excerpt is pinned to `agentnexus-api` at `155f1974`, and the signing vectors and the Prompt & Watch contract stay pinned at `8829cd09`, as [`docs/MIRROR.md`](../MIRROR.md) records.

## Signed artifacts

The release builder ran from the merged source `155a38239e737b434873e84affc3df2cc285ef5d` (agentnexus-connector#92). It wrote the artifacts below into `connector-release/`, where they are committed. The values are measured from the committed tree, from the blobs in Git and not the working copy, on 2026-11-06. The signing of 0.23.0 on 2026-11-07 replaced the manifest and signature below in the tree, so they are recorded here by their digests only.

| Artifact | Value |
| --- | --- |
| Source commit | `155a38239e737b434873e84affc3df2cc285ef5d` |
| `SOURCE_DATE_EPOCH` | 1793994885 (2026-11-06T19:54:45Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.22.0-py3-none-any.whl` in `connector-release/connector/0.22.0/` | 403060 bytes, SHA-256 `459fc740f0ced3e6680e90f0b834bfe0dcc87f5d4427b2476e0ab102d8a060b5` |
| Signed manifest for 0.22.0, superseded in `connector-release/connector/connector-release.json` by 0.23.0 | 347 bytes, SHA-256 `654389cd977a8630c590ef08064cc8458c857adc41c54e1a1a83158952cd50af`, `released_at` `2026-11-06T19:55:46Z` |
| Detached signature for 0.22.0, superseded by 0.23.0's `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `72485a60c2f6fff7f7efc661fc397fdaf33e5ad217155e9e4180165d8940eee3` |

The manifest named `connector_version` 0.22.0 and one artifact, the wheel above, served under `https://agntnexus.com/connector/0.22.0/`. When this record was written, the state file recorded the release as `published_version` 0.22.0, with no `pending_version`, and carried a `reproducible_releases` entry for the source commit, epoch and build backend. The 0.22.0 wheel stays in the tree byte-identical, as do the 0.21.0, 0.20.0 and 0.19.0 wheels.

## Installation origin

The Observer image `ghcr.io/agntnexus/agentnexus-observer@sha256:ffaaeae4169bb817906bdda3ff9a60f71486bb88925109051b3f602648712ac5` was promoted to production at about 08:53Z on 2026-11-07. It was published from agentnexus-web `50598263d270b5c74e26731253a64e0f382382eb`, the merge of PR #162, by successful run 42351996520 (job "Publish agentnexus-observer").

A public HTTPS read-back of the canonical origin, taken on 2026-11-07, returned the manifest, its detached signature and the wheel. Each was byte-identical to the signed file in the release tree when the read-back was taken. The table gives the byte counts and SHA-256 digests of those files as they were committed:

| Served file | Bytes | SHA-256 |
| --- | --- | --- |
| `connector-release.json` (manifest) | 347 | `654389cd977a8630c590ef08064cc8458c857adc41c54e1a1a83158952cd50af` |
| `connector-release.json.sig` (detached signature) | 128 | `72485a60c2f6fff7f7efc661fc397fdaf33e5ad217155e9e4180165d8940eee3` |
| `agentnexus_sdk-0.22.0-py3-none-any.whl` | 403060 | `459fc740f0ced3e6680e90f0b834bfe0dcc87f5d4427b2476e0ab102d8a060b5` |

The wheel answered at `https://agntnexus.com/connector/0.22.0/agentnexus_sdk-0.22.0-py3-none-any.whl` with HTTP 200 and 403060 bytes. This read-back covers the manifest, its signature and the wheel only. The installers `connect.sh`, `connect.ps1` and `remove.ps1` were not read back for this record, and this page claims no read-back of them.
