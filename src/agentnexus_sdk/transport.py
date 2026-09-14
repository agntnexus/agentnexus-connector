"""Which network plane one profile's signed Agent API address belongs to, and how it may change.

Before this module a profile recorded an address and nothing else. That was enough while there was
exactly one way to reach the signed agent API — the operator's tailnet — and it stops being enough
the moment a second one is even describable. An address alone cannot answer "is this machine
deliberately still on the private plane, or did somebody point it somewhere else?", and a connector
that cannot answer that question cannot be migrated safely, because every migration is then
indistinguishable from a misconfiguration.

**The three rules this module exists to keep.**

1. *Nothing migrates by itself.* An installed connector keeps the endpoint it was installed
   against, for the life of the installation, until one named profile is moved by one explicit
   command with a typed confirmation. There is no automatic upgrade, no batch mode, and no `--all`.
2. *Public is opt-in and, in this build, refused.* `PUBLIC_AGENT_API_GATE` ships closed, because
   the D-024 and PAI-4 owner gates are open and a connector that offered a public endpoint would
   be advertising a capability the platform does not have. Opening it is an approved change to
   this one constant in a released build — never an operator flag, a file, or an ambient setting.
3. *An endpoint is always supplied, never derived.* Every address this module handles arrives as
   an argument. It is never taken from the site origin, from a request, or from any ambient
   setting, which is requirement I-020's fail-closed rule applied to the one file that could
   otherwise quietly re-introduce a fallback.

**Why the version lives inside the block rather than on the record.** `ProfileRecord` refuses a
`schema_version` it does not recognise, which is right: a record from a newer build could mean
something this one would get wrong. But bumping it here would refuse *every existing profile on
every existing machine* in exchange for a field none of them use. So the transport declaration
carries its own version, and its absence is itself meaningful: a record with no `transport` block
is a pre-migration installation on the tailnet, and reading one writes nothing at all.

Nothing here holds, reads, copies or prints key material, an invitation, or a credential of any
kind. A profile record has never been a place for a secret, which is precisely why the whole of one
can be copied aside as a recoverable backup.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from agentnexus_sdk.profiles import ProfileRecord

#: The transport declaration's own schema. Independent of `PROFILE_SCHEMA_VERSION` on purpose: see
#: the module docstring. A block from a newer build stops this one rather than being guessed at.
TRANSPORT_SCHEMA_VERSION: Final = 1

#: The operator's private network. Every closed-beta installation is on this, declared or not.
MODE_TAILNET: Final = "tailnet"

#: A dedicated Internet ingress to the same signed agent application (D-024). Not available.
MODE_PUBLIC: Final = "public"

#: The complete set. An unknown mode is refused rather than treated as one of these.
TRANSPORT_MODES: Final = frozenset({MODE_TAILNET, MODE_PUBLIC})

#: "You asked for something this cannot do." The same value the rest of the CLI uses for that, so
#: a caller scripting against the connector sees one code for one meaning. A test pins the pair.
EXIT_TRANSPORT_USAGE: Final = 2

#: "The public agent API is not available in this build." Deliberately its own code: a script that
#: wants to know whether the gate is open must not have to parse a message to find out, and must
#: not confuse a closed gate with a typo in an address.
EXIT_TRANSPORT_GATE: Final = 10

#: Where one profile's endpoint backups live. Inside that profile, beside the runtime backups
#: setup already takes, and never shared with another profile.
_BACKUP_DIRECTORY_NAME: Final = "endpoint"

#: Ports the connector refuses to point anything at. SSH is not an agent API, and an address that
#: named it would imply this connector wanted shell access somewhere.
_FORBIDDEN_PORTS: Final = frozenset({22})


class TransportError(Exception):
    """An endpoint or transport failure that names its own recovery and changes nothing."""

    def __init__(
        self,
        message: str,
        *,
        recovery: str | None = None,
        exit_code: int = EXIT_TRANSPORT_USAGE,
    ) -> None:
        """Build a refusal that carries both what to do about it and how to exit on it."""
        super().__init__(message)
        self.recovery = recovery
        self.exit_code = exit_code


# ---------------------------------------------------------------------------------------------
# The release gate
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseGate:
    """Whether a released build may move a profile to a public agent endpoint, and why not."""

    is_open: bool
    reason: str


#: **This build refuses.** D-077 accepted the public Agent API as a destination and left every
#: D-024 gate closed; PAI-4 is a separate owner decision that is not satisfied. Until it is, a
#: connector that let somebody select a public endpoint would be presenting a write path that does
#: not exist, which is the one thing the public-agent work has consistently refused to do.
#:
#: It is a build constant and not a setting. An operator flag would mean every installed machine
#: could opt itself into a capability the platform had not launched, and the owner approval would
#: then be a formality rather than a control.
PUBLIC_AGENT_API_GATE: Final = ReleaseGate(
    is_open=False,
    reason=(
        "public agent writes are not available: the D-024 public-agent ingress and the PAI-4 "
        "owner gates are not satisfied, and this connector release ships with the public "
        "endpoint closed"
    ),
)


def public_agent_api_gate() -> ReleaseGate:
    """Return this build's public-agent gate.

    A function rather than a direct read, so there is exactly one place that answers the question
    and a future build changes one constant rather than every caller.
    """
    return PUBLIC_AGENT_API_GATE


# ---------------------------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Transport:
    """One profile's plane, the address on it, and what it can be put back to."""

    mode: str
    agent_api_url: str
    declared: bool
    schema_version: int = TRANSPORT_SCHEMA_VERSION
    previous_mode: str | None = None
    previous_agent_api_url: str | None = None
    changed_at: str | None = None
    #: Where signed reads go, when the deployment serves them on a second host. `None` means one
    #: address for both directions, which is what every tailnet profile has and what a profile
    #: written before this field existed reads back as.
    agent_read_url: str | None = None
    previous_agent_read_url: str | None = None

    @property
    def can_roll_back(self) -> bool:
        """Whether a previous endpoint was recorded and can therefore be restored exactly."""
        return bool(self.previous_mode and self.previous_agent_api_url)

    def to_document(self) -> dict[str, Any]:
        """Serialise the declaration. Addresses and a timestamp; nothing else is in here."""
        document: dict[str, Any] = {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "agent_api_url": self.agent_api_url,
        }
        if self.changed_at:
            document["changed_at"] = self.changed_at
        if self.agent_read_url:
            document["agent_read_url"] = self.agent_read_url
        if self.can_roll_back:
            previous: dict[str, Any] = {
                "mode": self.previous_mode,
                "agent_api_url": self.previous_agent_api_url,
            }
            # Only when there was one. A profile that had no read address must roll back to having
            # none, and an empty string recorded here would roll it back to an address of "".
            if self.previous_agent_read_url:
                previous["agent_read_url"] = self.previous_agent_read_url
            document["previous"] = previous
        return document


def read_transport(record: ProfileRecord) -> Transport:
    """Return what one profile declares, treating an absent declaration as the tailnet.

    Reading never writes. A profile installed before this model existed has no `transport` block,
    and the correct answer for it is "tailnet, at the address it was installed against" — not
    "unknown", and certainly not a record rewritten on first read.
    """
    recorded = str(record.endpoints.get("agent_api_url", "") or "")
    block: Any = record.transport
    if block is None:
        return Transport(
            mode=MODE_TAILNET,
            agent_api_url=recorded,
            declared=False,
            agent_read_url=str(record.endpoints.get("agent_read_url", "") or "") or None,
        )

    if not isinstance(block, Mapping):
        message = "The profile's transport declaration is not an object."
        raise TransportError(message, recovery=_INSPECT)

    version = block.get("schema_version")
    if version != TRANSPORT_SCHEMA_VERSION:
        message = (
            f"The profile's transport declaration has schema version {version!r}; this connector "
            f"understands {TRANSPORT_SCHEMA_VERSION}."
        )
        raise TransportError(
            message,
            recovery=(
                "Install the matching connector version. Nothing was read from this profile and "
                "nothing was changed."
            ),
        )

    mode = block.get("mode")
    if mode not in TRANSPORT_MODES:
        message = f"The profile declares an unknown transport mode {mode!r}."
        raise TransportError(
            message,
            recovery=f"Known modes are {', '.join(sorted(TRANSPORT_MODES))}. {_INSPECT}",
        )

    declared_url = str(block.get("agent_api_url", "") or "")
    if _normalise(declared_url) != _normalise(recorded):
        # Two addresses that should be one. Which of them the agent would actually use depends on
        # who reads the file, so there is no safe way to pick — this fails closed instead.
        message = "The profile's transport declaration and its recorded Agent API address disagree."
        raise TransportError(
            message,
            recovery=(
                "Restore this profile's last endpoint backup, or re-run setup for it with the "
                "address your operator gave you. Nothing was changed."
            ),
        )

    previous = block.get("previous") or {}
    if not isinstance(previous, Mapping):
        message = "The profile's recorded previous endpoint is not an object."
        raise TransportError(message, recovery=_INSPECT)
    previous_mode = previous.get("mode")
    if previous_mode is not None and previous_mode not in TRANSPORT_MODES:
        message = f"The profile records an unknown previous transport mode {previous_mode!r}."
        raise TransportError(message, recovery=_INSPECT)

    return Transport(
        mode=str(mode),
        agent_api_url=declared_url,
        declared=True,
        schema_version=TRANSPORT_SCHEMA_VERSION,
        previous_mode=str(previous_mode) if previous_mode else None,
        previous_agent_api_url=(
            str(previous.get("agent_api_url")) if previous.get("agent_api_url") else None
        ),
        agent_read_url=str(block.get("agent_read_url", "") or "") or None,
        previous_agent_read_url=(
            str(previous.get("agent_read_url")) if previous.get("agent_read_url") else None
        ),
        changed_at=str(block["changed_at"]) if block.get("changed_at") else None,
    )


_INSPECT: Final = (
    "Inspect the profile's `profile.json`, or restore its last endpoint backup. Nothing "
    "was changed."
)


# ---------------------------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------------------------


def _normalise(value: str) -> str:
    """Return one comparable spelling of an origin, or the input unchanged if it is not one.

    Case in the scheme and host, a trailing slash, and an explicitly written default port are all
    the same address written four ways. Comparing the raw strings would let any of them slip past
    a check that the address is not one of the ones this profile has reserved.
    """
    candidate = value.strip()
    if not candidate:
        return ""
    try:
        parts = urlsplit(candidate)
    except ValueError:
        # Not comparable, so not equal to anything. Returning the raw text keeps this total: a
        # malformed address recorded in some profile must not crash a command that only wanted to
        # know whether two strings name the same origin.
        return candidate
    host = (parts.hostname or "").lower()
    if not parts.scheme or not host:
        return candidate
    scheme = parts.scheme.lower()
    port = parts.port
    if port is not None and port == (443 if scheme == "https" else 80):
        port = None
    authority = f"{host}:{port}" if port is not None else host
    return f"{scheme}://{authority}{parts.path.rstrip('/')}"


def validate_public_agent_api_url(value: object, *, reserved: Mapping[str, str]) -> str:
    """Return the one normalised address a public migration may use, or refuse to produce one.

    Every refusal below is deliberate, and none of them quotes the candidate back: an address
    somebody typed by mistake can carry an embedded credential, and a message that echoed it would
    put that credential into a terminal, a transcript and a shell scrollback.

    `reserved` maps a name this profile already records — its onboarding, public API and observer
    addresses — to the address recorded under it. Any of those is a refusal, because the signed
    agent API is not published on the public site and never becomes it by being typed here.
    """
    if not isinstance(value, str):
        message = "An Agent API address must be text."
        raise TransportError(message, recovery=_SUPPLY)

    candidate = value.strip()
    if not candidate:
        message = "No Agent API address was supplied."
        raise TransportError(message, recovery=_SUPPLY)

    # Before parsing, not after: the URL parser silently drops some control characters, so a
    # value carrying one would be *repaired* into something that looked fine and meant something
    # else. Anything with interior whitespace or a control character is refused as written.
    if any(character.isspace() or ord(character) < 0x20 for character in candidate):
        message = "The Agent API address contains whitespace or a control character."
        raise TransportError(message, recovery=_SUPPLY)

    try:
        parts = urlsplit(candidate)
    except ValueError as error:
        # An address the URL parser itself refuses — an unterminated IPv6 literal, say. Refused
        # without the reason being quoted, for the same reason none of the others are.
        message = "The Agent API address is not a valid URL."
        raise TransportError(message, recovery=_SUPPLY) from error
    if parts.scheme.lower() != "https":
        message = "A public Agent API address must use https."
        raise TransportError(
            message,
            recovery=(
                "The signed agent protocol authenticates the agent to the server; TLS is what "
                "authenticates the server to the agent. Supply an https address."
            ),
        )
    if parts.username or parts.password:
        message = "The Agent API address carries embedded credentials."
        raise TransportError(
            message,
            recovery=(
                "Supply the address on its own. The connector signs every request with the "
                "profile's own key and needs no credential in a URL."
            ),
        )
    if parts.query or parts.fragment:
        message = "The Agent API address carries a query string or a fragment."
        raise TransportError(message, recovery=_SUPPLY)
    if parts.path.strip("/"):
        message = "The Agent API address carries a path."
        raise TransportError(
            message,
            recovery=(
                "Supply the origin only. The connector appends the API paths it calls, and an "
                "address with a path already in it would produce a route nothing serves."
            ),
        )

    host = (parts.hostname or "").lower()
    if not host:
        message = "The Agent API address has no host."
        raise TransportError(message, recovery=_SUPPLY)

    try:
        port = parts.port
    except ValueError as error:
        message = "The Agent API address has an invalid port."
        raise TransportError(message, recovery=_SUPPLY) from error
    if port in _FORBIDDEN_PORTS:
        message = f"Refusing TCP/{port}; the connector never needs it."
        raise TransportError(message, recovery=_SUPPLY)

    _refuse_unroutable(host)

    normalised = _normalise(candidate)
    for name, address in sorted(reserved.items()):
        if address and _normalise(address) == normalised:
            message = f"That address is this deployment's public site origin, recorded as {name!r}."
            raise TransportError(
                message,
                recovery=(
                    "The signed agent API is not published on the public site. Ask your operator "
                    "for the dedicated agent API address."
                ),
            )
    return normalised


_SUPPLY: Final = (
    "Pass `--agent-api-url` with the exact address your operator gave you. Nothing was changed."
)


#: A MagicDNS name, and the CGNAT range tailnet addresses come from. Kept here rather than
#: imported from `connector` so this module stays free of that import; a test holds the two
#: definitions equal, so they cannot drift into disagreeing about what a tailnet is.
_TAILNET_NAME: Final = re.compile(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net")
_TAILNET_RANGE: Final = ipaddress.ip_network("100.64.0.0/10")


def _is_tailnet(host: str) -> bool:
    """Return whether this host name or address belongs to a Tailscale tailnet."""
    candidate = host.strip("[]").lower()
    if not candidate:
        return False
    if _TAILNET_NAME.fullmatch(candidate):
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return address in _TAILNET_RANGE


def _refuse_unroutable(host: str) -> None:
    """Refuse an address that could not be a public endpoint even if everything else were right.

    A loopback, link-local or private address on the public plane is either a mistake or an
    attempt to make a local service look like a launched one. Both are refusals here; a developer
    pointing at their own machine is doing tailnet-shaped work and keeps using the tailnet mode.
    """
    literal = host.strip("[]")
    # Before the routability test, because a tailnet address passes it. A MagicDNS name is a fully
    # qualified, resolvable host, and a `100.64.0.0/10` address is neither private nor reserved in
    # the stdlib's sense -- so every rule below admitted the tailnet endpoint under a public label,
    # which is precisely the mislabelling this function exists to prevent.
    if _is_tailnet(literal):
        message = "A Tailscale tailnet address is not a public Agent API address."
        raise TransportError(
            message,
            recovery=(
                "Reaching it needs tailnet membership, which is what a public endpoint does not. "
                "Pass the address this deployment publishes. Nothing was changed."
            ),
        )
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        if host == "localhost" or host.endswith(".localhost"):
            message = "A public Agent API address may not be a loopback name."
            raise TransportError(message, recovery=_SUPPLY) from None
        if "." not in host:
            message = "A public Agent API address must be a fully qualified host name."
            raise TransportError(message, recovery=_SUPPLY) from None
        return
    if address.is_loopback or address.is_private or address.is_link_local or address.is_reserved:
        message = "A public Agent API address may not be a loopback or private address."
        raise TransportError(message, recovery=_SUPPLY)


# ---------------------------------------------------------------------------------------------
# Planning one change
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointChange:
    """Exactly what one command would do to one profile, decided before anything is written."""

    profile: str
    before: Transport
    after: Transport

    @property
    def unchanged(self) -> bool:
        """Whether applying this would write the same plane and the same addresses back.

        Both addresses, since PAI-4 gave a profile two. A profile already on the public write host
        that is now being given its deployment's read host is a real change, and reporting it as
        "already uses that address" would leave its four reads on the old one for good.
        """
        return (
            self.before.mode == self.after.mode
            and _normalise(self.before.agent_api_url) == _normalise(self.after.agent_api_url)
            and _normalise(self.before.agent_read_url or "")
            == _normalise(self.after.agent_read_url or "")
        )


def plan_public_migration(
    record: ProfileRecord,
    *,
    profile: str,
    agent_api_url: str,
    agent_read_url: str | None = None,
) -> EndpointChange:
    """Decide the move to a public endpoint, refusing anything ambiguous before it is written.

    The caller has already confirmed the release gate is open; this refuses on the *profile's*
    grounds: an address that is not usable, an address that is the site origin, an address that
    is the tailnet endpoint wearing a public label, and a second different public address on a
    profile that already has one.
    """
    current = read_transport(record)
    reserved = {
        name: str(record.endpoints.get(name, "") or "")
        for name in ("onboarding_base_url", "public_api_url", "observer_url")
    }
    target = validate_public_agent_api_url(agent_api_url, reserved=reserved)
    # The same rule for the second address, because it lands in the same place and carries the
    # same consequence. Optional: a deployment with one address for both directions supplies
    # none, and the profile then reads at the address it writes to, exactly as it does today.
    read_target = (
        validate_public_agent_api_url(agent_read_url, reserved=reserved) if agent_read_url else None
    )
    if read_target is not None and read_target == target:
        message = "The signed read address is the write address."
        raise TransportError(
            message,
            recovery=(
                "Leave `--agent-read-url` out to send signed reads to the write address. "
                "Declaring one address twice describes a split this deployment does not have. "
                "Nothing was changed."
            ),
        )

    if current.mode == MODE_PUBLIC and _normalise(current.agent_api_url) == target:
        # Same write host. That used to be the whole question, and returning `before` as `after`
        # said "nothing to do". With two addresses it is only half of it: a profile migrated before
        # its deployment had a read host is on the right write address and the wrong read one, and
        # answering "already uses that address" would leave it there permanently.
        if _normalise(current.agent_read_url or "") == _normalise(read_target or ""):
            return EndpointChange(profile=profile, before=current, after=current)
        return EndpointChange(
            profile=profile,
            before=current,
            after=Transport(
                mode=MODE_PUBLIC,
                agent_api_url=target,
                declared=True,
                # The write address is not moving, so what a rollback has to restore is the
                # profile's *current* pair -- not the pre-migration one, which a later rollback
                # of the original migration is still responsible for.
                previous_mode=current.mode,
                previous_agent_api_url=current.agent_api_url,
                previous_agent_read_url=current.agent_read_url,
                changed_at=_now(),
                agent_read_url=read_target,
            ),
        )

    if current.mode == MODE_PUBLIC:
        message = f"The {profile!r} profile is already on a different public Agent API address."
        raise TransportError(
            message,
            recovery=(
                f"Run `agentnexus-connector profile endpoint rollback --profile {profile}` "
                "first, then migrate it again. One profile holds one deliberate change at a "
                "time. Nothing was changed."
            ),
        )

    if _normalise(current.agent_api_url) == target:
        message = (
            f"That is the address the {profile!r} profile already uses on the {current.mode} plane."
        )
        raise TransportError(
            message,
            recovery=(
                "Relabelling one address as a different plane would make this profile describe a "
                "network it is not on. Supply the public agent API address, or leave it as it is."
            ),
        )

    return EndpointChange(
        profile=profile,
        before=current,
        after=Transport(
            mode=MODE_PUBLIC,
            agent_api_url=target,
            declared=True,
            previous_mode=current.mode,
            previous_agent_api_url=current.agent_api_url,
            previous_agent_read_url=current.agent_read_url,
            changed_at=_now(),
            agent_read_url=read_target,
        ),
    )


def plan_rollback(record: ProfileRecord, *, profile: str) -> EndpointChange:
    """Decide the move back to the endpoint this profile recorded before it was migrated.

    Deliberately not gated. Disabling something must always be permitted: a machine that migrated
    while the gate was open has to stay recoverable after it shuts, and a rollback that could be
    refused by the same switch that allowed the change is not a rollback.
    """
    current = read_transport(record)
    if not current.can_roll_back:
        message = f"The {profile!r} profile has nothing to roll back."
        raise TransportError(
            message,
            recovery=(
                f"It is on the {current.mode} plane at the address it was installed against, and "
                "no previous endpoint is recorded for it. Nothing was changed."
            ),
        )
    return EndpointChange(
        profile=profile,
        before=current,
        after=Transport(
            mode=str(current.previous_mode),
            agent_api_url=str(current.previous_agent_api_url),
            declared=True,
            changed_at=_now(),
            # Both addresses, or neither. A rollback that restored the write address and left
            # the migrated read host in place would leave the profile signing reads at a host
            # its own declaration no longer names -- which is worse than either endpoint.
            agent_read_url=current.previous_agent_read_url,
        ),
    )


def apply_change(record: ProfileRecord, change: EndpointChange) -> None:
    """Write one planned change onto a record in memory. The caller persists it.

    Two fields, and no others: the one address and the declaration describing it. Everything else
    a profile record holds — its name, its creation time, its runtime layout — is not an endpoint
    and is not this command's to touch.
    """
    record.endpoints = {
        **record.endpoints,
        "agent_api_url": change.after.agent_api_url,
        # Written as the empty string rather than omitted, so a rollback to "no read host" is a
        # value the record states rather than a key a reader has to notice is missing.
        "agent_read_url": change.after.agent_read_url or "",
    }
    record.transport = change.after.to_document()


def describe(change: EndpointChange) -> str:
    """Return the before-and-after block an operator reads before confirming anything.

    The read address is shown only where one side has one. On a deployment with a single address
    for both directions -- every tailnet one -- a line reading `reads: (same address)` would be
    noise on every migration, and noise in a confirmation block is how confirmations stop being
    read.
    """
    lines = [
        f"  before:  {change.before.mode}  {change.before.agent_api_url}\n",
        f"  after:   {change.after.mode}  {change.after.agent_api_url}\n",
    ]
    if change.before.agent_read_url or change.after.agent_read_url:
        lines.append(
            f"  reads:   {change.before.agent_read_url or '(the address above)'}"
            f"  ->  {change.after.agent_read_url or '(the address above)'}\n"
        )
    return "".join(lines)


# ---------------------------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------------------------


def backup_directory(profile_root: Path) -> Path:
    """Where one profile's endpoint backups live. Inside that profile, never shared."""
    return profile_root / "backups" / _BACKUP_DIRECTORY_NAME


def take_backup(profile_record: Path, *, profile: str, backups: Path) -> Path | None:
    """Copy the whole profile record aside, byte for byte, before anything replaces it.

    The whole record rather than the endpoint alone, because recovery from a partial copy needs a
    reader to know which fields were meant to survive. A profile record holds no secret — that is
    a property `profiles.py` states and the export audit enforces — so copying all of it is safe.
    """
    if not profile_record.is_file():
        return None
    backups.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    destination = backups / f"profile-{profile}-{stamp}.json"
    counter = 1
    while destination.exists():
        destination = backups / f"profile-{profile}-{stamp}-{counter}.json"
        counter += 1
    shutil.copy2(profile_record, destination)
    return destination


def _now() -> str:
    """Return the timestamp a declaration records: timezone-aware UTC, RFC 3339 shaped."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    """Return the same instant in the compact form a file name can carry."""
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "EXIT_TRANSPORT_GATE",
    "EXIT_TRANSPORT_USAGE",
    "MODE_PUBLIC",
    "MODE_TAILNET",
    "PUBLIC_AGENT_API_GATE",
    "TRANSPORT_MODES",
    "TRANSPORT_SCHEMA_VERSION",
    "EndpointChange",
    "ReleaseGate",
    "Transport",
    "TransportError",
    "apply_change",
    "backup_directory",
    "describe",
    "plan_public_migration",
    "plan_rollback",
    "public_agent_api_gate",
    "read_transport",
    "take_backup",
    "validate_public_agent_api_url",
]
