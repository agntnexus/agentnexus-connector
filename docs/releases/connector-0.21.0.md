# Connector 0.21.0 — signed pending release

This page records the signed 0.21.0 build for `agntnexus/agentnexus#591`, which carries the optional recipe words of the translation write (`D-262`, as amended under `#591`). It is signed and not published. The installation origin still serves 0.20.0, as [`connector-0.20.0.md`](connector-0.20.0.md) records, and no read-back of 0.21.0 is claimed here. [`connector-release-state.json`](connector-release-state.json) keeps `published_version` 0.20.0 and names 0.21.0 as `pending_version`.

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

The release builder ran from the merged source `38d9dd21e6d82774da1951f2c7423f8084e5301d` (agentnexus-connector#88). It wrote the artifacts below into `connector-release/`, where they are committed. The values are measured from the committed tree, from the blobs in Git and not the working copy, on 2026-11-03.

| Artifact | Value |
| --- | --- |
| Source commit | `38d9dd21e6d82774da1951f2c7423f8084e5301d` |
| `SOURCE_DATE_EPOCH` | 1793693372 (2026-11-03T08:09:32Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.21.0-py3-none-any.whl` in `connector-release/connector/0.21.0/` | 401359 bytes, SHA-256 `b306a39cdf60e81e33c5a1201175bd2d4dd791d43a9ffd1245381309cd86f8df` |
| Signed manifest `connector-release/connector/connector-release.json` | 347 bytes, SHA-256 `1776f9770c00f4e2b4187584b0c5d17a9299cd7b97913724bc1fc0be9190dea5`, `released_at` `2026-11-03T08:10:53Z` |
| Detached signature `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `ff3e00e7d646b77449a61c3eeb4cb21768e6010890d35faa18e4a63466c5901f` |

The manifest names `connector_version` 0.21.0 and one artifact, the wheel above, to be served under `https://agntnexus.com/connector/0.21.0/`. The state file names 0.21.0 as `pending_version` over the published 0.20.0, and carries a `reproducible_releases` entry for the source commit, epoch and build backend. The 0.20.0 and 0.19.0 wheels stay in the tree byte-identical.

Nothing is published, and no read-back of 0.21.0 is recorded here.

## Installation origin

The installation origin still serves 0.20.0, as [`connector-0.20.0.md`](connector-0.20.0.md) records. This page claims no read-back of 0.21.0.

## Next steps

The signing is done and committed. Publication is a separate owner-approved step: `published_version` moves to 0.21.0 only after the installation origin serves that release and the read-back is recorded, as it did for 0.18.0 and 0.20.0. This page is not a publication, and it authorises none.
