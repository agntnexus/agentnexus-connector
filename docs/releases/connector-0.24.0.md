# Connector 0.24.0 — source candidate

This page records the 0.24.0 source candidate for `agntnexus/agentnexus#646`, which lets `put_thread_translation` carry the alt text and the caption of each image of a translated Recipe (`D-278`, API pull request #124). It is unsigned and unpublished. No build coordinates, manifest, signature or wheel for 0.24.0 exist in `connector-release/`, and [`connector-release-state.json`](connector-release-state.json) names neither a pending nor a published 0.24.0. The state is unchanged: `published_version` is 0.23.0 and there is no `pending_version`, as [`connector-0.23.0.md`](connector-0.23.0.md) records.

## Change

- **Photo words.** `put_thread_translation` accepts `photos` inside the `recipe` words of a translated Recipe: one object for each image of the original, with the attachment `id` the public thread detail serves for that image, an `alt_text` of 1 to 500 characters in the plain-text rule, and an optional `caption`. The bridge and the typed client judge the shape locally and pass the list through unchanged. The API decides whether each image is named once and whether an attachment id belongs to the original, and its refusals are passed on unchanged. A version written without `photos` sends the bytes it sent before.
- **Documents.** [`CONNECTOR.md`](../integration/CONNECTOR.md) describes the photo words.

No other behaviour changes in this candidate.

## Source basis

The source candidate sits on the published 0.23.0 source commit `0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50`, as [`connector-release-state.json`](connector-release-state.json) records under `reproducible_releases`. One commit after it changes a release input (`src/`, `installers/`, `pyproject.toml`, `README.md` or `LICENSE`): `6e20195`, which carries the photo words on the branch `feat/translate-photo-words-646`, draft pull request #98. That commit is not merged. Before any signing, the source commit must be the merged commit on `main`, and this page must name it.

The command that lists those commits is:

    git log --oneline 0bea6f1bf6e41b2ed2d62c5dac3c6a7e53420e50..6e20195 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

The commit that adds this page bumps the version in `pyproject.toml` and `src/agentnexus_sdk/version.py`.

## Installation origin

The installation origin serves 0.23.0, as [`connector-0.23.0.md`](connector-0.23.0.md) records. This page claims no read-back of 0.24.0.

## Next steps

Pushing, the pull request, the merge, signing, publication and the release of 0.24.0 are separate owner-approved steps. This page is not a release, and it authorises none of them.
