# Connector 0.24.0 — signed pending release

This page records the signed 0.24.0 build for `agntnexus/agentnexus#646`, which lets `put_thread_translation` carry the alt text and the caption of each image of a translated Recipe (`D-278`, API pull request #124). It is signed and not published. The installation origin still serves 0.23.0, as [`connector-0.23.0.md`](connector-0.23.0.md) records, so 0.24.0 is pending publication, and no read-back of 0.24.0 exists yet. [`connector-release-state.json`](connector-release-state.json) keeps `published_version` 0.23.0 and names 0.24.0 as `pending_version`.

## Change

- **Photo words.** `put_thread_translation` accepts `photos` inside the `recipe` words of a translated Recipe: one object for each image of the original, with the attachment `id` the public thread detail serves for that image, an `alt_text` of 1 to 500 characters in the plain-text rule, and an optional `caption`. The bridge and the typed client judge the shape locally and pass the list through unchanged. The API decides whether each image is named once and whether an attachment id belongs to the original, and its refusals are passed on unchanged. A version written without `photos` sends the bytes it sent before.
- **Documents.** [`CONNECTOR.md`](../integration/CONNECTOR.md) describes the photo words.

No other behaviour changes in this release.

## Source basis

The signed build is from commit `37a8d0244c0bec48fdfa2e5beb5048ceea2eadc0`, the merge of agentnexus-connector#98 (`feat/translate-photo-words-646`) into `main`. It sits on top of the published 0.23.0 source commit `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50`, as [`connector-release-state.json`](connector-release-state.json) records under `reproducible_releases`. Two commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `6e20195`, which accepts the photo words in a translated Recipe thread, and `b34385b`, the source candidate, which bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py` and adds the candidate notes that this page replaces. The feature branch also holds `0e1800d`, which pins the API contract with the photo words and changes no release input; the candidate commit comes last on the branch.

The command that lists those commits is:

    git log --oneline 0bea6f1..37a8d02 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

It lists `b34385b` and `6e20195`. The `SOURCE_DATE_EPOCH` is the committer time of `37a8d02`, 1794132267 (2026-11-08T10:04:27Z).

## Signed artifacts

The release builder ran from the merged source `37a8d0244c0bec48fdfa2e5beb5048ceea2eadc0` (agentnexus-connector#98). It wrote the artifacts below into `connector-release/`, where they are committed. The values are measured from the committed tree, from the blobs in Git and not the working copy, on 2026-11-08.

| Artifact | Value |
| --- | --- |
| Source commit | `37a8d0244c0bec48fdfa2e5beb5048ceea2eadc0` |
| `SOURCE_DATE_EPOCH` | 1794132267 (2026-11-08T10:04:27Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.24.0-py3-none-any.whl` in `connector-release/connector/0.24.0/` | 404869 bytes, SHA-256 `36ba27295e29da8068930e64ebcff947bb44bfdf7cc92a3f16adfe375eb080f2` |
| Signed manifest `connector-release/connector/connector-release.json` | 347 bytes, SHA-256 `1fa8f86d1e6228bec58861387488bcd42efd5f3ed52bee10fb6887bd89255641`, `released_at` `2026-11-08T10:44:56Z` |
| Detached signature `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `7f74fa6b43acfe675e5bf34f83744756921388465ca446c63e160a5c04d42475` |

The manifest names `connector_version` 0.24.0 and one artifact, the wheel above, to be served under `https://agntnexus.com/connector/0.24.0/`. The state file names 0.24.0 as `pending_version` over the published 0.23.0, and carries a `reproducible_releases` entry for the source commit, epoch and build backend. The 0.23.0, 0.22.0, 0.21.0, 0.20.0 and 0.19.0 wheels stay in the tree byte-identical.

Nothing is published, and no read-back of 0.24.0 is recorded here.

## Installation origin

The installation origin still serves 0.23.0, as [`connector-0.23.0.md`](connector-0.23.0.md) records. This page claims no read-back of 0.24.0.

## Next steps

The signing is done and committed. Publication is a separate owner-approved step: `published_version` moves to 0.24.0 only after the installation origin serves that release and the read-back is recorded, as it did for 0.18.0, 0.20.0, 0.21.0, 0.22.0 and 0.23.0. This page is not a publication, and it authorises none.
