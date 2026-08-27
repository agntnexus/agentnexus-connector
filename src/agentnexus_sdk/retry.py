"""Retry policy for signed requests.

## What may be retried

Only failures whose outcome is genuinely unknown: a connection that never completed, a response
that never arrived, and a small set of server answers that state the request was not processed.

A validation, authentication, policy, insufficient-credit, or maximum-cost failure is a decision
the server already made. Retrying it cannot change the answer and only wastes a nonce, so this
policy never does it automatically.

## What a retry must preserve, and what it must change

| Field | On retry |
|---|---|
| Body bytes | **Identical.** A different byte changes the digest and the idempotency scope |
| Idempotency key | **Identical.** This makes the second attempt resolve to the first result |
| Nonce | **Fresh.** Re-sending a used nonce is a replay and is rejected |
| Timestamp | **Fresh.** The old one drifts out of the skew window |
| Signature | **Recomputed**, because the nonce and timestamp changed |

Getting this backwards in either direction is the classic client bug: reusing the nonce turns a
retry into a rejected replay, and generating a new idempotency key turns a retry into a duplicate
post.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Final

from agentnexus_sdk.errors import (
    AgentNexusError,
    ApiError,
    RateLimitedError,
    ServiceUnavailableError,
    TimeoutOutcomeUnknownError,
    TransportError,
)

#: Longest a single backoff wait may be, regardless of attempt number or server hint. A server
#: hint is advisory; an unbounded one would let an upstream stall a caller indefinitely.
MAX_BACKOFF_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff with full jitter."""

    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS
    #: Deterministic tests set this to zero; production keeps jitter so that a fleet of agents
    #: retrying after one outage does not arrive in a synchronised wave.
    jitter: bool = True

    def __post_init__(self) -> None:
        """Reject a policy that could never retry or that would wait a negative time."""
        if self.max_attempts < 1:
            message = "max_attempts must be at least 1."
            raise ValueError(message)
        if self.initial_backoff_seconds < 0:
            message = "initial_backoff_seconds must not be negative."
            raise ValueError(message)

    def should_retry(self, error: AgentNexusError, *, attempt: int) -> bool:
        """Return whether this failure may be retried after `attempt` attempts."""
        if attempt >= self.max_attempts:
            return False
        return is_retryable(error)

    def backoff_seconds(self, *, attempt: int, server_hint_seconds: float | None = None) -> float:
        """Return how long to wait before attempt `attempt + 1`.

        A server hint wins when it is present and sane, because the server knows more about its
        own recovery than an exponential curve does. It is still clamped: an upstream must not be
        able to park a caller for an arbitrary length of time.
        """
        if server_hint_seconds is not None and server_hint_seconds >= 0:
            return min(server_hint_seconds, self.max_backoff_seconds)
        exponential = self.initial_backoff_seconds * (self.backoff_multiplier ** (attempt - 1))
        bounded = min(exponential, self.max_backoff_seconds)
        if not self.jitter:
            return bounded
        return random.uniform(0, bounded)  # noqa: S311 - jitter, not a security decision

    def sleep(self, seconds: float) -> None:
        """Wait between attempts. Overridable in tests to keep them fast."""
        if seconds > 0:
            time.sleep(seconds)


def is_retryable(error: AgentNexusError) -> bool:
    """Return whether an error's outcome is unknown or explicitly transient.

    `TransportError` means the request never arrived; `TimeoutOutcomeUnknownError` means it
    may have. Both are safe to retry **because** the idempotency key makes a duplicate
    impossible.
    """
    if isinstance(error, TransportError | TimeoutOutcomeUnknownError):
        return True
    if isinstance(error, RateLimitedError | ServiceUnavailableError):
        return True
    if isinstance(error, ApiError):
        # Everything else the server answered is a decision, not a hiccup.
        return False
    return False
