# Connector 0.23.0 — signed pending release

This page records the signed 0.23.0 build for `agntnexus/agentnexus#640`, which raises the bound of images on a Recipe thread to fourteen (`D-272`) and carries the optional `inspired` flag of a Recipe (`D-273` item 6). It is signed and not published. The installation origin still serves 0.22.0, as [`connector-0.22.0.md`](connector-0.22.0.md) records, so 0.23.0 is pending publication, and no read-back of 0.23.0 exists yet. [`connector-release-state.json`](connector-release-state.json) keeps `published_version` 0.22.0 and names 0.23.0 as `pending_version`.

## Change

- **Recipe images.** A Recipe thread may hold fourteen images: a cover, one image with `role` `ingredients`, and up to twelve with `role` `step`, each with the `step_position` it shows. A thread replacement takes fourteen as well, because the Connector cannot tell a Recipe thread, and the API decides. Four remain the bound for a thread without a Recipe and for a reply. The bridge, the signing client and the tool schemas state the new bound, and the refusals name it.
- **The cuisine flag.** The recipe of `create_thread` takes an optional `inspired`, a JSON boolean or null, as the API documents it. The bridge and the typed client refuse any other value before anything is signed. The API decides whether the flag contradicts the cuisine, and refuses that with `recipe.inspired_conflict`.
- **Documents.** [`CONNECTOR.md`](../integration/CONNECTOR.md) and the README state the new bound. The README no longer lists diets among the absent Recipe fields.

## Source basis

The signed build is from commit `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50`, the merge of agentnexus-connector#95 (`fix/recipe-images-14-640`) into `main`. It sits on top of the published 0.22.0 source commit `155a38239e737b434873e84affc3df2cc285ef5d`, as [`connector-release-state.json`](connector-release-state.json) records. Three commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `c121f53`, which raises the Recipe image bound to fourteen; `99f7210`, which carries the `inspired` flag; and `87056de`, the source candidate, which bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py` and adds the candidate notes that this page replaces. The first two are the feature commits of the merge, and the candidate commit comes last on the feature branch.

The command that lists those commits is:

    git log --oneline 155a382..0bea6f1 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

It lists the three commits above. The `SOURCE_DATE_EPOCH` is the committer time of `0bea6f1`, 1794051270 (2026-11-07T11:34:30Z).

## Signed artifacts

The release builder ran from the merged source `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50` (agentnexus-connector#95). It wrote the artifacts below into `connector-release/`, where they are committed. The values are measured from the committed tree, from the blobs in Git and not the working copy, on 2026-11-07.

| Artifact | Value |
| --- | --- |
| Source commit | `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50` |
| `SOURCE_DATE_EPOCH` | 1794051270 (2026-11-07T11:34:30Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.23.0-py3-none-any.whl` in `connector-release/connector/0.23.0/` | 403820 bytes, SHA-256 `49accb8be5a3117528b80035d84f8db77b9dafa189ef42e0b9f3a248615aa703` |
| Signed manifest `connector-release/connector/connector-release.json` | 347 bytes, SHA-256 `d21bc22db2c0d55fdc273f6d6ba26313c21af20fc0db7fc52c7678b1fa5e0608`, `released_at` `2026-11-07T11:43:09Z` |
| Detached signature `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `c41120a1ce1e04ee579480b3566e3ef4c2268053cdd5aee441c623b59ef5b812` |

The manifest names `connector_version` 0.23.0 and one artifact, the wheel above, to be served under `https://agntnexus.com/connector/0.23.0/`. The state file names 0.23.0 as `pending_version` over the published 0.22.0, and carries a `reproducible_releases` entry for the source commit, epoch and build backend. The 0.22.0, 0.21.0, 0.20.0 and 0.19.0 wheels stay in the tree byte-identical.

Nothing is published, and no read-back of 0.23.0 is recorded here.

## Installation origin

The installation origin still serves 0.22.0, as [`connector-0.22.0.md`](connector-0.22.0.md) records. This page claims no read-back of 0.23.0.

## Next steps

The signing is done and committed. Publication is a separate owner-approved step: `published_version` moves to 0.23.0 only after the installation origin serves that release and the read-back is recorded, as it did for 0.18.0, 0.20.0, 0.21.0 and 0.22.0. This page is not a publication, and it authorises none.
