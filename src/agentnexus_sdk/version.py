"""Package version and the User-Agent it advertises."""

from __future__ import annotations

from typing import Final

#: The one authoritative connector version. `scripts/build_connector_release.py` defaults to it,
#: so the manifest, the `connector/<version>/` directory, the wheel filename, and the artifact URL
#: all follow from this line.
#:
#: **0.1.0, 0.2.0 and 0.2.1 are published and immutable.** Their wheels are served with
#: `Cache-Control: public, max-age=31536000, immutable`, so those bytes can never be replaced —
#: a cache holding them would keep serving them regardless. Every release therefore ships at a new
#: address rather than replacing old bytes.
#:
#: 0.3.0 is a minor bump rather than a patch. It withdraws behaviour that shipped: `--destroy-key`
#: still parses and now refuses before touching anything, because the version that shipped deleted
#: a private key on a typed confirmation alone, without ever asking whether the server had revoked
#: it. Removing a capability somebody may have scripted is a breaking change even when the flag is
#: still recognised. It also adds safe profile removal with key quarantine.
__version__: Final = "0.3.0"

#: Stable User-Agent identifying the SDK and its version. Operators use it to tell an SDK client
#: apart from a hand-rolled one when reading access logs.
USER_AGENT: Final = f"agentnexus-sdk-python/{__version__}"
