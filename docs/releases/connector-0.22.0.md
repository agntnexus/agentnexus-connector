# Connector 0.22.0 — signed pending release

This page records the signed 0.22.0 build for `agntnexus/agentnexus#632`, which carries the diets of a Recipe (`D-274`). It is signed and not published. The installation origin still serves 0.21.0, as [`connector-0.21.0.md`](connector-0.21.0.md) records, and no read-back of 0.22.0 is claimed here. [`connector-release-state.json`](connector-release-state.json) keeps `published_version` 0.21.0 and names 0.22.0 as `pending_version`.

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

The release builder ran from the merged source `155a38239e737b434873e84affc3df2cc285ef5d` (agentnexus-connector#92). It wrote the artifacts below into `connector-release/`, where they are committed. The values are measured from the committed tree, from the blobs in Git and not the working copy, on 2026-11-06.

| Artifact | Value |
| --- | --- |
| Source commit | `155a38239e737b434873e84affc3df2cc285ef5d` |
| `SOURCE_DATE_EPOCH` | 1793994885 (2026-11-06T19:54:45Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.22.0-py3-none-any.whl` in `connector-release/connector/0.22.0/` | 403060 bytes, SHA-256 `459fc740f0ced3e6680e90f0b834bfe0dcc87f5d4427b2476e0ab102d8a060b5` |
| Signed manifest `connector-release/connector/connector-release.json` | 347 bytes, SHA-256 `654389cd977a8630c590ef08064cc8458c857adc41c54e1a1a83158952cd50af`, `released_at` `2026-11-06T19:55:46Z` |
| Detached signature `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `72485a60c2f6fff7f7efc661fc397fdaf33e5ad217155e9e4180165d8940eee3` |

The manifest names `connector_version` 0.22.0 and one artifact, the wheel above, to be served under `https://agntnexus.com/connector/0.22.0/`. The state file names 0.22.0 as `pending_version` over the published 0.21.0, and carries a `reproducible_releases` entry for the source commit, epoch and build backend. The 0.21.0, 0.20.0 and 0.19.0 wheels stay in the tree byte-identical.

Nothing is published, and no read-back of 0.22.0 is recorded here.

## Installation origin

The installation origin still serves 0.21.0, as [`connector-0.21.0.md`](connector-0.21.0.md) records. This page claims no read-back of 0.22.0.

## Next steps

The signing is done and committed. Publication is a separate owner-approved step: `published_version` moves to 0.22.0 only after the installation origin serves that release and the read-back is recorded, as it did for 0.18.0, 0.20.0 and 0.21.0. This page is not a publication, and it authorises none.
