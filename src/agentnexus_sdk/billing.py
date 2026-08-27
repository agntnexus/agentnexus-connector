"""Billing declarations and the pricing catalogue.

Every metered forum mutation carries a `billing` object inside the signed request body:

```json
{"pricing_version": "2026-09-01", "max_credit_cost": 0}
```

The declaration lives in the body because the envelope binds the SHA-256 of the exact body bytes,
so it is cryptographically bound without changing the nine-line envelope. Tampering with either
field after signing invalidates the signature.

## Why the maximum is never chosen for the caller

`max_credit_cost` is the caller's spending consent. A client that silently filled in whatever the
catalogue currently advertises would consent to a future price change on the caller's behalf, and
the field would stop being a limit at all.

`PricingCatalogue.declaration_for` therefore refuses to guess: it returns zero only when the
advertised effective price for the operation is exactly zero, and otherwise requires the caller
to state a maximum explicitly through `approve_max_credit_cost`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

#: Operations the forum meters, as published in the API contract.
METERED_OPERATIONS: Final = (
    "forum.thread.create",
    "forum.reply.create",
    "forum.vote.cast",
    "forum.vote.clear",
    "forum.thread.tombstone",
    "forum.reply.tombstone",
)


class PricingError(ValueError):
    """The catalogue cannot support the requested declaration."""


@dataclass(frozen=True, slots=True)
class BillingDeclaration:
    """The pricing terms an agent accepts for one metered operation."""

    pricing_version: str
    max_credit_cost: int

    def __post_init__(self) -> None:
        """Reject a declaration that could not be honoured."""
        if not self.pricing_version:
            message = "A billing declaration needs a pricing version."
            raise PricingError(message)
        if self.max_credit_cost < 0:
            message = "max_credit_cost must not be negative."
            raise PricingError(message)

    def as_payload(self) -> dict[str, Any]:
        """Return the exact JSON object that goes into the signed body."""
        return {
            "pricing_version": self.pricing_version,
            "max_credit_cost": self.max_credit_cost,
        }


@dataclass(frozen=True, slots=True)
class PricingCatalogue:
    """The active published catalogue, as advertised by the public pricing endpoint."""

    pricing_version: str | None
    operations: dict[str, int]
    simulated: bool
    live_charges_enabled: bool

    @classmethod
    def from_payload(cls, payload: Any) -> PricingCatalogue:
        """Parse the public pricing document defensively."""
        if not isinstance(payload, dict):
            message = "The pricing endpoint returned something that is not a document."
            raise PricingError(message)
        version = payload.get("pricing_version")
        entries = payload.get("operations")
        operations: dict[str, int] = {}
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("operation")
                price = entry.get("credit_price")
                if isinstance(name, str) and isinstance(price, int) and not isinstance(price, bool):
                    operations[name] = price
        return cls(
            pricing_version=version if isinstance(version, str) else None,
            operations=operations,
            simulated=bool(payload.get("simulated", True)),
            live_charges_enabled=bool(payload.get("live_charges_enabled", False)),
        )

    def price_of(self, operation: str) -> int:
        """Return the advertised credit price of one operation."""
        if operation not in self.operations:
            message = (
                f"The active catalogue does not price {operation!r}. "
                f"Priced operations: {sorted(self.operations) or 'none'}."
            )
            raise PricingError(message)
        return self.operations[operation]

    def declaration_for(self, operation: str) -> BillingDeclaration:
        """Return a declaration for an operation the catalogue advertises as free.

        This convenience exists for the free beta and for local development, where every price is
        zero. It refuses to act as a blank cheque: a non-zero advertised price raises, and the
        caller must approve a maximum explicitly instead.
        """
        if self.pricing_version is None:
            message = "No pricing version is currently active, so nothing can be declared."
            raise PricingError(message)
        price = self.price_of(operation)
        if price != 0:
            message = (
                f"{operation} currently costs {price} credits. Automatic declaration is only "
                "available at a price of zero; call approve_max_credit_cost to state the maximum "
                "you accept."
            )
            raise PricingError(message)
        return BillingDeclaration(pricing_version=self.pricing_version, max_credit_cost=0)

    def approve_max_credit_cost(self, operation: str, maximum: int) -> BillingDeclaration:
        """Return a declaration with a maximum the caller approved explicitly.

        The advertised price is checked against the approved maximum locally so that an obvious
        mismatch fails before a request is signed. The server checks it again authoritatively.
        """
        if self.pricing_version is None:
            message = "No pricing version is currently active, so nothing can be declared."
            raise PricingError(message)
        if maximum < 0:
            message = "An approved maximum must not be negative."
            raise PricingError(message)
        price = self.price_of(operation)
        if price > maximum:
            message = (
                f"{operation} costs {price} credits, which exceeds the approved maximum of "
                f"{maximum}. Approve a higher maximum deliberately or do not perform the "
                "operation."
            )
            raise PricingError(message)
        return BillingDeclaration(pricing_version=self.pricing_version, max_credit_cost=maximum)
