"""Clock-skew advice.

The server rejects a request signed outside its skew window with `auth.timestamp_stale` or
`auth.timestamp_future`. Both mean the same thing to an operator: this host's clock is wrong.

## What this module deliberately does not do

It does not adjust the signing clock. Taking a correction from a server response would let
whoever controls that response — or anyone who can inject one — move a client's signing time,
which is precisely the freshness property the timestamp exists to provide. The only supported fix
is to correct the system clock, normally with NTP.

The advisory offset below is computed from an HTTPS `Date` header purely so the message can say
*how far* off the clock appears to be. It is reported to a human and never fed back into signing.
"""

from __future__ import annotations

import datetime as dt
from email.utils import parsedate_to_datetime
from typing import Final

#: Largest offset this module will report. Beyond this the number stops being useful advice and
#: starts being noise from a broken or hostile clock source.
MAX_REPORTED_OFFSET_SECONDS: Final = 86_400

SYNCHRONISE_ADVICE: Final = (
    "Synchronise this host's clock, for example with NTP, and try again. The SDK deliberately "
    "does not adjust its signing clock from a server response."
)


def advisory_offset_seconds(date_header: str | None, *, now: dt.datetime) -> float | None:
    """Return how far this host's clock appears to be from the server's, in seconds.

    A positive value means the local clock is ahead. Returns None when the header is absent,
    unparsable, or implausible.
    """
    if not date_header:
        return None
    try:
        server_time = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if server_time.tzinfo is None:
        server_time = server_time.replace(tzinfo=dt.UTC)
    offset = (now - server_time).total_seconds()
    if abs(offset) > MAX_REPORTED_OFFSET_SECONDS:
        return None
    return offset


def skew_advice(offset_seconds: float | None) -> str:
    """Return an actionable message for a stale or future timestamp rejection."""
    if offset_seconds is None:
        return SYNCHRONISE_ADVICE
    direction = "ahead of" if offset_seconds > 0 else "behind"
    return (
        f"This host's clock appears to be about {abs(offset_seconds):.0f} seconds {direction} "
        f"the server's. {SYNCHRONISE_ADVICE}"
    )
