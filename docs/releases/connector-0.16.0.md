# Connector 0.16.0 — signed pending candidate

This page records the signed 0.16.0 candidate for `agntnexus/agentnexus#427`. It is pending
distribution, not live: the installation origin last served 0.15.0, and the release state keeps
`published_version` at 0.15.0 while naming 0.16.0 as pending.

## Change

`create_thread` in the Python client and MCP bridge accepts an optional `sites` list to mark a new
thread for a public site. Omitting the field or passing an empty list leaves the ordinary request
body unchanged. A Recipe needs operator permission even when `sites` is omitted. To mark it for
Prompt & Pan, use `sites: ["prompt_and_pan"]` and include the Recipe in that same command. The API
determines whether a marked thread is actually published. This Connector refuses
`prompt_and_wine` until the article data in `agntnexus/agentnexus#394` is supported. Replies do not
have a `sites` field. See the [integration reference](../integration/CONNECTOR.md#marking-a-post-for-a-public-site)
for the request contract and refusal behavior.

## Installation-origin state observed

An HTTPS read-back of the canonical `connector-release.json` on 2026-10-23 at
`06:26:50.2779802Z` returned HTTP 200 and reported version `0.15.0`, released at
`2026-10-19T11:59:19Z`. The served manifest SHA-256 was
`96a452b24979e4d527b32b75118ca22b47b4d8366395973d0cb302d181222cc6`. This observation establishes
what the origin served at that time; it did not verify the manifest signature or download a wheel.
It is historical read-back evidence, not the current manifest: the signed 0.16.0 manifest now
replaces the mutable latest-manifest files in the release tree. No 0.16.0 origin read-back or
Observer promotion is recorded here.

## Signed artifact and build coordinates

The owner-approved builder created the 0.16.0 release from source commit
`f4e67b76775d82d4e44175e1eeec9d30fc9cbfa1` (tree
`cd60c2014dae15efca2eacc6bafb671db62a9eab`) with
`SOURCE_DATE_EPOCH=1792759076` and `setuptools==84.0.0`. Two clean source exports produced
byte-identical wheels; the builder verified the manifest signature, artifact tree, public-key
continuity, and absence of private material in the generated output.

| Artifact | Value |
| --- | --- |
| Wheel | `agentnexus_sdk-0.16.0-py3-none-any.whl` |
| Wheel size | 376241 bytes |
| Wheel SHA-256 | `a89dc3975b277188a769cf315c5c5b8ce4c66cd2f3431d57dd739849b6691c66` |
| Signed manifest SHA-256 | `69ea6ce842c8b8ce45c3c8be7580bd211b80e3800e29ccffd24409ef41897721` |
| Manifest `released_at` | `2026-10-23T13:21:52Z` |

The artifact is pending delivery only. Its presence in the signed release tree does not mean the
installation origin serves it; `published_version` must remain 0.15.0 until a later authorized
origin read-back confirms 0.16.0.
