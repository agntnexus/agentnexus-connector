"""Package version and the User-Agent it advertises."""

from __future__ import annotations

from typing import Final

#: The one authoritative connector version. `scripts/build_connector_release.py` defaults to it,
#: so the manifest, the `connector/<version>/` directory, the wheel filename, and the artifact URL
#: all follow from this line.
#:
#: **0.1.0 is published and immutable.** Its wheel is served with
#: `Cache-Control: public, max-age=31536000, immutable`, so those bytes can never be replaced —
#: a cache holding them would keep serving them regardless. Named multi-agent profiles (I-018)
#: therefore ship as a new version at a new address rather than as new bytes at the old one.
__version__: Final = "0.2.0"

#: Stable User-Agent identifying the SDK and its version. Operators use it to tell an SDK client
#: apart from a hand-rolled one when reading access logs.
USER_AGENT: Final = f"agentnexus-sdk-python/{__version__}"
