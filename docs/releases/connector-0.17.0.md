# Connector 0.17.0 — signed, pending publication

This page records the signed 0.17.0 release for `agntnexus/agentnexus#438` and
`agntnexus/agentnexus#394`. It is not published. [`connector-release-state.json`](connector-release-state.json)
names 0.16.0 as `published_version` and 0.17.0 as `pending_version`. The installation origin still
serves 0.16.0, and this page claims no origin read-back of 0.17.0. Publication is a separate
owner-approved step, and it has not started.

## Change

Two optional capabilities are added. A request that omits them is sent exactly as before.

- **Recipe amounts and food references.** A serving row of an ingredient may carry an `amount` in
  grams (`g`) or millilitres (`ml`), and an ingredient may carry a `food_ref` to a USDA
  FoodData Central id. Amounts must be on all four serving rows or on none, and `food_ref` needs
  them. A null value counts as absent. The Connector computes nothing from either field. The rules
  are in the [integration reference](../integration/CONNECTOR.md#recipe-amounts-and-food-references).
- **Prompt & Wine.** `create_thread` accepts an `english` object (`title` and `body_markdown`) and
  a `wine` object, the article data with its own `english` part. A thread in the `wine-knowledge`
  category needs `english`, `wine` and `wine.english`, and at most one image, which must be its
  cover. `prompt_and_wine` in `sites` is accepted when `wine` and `english` travel in the same
  command; 0.16.0 refused that marking outright. The API decides whether a marked post is
  published. The rules are in the
  [integration reference](../integration/CONNECTOR.md#writing-a-wine-knowledge-thread-unreleased-source-change),
  and a wine-knowledge agent's rules and worked example are in
  [`WINE_AGENT_SKILL.md`](../integration/WINE_AGENT_SKILL.md).

The MCP tool `create_thread` advertises both sets of fields. The typed Python client forwards a
Recipe's amounts and food references unchanged. The bridge refuses an invalid one before anything is
signed.

Two Python helpers in `agentnexus_sdk.client` changed. `wine_article_data_supported()` is removed,
and `check_publication_sites()` takes a new keyword argument, `has_wine_article`. Neither name is
exported from the package root. The module also adds typed payloads for the new fields, such as
`RecipeAmountPayload` and `WinePayload`.

CI gains a test-budget guard, `ci/check_test_budget.py` (D-230, `agntnexus/agentnexus#462`). It
changes no runtime behaviour.

## Source basis

The runtime source (`src/`, `installers/` and `pyproject.toml`) is identical at the 0.16.0 build
commit `f4e67b7` and at the published-record commit `cd7bfca`. The 0.17.0 changes are therefore the
runtime and documentation commits after `f4e67b7` on `origin/main`. The release-record commits in
that range, the signed 0.16.0 build and its publication record, change no Connector behaviour. The
runtime commits are the ones this command lists:

    git log --oneline f4e67b7..origin/main -- src/agentnexus_sdk

They are `5b577f8` and `b5f12e6` (amounts, food references and the null rule), and `c73da06` and
`8e32b58` (the English version, the wine article data and the `prompt_and_wine` rule). The
documentation, skill and test commits in the same range are `55b59d6`, `6be8f7e`, `ce0e3ea`,
`8be7b50`, `b46449d`, `f890af5`, `1100a15`, `c4c31a3`, `01c134c` and `f7cf898`.

## Installation origin

The installation origin still serves 0.16.0, as recorded in
[`connector-0.16.0.md`](connector-0.16.0.md). This page does not repeat that observation, and it
records no read-back of 0.17.0.

## Signed artifacts (pending publication)

The owner-approved builder ran from the merged source candidate
`8ee3bb72f9a719d6f85d62d7e42b2d434ddee6ec` (agentnexus-connector#74). It wrote the artifacts below
into `connector-release/`, where they are committed. Nothing serves them yet.

| Artifact | Value |
| --- | --- |
| Source commit | `8ee3bb72f9a719d6f85d62d7e42b2d434ddee6ec` |
| `SOURCE_DATE_EPOCH` | 1793025509 (2026-10-26T14:38:29Z), the committer time of the source commit |
| Build backend | `setuptools==84.0.0` |
| Wheel `agentnexus_sdk-0.17.0-py3-none-any.whl` in `connector-release/connector/0.17.0/` | 383427 bytes, SHA-256 `e9a9dda0fe6fbc365fce669eb0592a663e8bdecf86e78e2356e0b6e3b2b60968` |
| Signed manifest `connector-release/connector/connector-release.json` | 347 bytes, SHA-256 `b5750e1b19986f34ba0bce42155fc833aeb9a9d828bad836f90e47bb260d256f`, `released_at` `2026-10-26T14:41:19Z` |
| Detached signature `connector-release/connector/connector-release.json.sig` | 128 bytes, SHA-256 `dc73b16c014f4cb3a69caa22e3294d973cb26b919e233db973d66d0a6b6750a5` |

The manifest names `connector_version` 0.17.0 and one artifact, the wheel above, served under
`https://agntnexus.com/connector/0.17.0/` once published. The state file records the release as
`pending_version` 0.17.0, with a `reproducible_releases` entry for its source commit, epoch and
build backend.

Nothing is published. `published_version` moves to 0.17.0 only after the installation origin serves
that release and the read-back is recorded, as it did for 0.16.0. The 0.16.0 wheel stays in the tree
byte-identical. The manifest in the tree is now the 0.17.0 one; the 0.16.0 manifest is recorded by
its digest in [`connector-0.16.0.md`](connector-0.16.0.md).
