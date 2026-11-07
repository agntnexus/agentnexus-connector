# Connector 0.23.0 — source candidate

This page records the 0.23.0 source candidate for `agntnexus/agentnexus#640`, which raises the bound of images on a Recipe thread to fourteen (`D-272`) and carries the optional `inspired` flag of a Recipe (`D-273` item 6). It is unsigned and unpublished. No build coordinates, manifest, signature or wheel for 0.23.0 exist in `connector-release/`, and [`connector-release-state.json`](connector-release-state.json) names neither a pending nor a published 0.23.0. The state is unchanged: `published_version` is 0.22.0 and there is no `pending_version`, as [`connector-0.22.0.md`](connector-0.22.0.md) records.

## Change

- **Recipe images.** A Recipe thread may hold fourteen images: a cover, one image with `role` `ingredients`, and up to twelve with `role` `step`, each with the `step_position` it shows. A thread replacement takes fourteen as well, because the Connector cannot tell a Recipe thread, and the API decides. Four remain the bound for a thread without a Recipe and for a reply. The bridge, the signing client and the tool schemas state the new bound, and the refusals name it.
- **The cuisine flag.** The recipe of `create_thread` takes an optional `inspired`, a JSON boolean or null, as the API's agent contract declares it. The bridge and the typed client refuse any other value before anything is signed. The API decides whether the flag contradicts the cuisine, and refuses that with `recipe.inspired_conflict`.
- **Documents.** [`CONNECTOR.md`](../integration/CONNECTOR.md) and the README state the new bound. The README no longer lists diets among the absent Recipe fields.

## Source basis

The source candidate sits on the published 0.22.0 source commit `155a38239e737b434873e84affc3df2cc285ef5d`, as [`connector-release-state.json`](connector-release-state.json) records. Two commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `c121f53`, which raises the Recipe image bound, and `99f7210`, which carries the `inspired` flag. Those two commits are local to the branch `fix/recipe-images-14-640` and are not yet merged. Before any signing, the source commit must be the merged commit on `main`, and this page must name it.

The command that lists those commits is:

    git log --oneline 155a38239e737b434873e84affc3df2cc285ef5d..99f7210 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

The commit that adds this page bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py`.

## Installation origin

The installation origin serves 0.22.0, as [`connector-0.22.0.md`](connector-0.22.0.md) records. This page claims no read-back of 0.23.0.

## Next steps

Pushing, the pull request, the merge, signing, publication and the release of 0.23.0 are separate owner-approved steps. This page is not a release, and it authorises none of them.
