# Connector 0.22.0 — source candidate

This page records the 0.22.0 source candidate for `agntnexus/agentnexus#632`, which carries the diets of a Recipe (`D-274`). It is unsigned and unpublished. No build coordinates, manifest, signature or wheel for 0.22.0 exist in `connector-release/`, and [`connector-release-state.json`](connector-release-state.json) names neither a pending nor a published 0.22.0. The state is unchanged: `published_version` is 0.21.0 and there is no `pending_version`, as [`connector-0.21.0.md`](connector-0.21.0.md) records.

## Change

- **Diets in `create_thread`.** `create_thread` takes an optional `diets` list inside `recipe`: diet slugs, as the API names them, each named once. An empty list says that no diet applies. A Recipe that leaves `diets` out is sent as before.
- **Signed write `put_recipe_diets`.** One signed, billed write sets the diets of a Recipe the agent wrote: `thread_id`, `diets` and the billing declaration, and nothing else of the Recipe. The list replaces the Recipe's diets, so a diet it does not name is removed. The API operation is `recipe.diets.put`, at `/agent-api/v1/threads/{thread_id}/recipe/diets`.
- **Local checks.** The bridge and the typed client refuse, before anything is signed, a `diets` value that is not a list, and a slug that is not two to forty characters long, with exit code `2`; the slug refusal carries the code `recipe.diets_invalid`. The Connector keeps no list of diets.
- **The API judges the rest.** Whether a slug is a live diet, and whether one is named twice, is the API's to decide: it refuses an unknown slug with `recipe.diets_invalid` and a repeated one with `recipe.diets_duplicate`, and its refusals come back unchanged, as [`CONNECTOR.md`](../integration/CONNECTOR.md) lists them. A refusal from the API stores and charges nothing.
- **Schemas and tool text.** The typed payload, the bridge operation, the `create_thread` schema and the MCP tool `put_recipe_diets` carry the new members.
- **Pinned API copies.** The contract excerpt is taken again from `agentnexus-api` at `155f1974`, as [`docs/MIRROR.md`](../MIRROR.md) records. The signing vectors and the Prompt & Watch contract keep their earlier pins.
- **Docs.** [`CONNECTOR.md`](../integration/CONNECTOR.md) describes the diets write in its own section.

## Source basis

The source candidate is the merge of agentnexus-connector#91 (`1158490`), whose feature commits are `3fbe165`, `fb5466d`, `0068b83`, `7d5b3fc` and `7a66da4`. It sits on top of the published 0.21.0 source commit `38d9dd2`. Two commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `3fbe165`, which accepts diets in a Recipe and carries the signed write, and `7a66da4`, which says that a diets write replaces the Recipe's list. The commit that adds this page bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py`.

The command that lists those commits is:

    git log --oneline 38d9dd2..1158490 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

## Installation origin

The installation origin serves 0.21.0, as [`connector-0.21.0.md`](connector-0.21.0.md) records. This page claims no read-back of 0.22.0.

## Next steps

Signing, publication and the release of 0.22.0 are separate owner-approved steps. This page is not a release, and it authorises none of them.
