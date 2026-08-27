"""Package version and the User-Agent it advertises."""

from __future__ import annotations

from typing import Final

__version__: Final = "0.1.0"

#: Stable User-Agent identifying the SDK and its version. Operators use it to tell an SDK client
#: apart from a hand-rolled one when reading access logs.
USER_AGENT: Final = f"agentnexus-sdk-python/{__version__}"
