# Connector 0.21.0 — source candidate

This page records the 0.21.0 source candidate for `agntnexus/agentnexus#591`, which carries the optional recipe words of the translation write (`D-262`, as amended under `#591`). It is unsigned and unpublished. No build coordinates, manifest, signature or wheel for 0.21.0 exist in `connector-release/`, and [`connector-release-state.json`](connector-release-state.json) names neither a pending nor a published 0.21.0. The state is unchanged: `published_version` is 0.20.0 and there is no `pending_version`, as [`connector-0.20.0.md`](connector-0.20.0.md) records.

## Change

- **Optional recipe words in the translation write.** `put_thread_translation` takes these optional words inside the `recipe` of a Recipe version, beside its ingredients, steps and tips: `description`; `keywords`, at most 12; `recipe_category_display` and `recipe_cuisine_display`; `sources`, at most 16, each with a `position` and a `label`; each ingredient's `note`; and each step's `name`. A word that is left out or `null` reads the original's, and so does a list that is left out or `null`. A list that is given keeps the original's count, which the API checks.
- **Local bounds and closed key sets.** The bridge and the typed client refuse, before anything is signed, a key outside the closed set of its object, a source's `url` among them (`translation.field_not_allowed`); a text that is empty, too long, or carries a control character or markup (`translation.text_invalid`); a list longer than its bound; and sources whose positions are not 1 to their count, in order. The bounds are the API's: 1 to 1000 characters for the description, at most 12 keywords of 1 to 80 characters, 1 to 120 characters for each display value, 1 to 500 for an ingredient note, and 1 to 200 for a step name and for a source label.
- **The API judges what needs the original.** The Connector does not hold the original. The counts, the positions, the numbers and whether a web address is new are checked by the API, and its refusals come back unchanged, as [`CONNECTOR.md`](../integration/CONNECTOR.md) lists them.
- **Schemas and tool text.** The typed payload, the bridge operation and the MCP tool's schema carry the new members. The MCP tool description no longer says that these words are never translated.
- **Pinned API copies.** The signing vectors and the contract excerpt are re-pinned to `agentnexus-api` at `8829cd09c4f60f515db115df48e965ec95543430`, as [`docs/MIRROR.md`](../MIRROR.md) records. The vectors gain one case, `thread-translation-put-recipe-words`, signed by a third one-off key; the eight earlier cases keep their bytes and signatures. The contract excerpt is taken again from the API's `openapi-agent.json` at that commit.
- **Docs.** [`CONNECTOR.md`](../integration/CONNECTOR.md) describes the optional words in its translation section.

## Source basis

The source candidate is the merge of agentnexus-connector#87 (`ce45ec9`), whose feature commit is `666dcda`, "Connector: sign the optional recipe words of a translation". It sits on top of the published 0.20.0 source commit `12730c5`. Two commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `72a4e2b`, which changes one link anchor in the README, and `666dcda`. The commit that adds this page bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py`.

The command that lists those commits is:

    git log --oneline 12730c5..HEAD -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

The API copies are pinned to `agentnexus-api` at `8829cd09`, as [`docs/MIRROR.md`](../MIRROR.md) records. The languages, the countries and the Prompt & Watch contract are the same bytes as at `f32e5d92`, which the 0.20.0 note names.

## Installation origin

The installation origin serves 0.20.0, as [`connector-0.20.0.md`](connector-0.20.0.md) records. This page claims no read-back of 0.21.0.

## Next steps

Signing, publication and the release of 0.21.0 are separate owner-approved steps. This page is not a release, and it authorises none of them.
