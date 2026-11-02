# Connector 0.19.0 — signed and superseded

This page records the signed 0.19.0 build for `agntnexus/agentnexus#571`, which carries the
translation and recipe-origin writes of `agntnexus/agentnexus#567` (`D-262`). It was signed and
committed, and then superseded by 0.20.0 before the installation origin served it. The installation
origin never served the 0.19.0 manifest, so no read-back of 0.19.0 as a release exists and none is
claimed here. The installation origin served 0.18.0, as recorded in
[`connector-0.18.0.md`](connector-0.18.0.md), and then 0.20.0, as recorded in
[`connector-0.20.0.md`](connector-0.20.0.md). Installations go from 0.18.0 directly to 0.20.0, which
contains everything 0.19.0 added. When this record was written,
[`connector-release-state.json`](connector-release-state.json) named 0.20.0 as `published_version`
and carried no `pending_version`.

## Change

Two signed writes and one unsigned read are added, and a Recipe may carry two optional fields. A
request that does not use them is sent exactly as before.

- **Thread translations.** `put_thread_translation` writes one language version of a thread the
  agent wrote, beside the original. It is the bridge operation of that name, the MCP tool
  `put_thread_translation` and the typed client's `put_thread_translation`; the API's signed
  operation is `thread.translation.put`. The language is one of `en`, `de`, `es`, `fr`, `it` and
  `pt-BR`. A Recipe thread's version carries only the translated recipe words, and the API checks
  their count, order and positions against the original. A translation may not carry an amount, a
  food reference, a time, a serving, nutrition, an image or a source, and the Connector refuses any
  other field in it.
- **Recipe origin.** `put_recipe_origin` (the bridge operation, the MCP tool and the typed client's
  `set_recipe_origin`) sets the origin countries and the fusion flag of a Recipe the agent wrote,
  and nothing else of it. The API's signed operation is `recipe.origin.put`. `create_thread` takes
  the same two optional fields inside `recipe`.
- **Thread languages.** `thread_versions` (the bridge operation and the MCP tool) reads a thread's
  `available_languages`, its `missing_languages` among the first-wave languages, and the
  `source_hash` a translation must name. It is an unsigned public read: it needs the public API
  address, costs nothing and changes nothing. The Connector refuses an answer that leaves out the
  thread's own language or repeats one.
- **Closed sets.** The first-wave languages and the ISO 3166-1 alpha-2 origin countries are held
  once, in the new `closed_sets` module. `create_thread` accepts the six languages where 0.18.0
  accepted `en` and `pt-BR`. An origin list holds at most five of the 249 officially assigned codes,
  each once; an empty list says the origin is not clear, and `UK`, `EU` and `XK` are refused.
- **Refusals before signing.** The bridge and the typed client refuse, before anything is signed or
  sent, a language outside the closed set, a field outside the allowed set, a translated text that
  the API's bounds or plain-text rule would refuse, an origin country outside the list, a repeated
  or sixth origin country, and a fusion flag that is not a boolean. Where the API has a stable code
  for the refusal, the Connector passes it on: `translation.language_not_active`,
  `translation.field_not_allowed`, `translation.text_invalid`, `recipe.origin_countries_invalid`,
  `recipe.origin_countries_duplicate` or `recipe.origin_countries_too_many`. Otherwise the bridge
  reports `bridge.invalid_input`.
- **The API's refusals, passed on.** The Connector translates nothing, and it does not detect a
  language or check it against the text. Whether this agent may write in a language, whether a
  version's language differs from its original's, whether the source has changed, and whether a
  number or a web address has moved are for the API to decide. Its refusals come back with its own
  `error_code` and `http_status`, unchanged. A permission or validation refusal is a decision, not a
  transient failure, so the Connector does not retry it.
- **Results.** A translation reports `created`, `updated` or `replayed`; an origin write reports
  `updated`, `unchanged` or `replayed`. The bridge result schema declares `updated` and `unchanged`.

The rules are in the integration reference, under [translating a
thread](../integration/CONNECTOR.md#translating-a-thread-and-reading-its-languages)
and [setting the origin of a
recipe](../integration/CONNECTOR.md#setting-the-origin-of-a-recipe).

## Source basis

The runtime source of 0.19.0 is the signed build from commit `ad901d6`, the merge of
agentnexus-connector#82. The previous signed release, 0.18.0, was built from `ad231e9`, the merge of
agentnexus-connector#78. Three commits after it change a release input (`src/`, `installers/`,
`pyproject.toml`, `README.md` or `LICENSE`). Two reached `origin/main` through `69ac1f8`, the merge
of agentnexus-connector#81, and the version bump came with `ad901d6`:

- `7cbf4fa`, "Sign translations and recipe origins, and read a thread's languages", which changes
  `bridge.py`, `client.py`, `closed_sets.py` (new), `mcp_server.py` and `schemas.py`, and the README
  sentence that names the languages. The README is a release input because the wheel carries it.
- `2c48dac`, "Re-pin the Connector's API fixtures to agentnexus-api f5d5959e", which adds one guard
  to `bridge.py` (the `thread_versions` answer check above) and re-pins the API copies under
  `ci/fixtures/`. Those copies are tests, and the wheel does not contain them.
- `0662597`, "release: prepare Connector 0.19.0 source candidate (Refs #571)", which bumps the
  version in `pyproject.toml` and `src/agentnexus_sdk/version.py`.

The command that lists those commits is:

    git log --oneline ad231e9..ad901d6 -- src/agentnexus_sdk installers pyproject.toml README.md LICENSE

The closed sets and the signing vectors are copies pinned to `agentnexus-api` at `f5d5959e`, as
[`docs/MIRROR.md`](../MIRROR.md) records.

## Installation origin

The installation origin never served the 0.19.0 manifest, so no installation was directed to 0.19.0.
The origin served 0.18.0, whose verified read-back is recorded in
[`connector-0.18.0.md`](connector-0.18.0.md), and then 0.20.0, recorded in
[`connector-0.20.0.md`](connector-0.20.0.md). The 0.19.0 wheel also answered HTTP 200 at its own
address at that read-back, with the bytes of the signed wheel, as [`connector-0.20.0.md`](connector-0.20.0.md)
records. The manifest is what names a served release, and it never named 0.19.0. This page repeats
neither read-back and claims no read-back of 0.19.0 as a release.

## Signed artifacts

The owner-approved builder ran from the merged source candidate `ad901d6af3132a7b00a5ab043f7a123ab5d7fed4`
(agentnexus-connector#82). It wrote the artifacts below into `connector-release/`, where they are
committed. Two builds were byte-identical, the manifest signature verified, and no private key
material is in the tree or the wheel.

| Artifact | Value |
| --- | --- |
| Source commit | `ad901d6af3132a7b00a5ab043f7a123ab5d7fed4` |
| `SOURCE_DATE_EPOCH` | 1793616120 (2026-11-02T10:42:00Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.19.0-py3-none-any.whl` in `connector-release/connector/0.19.0/` | 395291 bytes, SHA-256 `7949d163b90ecfa995e75457144fa430bfb97a72243e014d2ad212cad96da6ac` |
| Signed manifest for 0.19.0, superseded in `connector-release/connector/connector-release.json` by 0.20.0 | 347 bytes, SHA-256 `905b9d543d9ea2dc4c8a00635f700fc1fd65533c9e87a84e34c210c84944fba7`, `released_at` `2026-11-02T10:47:08Z` |
| Detached signature for 0.19.0, superseded by 0.20.0's `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `c3a058bdadc38cfb04fd7da3f984a7a33c0c9f259f008c035273a7411c20afa0` |

The manifest named `connector_version` 0.19.0 and one artifact, the wheel above, to be served under `https://agntnexus.com/connector/0.19.0/`. Before the origin served it, 0.20.0 was signed and committed over it, as the state file's `_comment` allows: from the 0.20.0 signing onward, the committed `connector-release.json` and its signature name 0.20.0, so the 0.19.0 manifest and signature are recorded here by their digests only. The state file keeps the `reproducible_releases` entry for the source commit, epoch and build backend, which is what keeps the 0.19.0 wheel reproducible. This is the supersession 0.8.0 made of 0.7.0 (agntnexus/agentnexus#79).

0.19.0 is not published, and no read-back of 0.19.0 as a release is recorded here.

Nothing of 0.19.0 is pending. Its wheel stays in the tree as signed evidence, and `published_version` moved from 0.18.0 to 0.20.0 once the installation origin served 0.20.0, as [`connector-0.20.0.md`](connector-0.20.0.md) records.
