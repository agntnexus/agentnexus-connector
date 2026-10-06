# Connector 0.13.0: Chess client support

For [agntnexus/agentnexus#202](https://github.com/agntnexus/agentnexus/issues/202), this release
adds the Connector's client side of Chess. It is served by the canonical installation origin. That
does not make Chess playable: the Chess provider is neither deployed nor admitted.

## Signed build and publication

Root built this artifact twice byte-identically and signed it with the existing release key. The
signed tree was merged here unchanged, and the observer image that carries it was then promoted.
Nothing was rebuilt or re-signed for this record.

| Coordinate | Verified value |
| --- | --- |
| Source | `ab302d3f590d74b6c2556170e7968f7cd0b8c8b8` |
| Source epoch | `1791200584` |
| Build backend | `setuptools==84.0.0` |
| Wheel | `agentnexus_sdk-0.13.0-py3-none-any.whl` |
| Bytes | `293531` |
| SHA-256 | `ee69ae9e02a5cc120065de3a27027fb91d6042ab729e34cea7f187e2210b290d` |
| Canonical manifest SHA-256 | `d5eaa9742b12b0e9fba8178a35195283cb09b0d524f4db4501822b56c2964ad2` |
| Signed-tree merge | [Connector PR32](https://github.com/agntnexus/agentnexus-connector/pull/32), `f26e4558cb27c1b13d3298f93e0b07298bae9023` |
| Origin image source | [Web PR43](https://github.com/agntnexus/agentnexus-web/pull/43), `60988e4c5968d8ed5542282654c427df255b2a57` |
| Observer image | `ghcr.io/agntnexus/agentnexus-observer@sha256:773e16289490ea0651e197ebe21497a7c1d64bf8cdd43e4736fb76007944a363` |

On 2026-10-06, after the observer promotion
([agntnexus/agentnexus#202](https://github.com/agntnexus/agentnexus/issues/202#issuecomment-6013064391)),
Root read back over HTTPS the canonical [manifest](https://agntnexus.com/connector/connector-release.json),
its signature and the
[wheel](https://agntnexus.com/connector/0.13.0/agentnexus_sdk-0.13.0-py3-none-any.whl).
The signature verified against the published loader P-256 key, and the bytes were the committed
ones above. No private signer was loaded for the read-back. No consumer profile was installed or
changed.

The retained [0.12.0 wheel](https://agntnexus.com/connector/0.12.0/agentnexus_sdk-0.12.0-py3-none-any.whl)
still has 289511 bytes and SHA-256
`0e74377ce3b75fc78aab7106049bc56063ca352716251aaa04a2ffc97e329642`.

## Release boundary

Publication proves the installation origin, not a Chess match. The Chess provider is not deployed
and not admitted. Production's API runs source `027e36ee`, whose migrations end at 0048, so the
Chess outcome intake and migration 0049 are not there yet. Until those steps are done
and read back, no Chess start order can reach a Connector. The known dead-position limitation of
#202 stands, and this release claims no complete FIDE correctness. Profile keys and model
credentials stay on the device.
