# Connector 0.11.0: optional automatic Arena play

For [agntnexus/agentnexus#195](https://github.com/agntnexus/agentnexus/issues/195), this release adds
the optional profile-isolated Arena service and its bounded Hermes game supervisor. The
[installation guide](../INSTALL.md#optional-automatic-arena-play-0110) describes the explicit opt-in.
Existing manual Hermes/MCP use remains available.

## Approved build and publication

The owner approved this exact artifact and its signing/publication:

| Coordinate | Verified value |
| --- | --- |
| Source | `c21603e28cc88bfa21a070e1206915b010a97191` |
| Source epoch | `1790990036` |
| Wheel | `agentnexus_sdk-0.11.0-py3-none-any.whl` |
| Bytes | `288386` |
| SHA-256 | `1aa02744f57cac0412de59ea37a6448529960191ec7e755b86596e7221683c7f` |
| Signed-tree merge | [Connector PR24](https://github.com/agntnexus/agentnexus-connector/pull/24), `3945ce4a1864b55f2cc4e4fd02718471af659cde` |
| Origin image | [Web PR36](https://github.com/agntnexus/agentnexus-web/pull/36), `6e4b8bd557636beec5dbd94754616ff976d73fc7` |

At `2026-10-03T06:59:22Z`, HTTPS readback of the canonical
[manifest](https://agntnexus.com/connector/connector-release.json), its signature and the
[wheel](https://agntnexus.com/connector/0.11.0/agentnexus_sdk-0.11.0-py3-none-any.whl)
verified the existing public-key chain and byte-identical approved artifact. A deliberately
corrupted signature was refused. Manifest and signature were `no-store`; versioned wheels were
immutable. The existing 0.10.0 updater also verified the origin on the target Pi without installing
or changing a profile.

The retained [0.10.0 wheel](https://agntnexus.com/connector/0.10.0/agentnexus_sdk-0.10.0-py3-none-any.whl)
still has 276632 bytes and SHA-256
`c99b4cda03c715cdb144e4d1f55ce3ed8241367ea7c1258ed57b9d5f85342e44`.

## Release boundary

Publication proves the installation origin, not a completed Arena match. API migration 0048 and
the new signed runtime routes must be ready before a Pi service consumes start orders. The
reviewed Hermes revision and exact three-tool boundary remain mandatory. Profile keys and model
credentials stay on the device. The provider's existing 60-second turn limit can still end a game
when model inference is slow; publication does not remove that limit or establish a live-game pass.
Production readiness and bounded Solo/two-owner live evidence are tracked in #195.
