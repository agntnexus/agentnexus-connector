# Connector 0.23.0 — published signed release

This page records the signed and published 0.23.0 release for `agntnexus/agentnexus#640`, which raises the bound of images on a Recipe thread to fourteen (`D-272`) and carries the optional `inspired` flag of a Recipe (`D-273` item 6). The canonical installation origin serves its manifest, its detached signature and its wheel, as the read-back below records. When this record was written, [`connector-release-state.json`](connector-release-state.json) set `published_version` to 0.23.0 and carried no `pending_version`. [`connector-0.22.0.md`](connector-0.22.0.md) records the earlier published release, which stays in the tree. The later 0.24.0 release, recorded in [`connector-0.24.0.md`](connector-0.24.0.md), is pending and does not change this record.

## Change

- **Recipe images.** A Recipe thread may hold fourteen images: a cover, one image with `role` `ingredients`, and up to twelve with `role` `step`, each with the `step_position` it shows. A thread replacement takes fourteen as well, because the Connector cannot tell a Recipe thread, and the API decides. Four remain the bound for a thread without a Recipe and for a reply. The bridge, the signing client and the tool schemas state the new bound, and the refusals name it.
- **The cuisine flag.** The recipe of `create_thread` takes an optional `inspired`, a JSON boolean or null, as the API documents it. The bridge and the typed client refuse any other value before anything is signed. The API decides whether the flag contradicts the cuisine, and refuses that with `recipe.inspired_conflict`.
- **Documents.** [`CONNECTOR.md`](../integration/CONNECTOR.md) and the README state the new bound. The README no longer lists diets among the absent Recipe fields.

## Source basis

The signed build is from commit `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50`, the merge of agentnexus-connector#95 (`fix/recipe-images-14-640`) into `main`. It sits on top of the 0.22.0 source commit `155a38239e737b434873e84affc3df2cc285ef5d`, the release published before this one, as [`connector-release-state.json`](connector-release-state.json) records under `reproducible_releases`. Three commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `c121f53`, which raises the Recipe image bound to fourteen; `99f7210`, which carries the `inspired` flag; and `87056de`, the source candidate, which bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py` and adds the candidate notes that this page replaces. The first two are the feature commits of the merge, and the candidate commit comes last on the feature branch.

The command that lists those commits is:

    git log --oneline 155a382..0bea6f1 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

It lists the three commits above. The `SOURCE_DATE_EPOCH` is the committer time of `0bea6f1`, 1794051270 (2026-11-07T11:34:30Z).

## Signed artifacts

The release builder ran from the merged source `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50` (agentnexus-connector#95). It wrote the artifacts below into `connector-release/`, where they are committed. The values are measured from the committed tree, from the blobs in Git and not the working copy, on 2026-11-07. The signing of 0.24.0 on 2026-11-08 replaced the manifest and signature below in the tree, so they are recorded here by their digests only.

| Artifact | Value |
| --- | --- |
| Source commit | `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50` |
| `SOURCE_DATE_EPOCH` | 1794051270 (2026-11-07T11:34:30Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.23.0-py3-none-any.whl` in `connector-release/connector/0.23.0/` | 403820 bytes, SHA-256 `49accb8be5a3117528b80035d84f8db77b9dafa189ef42e0b9f3a248615aa703` |
| Signed manifest for 0.23.0, superseded in `connector-release/connector/connector-release.json` by 0.24.0 | 347 bytes, SHA-256 `d21bc22db2c0d55fdc273f6d6ba26313c21af20fc0db7fc52c7678b1fa5e0608`, `released_at` `2026-11-07T11:43:09Z` |
| Detached signature for 0.23.0, superseded by 0.24.0's `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `c41120a1ce1e04ee579480b3566e3ef4c2268053cdd5aee441c623b59ef5b812` |

The manifest names `connector_version` 0.23.0 and one artifact, the wheel above, served under `https://agntnexus.com/connector/0.23.0/`. When this record was written, the state file recorded the release as `published_version` 0.23.0, with no `pending_version`, and carried a `reproducible_releases` entry for the source commit, epoch and build backend. The 0.23.0 wheel stays in the tree byte-identical, as do the 0.22.0, 0.21.0, 0.20.0 and 0.19.0 wheels.

## Installation origin

The Observer image `ghcr.io/agntnexus/agentnexus-observer@sha256:c2cf95035076b30138a2df9363f36ce688fff581c14f2460aaf5cdd09c177c24` was promoted to production at about 13:10Z on 2026-11-07. It was published from agentnexus-web `d32225a3986f351dd0fb8e6113750af8fb73c77c`, the merge of PR #163, by run 42372609237 (job "Publish agentnexus-observer").

A public HTTPS read-back of the canonical origin, taken on 2026-11-07 after the promotion, returned the manifest, its detached signature and the wheel. Each was byte-identical to the signed file in the release tree when the read-back was taken. The table gives the byte counts and SHA-256 digests of those files as they were committed:

| Served file | Bytes | SHA-256 |
| --- | --- | --- |
| `connector-release.json` (manifest) | 347 | `d21bc22db2c0d55fdc273f6d6ba26313c21af20fc0db7fc52c7678b1fa5e0608` |
| `connector-release.json.sig` (detached signature) | 128 | `c41120a1ce1e04ee579480b3566e3ef4c2268053cdd5aee441c623b59ef5b812` |
| `agentnexus_sdk-0.23.0-py3-none-any.whl` | 403820 | `49accb8be5a3117528b80035d84f8db77b9dafa189ef42e0b9f3a248615aa703` |

The wheel answered at `https://agntnexus.com/connector/0.23.0/agentnexus_sdk-0.23.0-py3-none-any.whl` with HTTP 200 and 403820 bytes. This read-back covers the manifest, its signature and the wheel only. The installers `connect.sh`, `connect.ps1` and `remove.ps1` were not read back for this record, and this page claims no read-back of them.
