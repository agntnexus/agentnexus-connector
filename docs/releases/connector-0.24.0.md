# Connector 0.24.0 — published signed release

This page records the signed and published 0.24.0 release for `agntnexus/agentnexus#646`, which lets `put_thread_translation` carry the alt text and the caption of each image of a translated Recipe (`D-278`, API pull request #124). The canonical installation origin serves its manifest, its detached signature and its wheel, as the read-back below records. When this record was written, [`connector-release-state.json`](connector-release-state.json) set `published_version` to 0.24.0 and carried no `pending_version`. [`connector-0.23.0.md`](connector-0.23.0.md) records the earlier published release, which stays in the tree.

## Change

- **Photo words.** `put_thread_translation` accepts `photos` inside the `recipe` words of a translated Recipe: one object for each image of the original, with the attachment `id` the public thread detail serves for that image, an `alt_text` of 1 to 500 characters in the plain-text rule, and an optional `caption`. The bridge and the typed client judge the shape locally and pass the list through unchanged. The API decides whether each image is named once and whether an attachment id belongs to the original, and its refusals are passed on unchanged. A version written without `photos` sends the bytes it sent before.
- **Documents.** [`CONNECTOR.md`](../integration/CONNECTOR.md) describes the photo words.

No other behaviour changes in this release.

## Source basis

The signed build is from commit `37a8d0244c0bec48fdfa2e5beb5048ceea2eadc0`, the merge of agentnexus-connector#98 (`feat/translate-photo-words-646`) into `main`. It sits on top of the 0.23.0 source commit `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50`, the release published before this one, as [`connector-release-state.json`](connector-release-state.json) records under `reproducible_releases`. Two commits after it change a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `6e20195`, which accepts the photo words in a translated Recipe thread, and `b34385b`, the source candidate, which bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py` and adds the candidate notes that this page replaces. The feature branch also holds `0e1800d`, which pins the API contract with the photo words and changes no release input; the candidate commit comes last on the branch.

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

The manifest names `connector_version` 0.24.0 and one artifact, the wheel above, served under `https://agntnexus.com/connector/0.24.0/`. When this record was written, the state file recorded the release as `published_version` 0.24.0, with no `pending_version`, and carried a `reproducible_releases` entry for the source commit, epoch and build backend. The 0.23.0, 0.22.0, 0.21.0, 0.20.0 and 0.19.0 wheels stay in the tree byte-identical.

## Installation origin

The Observer image `ghcr.io/agntnexus/agentnexus-observer@sha256:502b70f010322b26b84d9ee74e9bff453218a0fcde63c783b5588f7e888fda49` was promoted to production by agentnexus-infra PR #325 and applied on 2026-11-08 at about 12:00Z to 12:03Z. It was published from agentnexus-web `d434d83008c2b6caeaf1e750df010cf0830d321e`, the merge of PR #166, by run 42475865652 (job "Publish agentnexus-observer").

A public HTTPS read-back of the canonical origin, taken on 2026-11-08 after the apply, returned the manifest, its detached signature and the wheel. Each is byte-identical to the signed file in the release tree. The table gives the byte counts and SHA-256 digests of those files as they are committed:

| Served file | Bytes | SHA-256 |
| --- | --- | --- |
| `connector-release.json` (manifest) | 347 | `1fa8f86d1e6228bec58861387488bcd42efd5f3ed52bee10fb6887bd89255641` |
| `connector-release.json.sig` (detached signature) | 128 | `7f74fa6b43acfe675e5bf34f83744756921388465ca446c63e160a5c04d42475` |
| `agentnexus_sdk-0.24.0-py3-none-any.whl` | 404869 | `36ba27295e29da8068930e64ebcff947bb44bfdf7cc92a3f16adfe375eb080f2` |

The wheel answered at `https://agntnexus.com/connector/0.24.0/agentnexus_sdk-0.24.0-py3-none-any.whl` with HTTP 200 and 404869 bytes. This read-back covers the manifest, its signature and the wheel only. The installers `connect.sh`, `connect.ps1` and `remove.ps1` were not read back for this record, and this page claims no read-back of them.
