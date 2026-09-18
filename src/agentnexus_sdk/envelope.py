"""Independent implementation of the `agentnexus-sig-v1` canonical string.

This module is written from the published normative specification (`docs/ai/SIGNED_REQUESTS.md`)
and is verified against the published test vectors. It deliberately imports nothing from the
AgentNexus server: a client that reuses the server's own construction proves only that the code
agrees with itself, not that the published protocol is implementable.

The envelope is exactly nine lines joined by a single line feed, with no trailing separator:

```text
agentnexus-sig-v1
<method>
<target>
<agent_id>
<key_id>
<timestamp>
<nonce>
<idempotency_key>
<body_sha256>
```

Two properties matter more than anything else here and are enforced rather than assumed:

- **The hashed bytes are the transmitted bytes.** There is no JSON canonicalisation, no key
  reordering, and no whitespace normalisation. `EnvelopeInput.body` is `bytes`, so a caller
  physically cannot hand this module one object and the transport another.
- **The target is verbatim.** The percent-encoded path is used as given and the raw query string
  is appended unchanged, so `?a=1&b=2` and `?b=2&a=1` are different signed targets.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Final

PROTOCOL_VERSION: Final = "agentnexus-sig-v1"

#: The single separator. Written as an escape so that a stray editor conversion to CRLF in this
#: file cannot silently change the protocol.
LINE_SEPARATOR: Final = "\n"

TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"

#: Nonce and idempotency key character set and bounds, from the published specification.
TOKEN_PATTERN: Final = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")

_UUID_PATTERN: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TIMESTAMP_PATTERN: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_METHOD_PATTERN: Final = re.compile(r"^[A-Z]+$")

#: Characters that must never appear in a signed field or an outgoing header value. A newline in
#: a header is request splitting; a newline in a signed field would let one field impersonate the
#: next line of the envelope.
_FORBIDDEN_IN_FIELD: Final = re.compile(r"[\x00-\x1f\x7f]")


class ProtocolError(ValueError):
    """The envelope could not be constructed from the supplied values.

    This is raised before anything is signed or sent. It never carries key material: every value
    it reports is a public request field.
    """


@dataclass(frozen=True, slots=True)
class EnvelopeInput:
    """Everything the canonical string binds.

    ``body`` is bytes on purpose. The digest must cover exactly what goes on the wire, and the
    only way to guarantee that in a type system is to refuse to accept anything that still needs
    serialising.
    """

    method: str
    path: str
    query_string: str
    agent_id: str
    key_id: str
    timestamp: dt.datetime
    nonce: str
    idempotency_key: str
    body: bytes


@dataclass(frozen=True, slots=True)
class Envelope:
    """A validated canonical string and the header values that must accompany it."""

    method: str
    target: str
    agent_id: str
    key_id: str
    timestamp: str
    nonce: str
    idempotency_key: str
    body_sha256: str

    @property
    def canonical_string(self) -> str:
        """Return the nine-line string exactly as it is signed."""
        return LINE_SEPARATOR.join(
            (
                PROTOCOL_VERSION,
                self.method,
                self.target,
                self.agent_id,
                self.key_id,
                self.timestamp,
                self.nonce,
                self.idempotency_key,
                self.body_sha256,
            )
        )

    def signing_bytes(self) -> bytes:
        """Return the UTF-8 bytes that Ed25519 signs."""
        return self.canonical_string.encode("utf-8")

    def headers(self, *, signature_base64: str) -> dict[str, str]:
        """Return the signature headers for this envelope.

        Every value is re-validated here. A header that reaches this point with a control
        character would be a request-splitting vector, and the check costs nothing.
        """
        headers = {
            "X-Agent-Signature-Version": PROTOCOL_VERSION,
            "X-Agent-ID": self.agent_id,
            "X-Agent-Key-ID": self.key_id,
            "X-Agent-Timestamp": self.timestamp,
            "X-Agent-Nonce": self.nonce,
            "Idempotency-Key": self.idempotency_key,
            "X-Agent-Signature": signature_base64,
        }
        for name, value in headers.items():
            _reject_control_characters(value, field=name)
        return headers


def build_envelope(request: EnvelopeInput) -> Envelope:
    """Validate the request parts and return the canonical envelope.

    Validation happens before signing so that a malformed value produces a local error the caller
    can fix, rather than an opaque `auth.headers_malformed` from a server the caller may not be
    able to see logs for.
    """
    method = _validate_method(request.method)
    target = _validate_target(request.path, request.query_string)
    return Envelope(
        method=method,
        target=target,
        agent_id=_validate_uuid(request.agent_id, field="agent_id"),
        key_id=_validate_uuid(request.key_id, field="key_id"),
        timestamp=format_timestamp(request.timestamp),
        nonce=_validate_token(request.nonce, field="nonce"),
        idempotency_key=_validate_token(request.idempotency_key, field="idempotency_key"),
        body_sha256=body_digest(request.body),
    )


def body_digest(body: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 of the exact body bytes.

    The runtime check looks redundant next to the annotation, and it is not: this module is used
    by callers who are not type-checked, and accepting a `str` here would silently reintroduce
    the encoding question the protocol exists to remove.
    """
    if not isinstance(body, bytes | bytearray):
        message = "The body must be bytes so that the hashed bytes are the transmitted bytes."  # type: ignore[unreachable]
        raise ProtocolError(message)
    return hashlib.sha256(bytes(body)).hexdigest()


def format_timestamp(moment: dt.datetime) -> str:
    """Format an instant as the protocol's second-precision UTC timestamp.

    A naive datetime is rejected rather than assumed to be UTC. Assuming a timezone is how a
    client ends up signing a timestamp that is hours outside the skew window on one machine and
    correct on another.
    """
    if moment.tzinfo is None:
        message = "The timestamp must be timezone-aware; a naive datetime is ambiguous."
        raise ProtocolError(message)
    return moment.astimezone(dt.UTC).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: str) -> dt.datetime:
    """Parse a protocol timestamp back into an aware datetime."""
    if _TIMESTAMP_PATTERN.match(value) is None:
        message = f"A protocol timestamp must look like 2026-08-27T12:00:00Z, not {value!r}."
        raise ProtocolError(message)
    return dt.datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=dt.UTC)


def new_nonce() -> str:
    """Return a fresh unpredictable nonce.

    A UUID4 hex string is 32 characters of the permitted alphabet and carries 122 bits of
    entropy, which is far beyond what a replay window of minutes requires.
    """
    return f"nonce-{uuid.uuid4().hex}"


def new_idempotency_key() -> str:
    """Return a fresh idempotency key."""
    return f"idem-{uuid.uuid4().hex}"


def _validate_method(method: str) -> str:
    upper = method.upper()
    if _METHOD_PATTERN.match(upper) is None:
        message = f"An HTTP method must be uppercase ASCII letters, not {method!r}."
        raise ProtocolError(message)
    return upper


def _validate_target(path: str, query_string: str) -> str:
    """Join the path and raw query string without normalising either.

    There is deliberately no normalisation: no segment decoding, no trailing-slash handling, and
    no query reordering. Every normalisation step is a place where the client and the server can
    disagree about what was signed.
    """
    if not path.startswith("/"):
        message = f"The request path must be absolute, not {path!r}."
        raise ProtocolError(message)
    _reject_control_characters(path, field="path")
    if query_string == "":
        return path
    _reject_control_characters(query_string, field="query_string")
    if query_string.startswith("?"):
        message = "The query string must be supplied without its leading '?'."
        raise ProtocolError(message)
    return f"{path}?{query_string}"


def _validate_uuid(value: str, *, field: str) -> str:
    if _UUID_PATTERN.match(value) is None:
        message = f"The {field} must be a lowercase hyphenated UUID, not {value!r}."
        raise ProtocolError(message)
    return value


def _validate_token(value: str, *, field: str) -> str:
    if TOKEN_PATTERN.match(value) is None:
        message = (
            f"The {field} must be 8 to 128 characters of A-Za-z0-9._~- "
            f"(received {len(value)} characters)."
        )
        raise ProtocolError(message)
    return value


def _reject_control_characters(value: str, *, field: str) -> None:
    if _FORBIDDEN_IN_FIELD.search(value) is not None:
        message = f"The {field} contains a control character, which is never valid here."
        raise ProtocolError(message)
