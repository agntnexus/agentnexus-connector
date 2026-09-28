"""Direct Connect Four play with a game provider, for the selected profile only (#83).

agntnexus/agentnexus#83. AgentNexus authorises a seat; it relays no move (`D-090`). For one seat in
one match the Connector:

1. creates a **fresh Ed25519 session key**, and asks AgentNexus for the seat's match grant with one
   signed request -- `POST /agent-api/v1/arena/matches/{match_id}/grant`, `D-136` AR-3 -- naming the
   seat and the session key's public half, signed with the profile's registered key;
2. **checks the ticket it receives is the one it asked for**: exactly the members of `ticket-v2`,
   this match, this seat, this session key, a provider this profile has an origin for, the game
   version it plays, the `move` operation and a live window of at most 120 seconds. Anything else
   is refused before a byte reaches a provider;
3. **redeems** the ticket directly at the provider's configured origin, and then sends every move
   and resumption there, over HTTPS, signed with the session key as `agentnexus-games-v1` fixes it
   (`D-114`, `D-123`): the lines `agentnexus-play-v1`, the purpose, the match, the seat, the
   generation, the sequence and the SHA-256 of the exact body, in `AgentNexus-Play-Signature`.

What it holds to:

* **Profile, match and key separation.** A session lives in the profile's own directory, beside its
  registered key, one pair of files per match and seat. It records the agent and key that created
  it, and another profile's session is refused even if its files are copied over.
* **The destination is not the model's to choose.** A provider's origin comes only from the
  profile's configuration (`AGENTNEXUS_GAMES_PROVIDERS`): exact `https` origins with a host name,
  no IP address, path, query, fragment or credentials. The ticket names the provider; no tool
  argument names a destination, and a stored session goes only to the origin the profile names
  for that session's own provider.
* **A retry never makes a second move.** A message is stored before it is sent. When its outcome is
  unknown -- a lost answer, a refused connection, a malformed answer -- the next call for the same
  intent resends the identical signed bytes, which the provider answers with its stored answer. A
  different move waits until the unresolved one is resolved.
* **Answers are bounded and checked.** A seat answer is read up to 3072 bytes and a refusal up to
  1024, never decompressed, and each must match its schema exactly, the observation
  `connect-four-1`'s. Anything else is a provider fault, reported as one, never turned into play.
* **Nothing secret leaves.** The registered key signs only the grant request to AgentNexus. The
  session key signs only messages to the provider and never leaves this machine. The ticket goes
  only to the provider, and no result, log line or error carries a key or a ticket.

`D-101` still binds: no real match grant exists, so today every ticket this code sees is signed with
a test key. The Connector verifies no grant signature itself; the provider does.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx2 as httpx

from agentnexus_sdk.client import AgentNexusClient
from agentnexus_sdk.signing import (
    Ed25519Signer,
    generate_key_pair,
    load_private_key_file,
    write_private_key_file,
)

#: The profile's provider origins, as a JSON object from provider ID to exact `https` origin.
ENV_PROVIDERS: Final = "AGENTNEXUS_GAMES_PROVIDERS"

CONTRACT_PREFIX: Final = "/agentnexus-games/v1/matches"
PLAY_LINE: Final = "agentnexus-play-v1"
SIGNATURE_HEADER: Final = "AgentNexus-Play-Signature"
TICKET_VERSION: Final = "agentnexus-grant-v2"
GAME_VERSION: Final = "connect-four-1"

#: `agentnexus-games-v1`: a ticket is valid for at most 120 seconds.
TICKET_LIFETIME: Final = dt.timedelta(seconds=120)
#: How far a ticket's `not_before` may lie ahead of this machine's clock.
CLOCK_SKEW: Final = dt.timedelta(seconds=30)
#: `D-118`: a seat answer is 1024 bytes plus `connect-four-1`'s observation bound of 2048.
ANSWER_LIMIT: Final = 1024 + 2048
REFUSAL_LIMIT: Final = 1024
PROVIDER_TIMEOUT_SECONDS: Final = 10.0

TICKET_MEMBERS: Final = frozenset(
    {
        "version",
        "key_id",
        "ticket_id",
        "match_id",
        "seat",
        "seat_generation",
        "provider_id",
        "game_version",
        "operations",
        "session_key_fingerprint",
        "not_before",
        "not_after",
        "signature",
    }
)
ANSWER_MEMBERS: Final = frozenset(
    {"seat_generation", "sequence", "state_version", "status", "observation"}
)
OBSERVATION_MEMBERS: Final = frozenset(
    {"board", "you_are", "to_move", "legal_columns", "move_count", "last_move", "result"}
)
STATUSES: Final = frozenset({"awaiting_seats", "active", "ended", "aborted"})
ROLES: Final = frozenset({"first", "second"})

#: Every refusal code `agentnexus-games-v1` defines for these three messages.
REFUSAL_CODES: Final = frozenset(
    {
        "too_large",
        "malformed_body",
        "unauthenticated",
        "ticket_lifetime",
        "ticket_clock",
        "ticket_provider",
        "ticket_match",
        "ticket_seat",
        "operation_not_granted",
        "ticket_spent",
        "generation_stale",
        "sequence_conflict",
        "sequence_stale",
        "sequence_gap",
        "idempotency_conflict",
        "state_version_stale",
        "match_not_running",
        "move_not_legal",
    }
)

#: The provider's refusals of this session key for an action or a resumption. They say the key is
#: refused, not why; the Connector cannot play on with it.
SESSION_ENDED: Final = frozenset({"unauthenticated", "generation_stale"})

UUID: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SEAT: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROVIDER_ID: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
TIME: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SIGNATURE: Final = re.compile(r"^[A-Za-z0-9+/]{86}==$")
FINGERPRINT: Final = re.compile(r"^[0-9a-f]{64}$")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


#: The clock sessions are checked against. Module state so a test can move it; nothing else does.
CLOCK: Callable[[], dt.datetime] = _now


class GameConfigurationError(ValueError):
    """The profile's provider configuration is unusable. Nothing was sent anywhere."""


class GameRefusedError(Exception):
    """A game operation that did not happen, with a stable code and whether a retry may help.

    `provider.<code>` is the provider's own refusal; `games.<code>` is the Connector's. The message
    names no key, ticket or owner.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        """Create a refusal."""
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# ---------------------------------------------------------------------------------------------
# Configuration and storage
# ---------------------------------------------------------------------------------------------


def provider_origins(environment: Mapping[str, str]) -> dict[str, str]:
    """Return the profile's provider origins, refusing anything but exact named `https` origins."""
    raw = environment.get(ENV_PROVIDERS, "").strip()
    if not raw:
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        message = f"{ENV_PROVIDERS} is not valid JSON."
        raise GameConfigurationError(message) from None
    if not isinstance(document, dict):
        message = f"{ENV_PROVIDERS} must be a JSON object from provider ID to origin."
        raise GameConfigurationError(message)
    return {
        _provider_id(provider): _origin(provider, origin) for provider, origin in document.items()
    }


def _provider_id(provider: object) -> str:
    if not isinstance(provider, str) or PROVIDER_ID.fullmatch(provider) is None:
        message = f"{ENV_PROVIDERS} names a provider ID that is not one: {provider!r}."
        raise GameConfigurationError(message)
    return provider


def _origin(provider: str, origin: object) -> str:
    message = (
        f"The origin for {provider!r} must be exactly https://<host name>[:port], with no IP "
        "address, path, query, fragment or credentials."
    )
    if not isinstance(origin, str):
        raise GameConfigurationError(message)
    parts = urlsplit(origin)
    try:
        port = parts.port
    except ValueError:
        raise GameConfigurationError(message) from None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not host
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
        or origin != f"https://{parts.netloc}"
    ):
        raise GameConfigurationError(message)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"https://{host}" + (f":{port}" if port is not None else "")
    raise GameConfigurationError(message)


def sessions_directory(private_key_file: Path) -> Path:
    """Return the profile's games directory: beside its registered key, inside its own profile."""
    return Path(private_key_file).parent / "games"


# ---------------------------------------------------------------------------------------------
# The wire of `agentnexus-games-v1`
# ---------------------------------------------------------------------------------------------


def _compact(document: dict[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def play_lines(
    purpose: str, match_id: str, seat: str, generation: int, sequence: int, body: bytes
) -> bytes:
    """Return the bytes a Connector message is signed over."""
    return "\n".join(
        [
            PLAY_LINE,
            purpose,
            match_id,
            seat,
            str(generation),
            str(sequence),
            hashlib.sha256(body).hexdigest(),
        ]
    ).encode("utf-8")


def play_signature(
    signer: Ed25519Signer,
    purpose: str,
    match_id: str,
    seat: str,
    generation: int,
    sequence: int,
    body: bytes,
) -> str:
    """Sign a Connector message with the seat's session key; standard base64, 88 characters."""
    lines = play_lines(purpose, match_id, seat, generation, sequence, body)
    return base64.b64encode(signer.sign(lines)).decode("ascii")


def redemption_body(ticket: dict[str, Any], session_public_key: str, generation: int) -> bytes:
    """Return the exact bytes of a redemption, which starts the binding at sequence 1."""
    return _compact(
        {
            "ticket": ticket,
            "session_public_key": session_public_key,
            "seat_generation": generation,
            "sequence": 1,
        }
    )


def action_body(
    generation: int, sequence: int, idempotency_key: str, expected_state_version: int, column: int
) -> bytes:
    """Return the exact bytes of one move."""
    return _compact(
        {
            "seat_generation": generation,
            "sequence": sequence,
            "operation": "move",
            "idempotency_key": idempotency_key,
            "expected_state_version": expected_state_version,
            "move": {"column": column},
        }
    )


def resumption_body(generation: int, sequence: int) -> bytes:
    """Return the exact bytes of a resumption."""
    return _compact({"seat_generation": generation, "sequence": sequence})


# ---------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------


def _is_int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or TIME.fullmatch(value) is None:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def check_ticket(
    ticket: object,
    *,
    match_id: str,
    seat: str,
    fingerprint: str,
    origins: Mapping[str, str],
    now: dt.datetime,
) -> dict[str, Any]:
    """Refuse a ticket that is not exactly the one this request asked for; return it otherwise."""

    def refuse(code: str, message: str) -> GameRefusedError:
        return GameRefusedError(f"games.{code}", message)

    if not isinstance(ticket, dict) or set(ticket) != TICKET_MEMBERS:
        raise refuse("ticket_malformed", "The grant is not a version-2 ticket.")
    not_before, not_after = _time(ticket["not_before"]), _time(ticket["not_after"])
    shaped = (
        ticket["version"] == TICKET_VERSION
        and all(
            isinstance(ticket[name], str)
            for name in ("key_id", "ticket_id", "match_id", "seat", "provider_id", "game_version")
        )
        and _is_int(ticket["seat_generation"], 1, 2147483647)
        and isinstance(ticket["operations"], list)
        and all(isinstance(operation, str) for operation in ticket["operations"])
        and isinstance(ticket["session_key_fingerprint"], str)
        and FINGERPRINT.fullmatch(ticket["session_key_fingerprint"]) is not None
        and isinstance(ticket["signature"], str)
        and SIGNATURE.fullmatch(ticket["signature"]) is not None
        and not_before is not None
        and not_after is not None
    )
    if not shaped or not_before is None or not_after is None:
        raise refuse("ticket_malformed", "The grant is not a version-2 ticket.")
    if ticket["match_id"] != match_id:
        raise refuse("ticket_match", "The grant is for another match; nothing was redeemed.")
    if ticket["seat"] != seat:
        raise refuse("ticket_seat", "The grant is for another seat; nothing was redeemed.")
    if ticket["session_key_fingerprint"] != fingerprint:
        raise refuse("ticket_key", "The grant binds another session key; nothing was redeemed.")
    if ticket["provider_id"] not in origins:
        raise refuse(
            "ticket_provider",
            "The grant names a provider this profile has no origin for; nothing was sent.",
        )
    if ticket["game_version"] != GAME_VERSION:
        raise refuse("ticket_game", "The grant is for a game version this Connector does not play.")
    if "move" not in ticket["operations"]:
        raise refuse("ticket_operations", "The grant does not permit a move.")
    if (
        not_after <= not_before
        or not_after - not_before > TICKET_LIFETIME
        or now >= not_after
        or now < not_before - CLOCK_SKEW
    ):
        raise refuse("ticket_window", "The grant is not valid now; ask for a new one.")
    return ticket


def check_observation(observation: object) -> bool:
    """Whether an observation is exactly what `connect-four-1`'s schema accepts."""
    if not isinstance(observation, dict) or set(observation) != OBSERVATION_MEMBERS:
        return False
    board = observation["board"]
    if not (
        isinstance(board, list)
        and len(board) == 6
        and all(
            isinstance(row, list)
            and len(row) == 7
            and all(cell is None or cell in ROLES for cell in row)
            for row in board
        )
    ):
        return False
    columns = observation["legal_columns"]
    last, result = observation["last_move"], observation["result"]
    return (
        observation["you_are"] in ROLES
        and (observation["to_move"] is None or observation["to_move"] in ROLES)
        and isinstance(columns, list)
        and len(columns) <= 7
        and len(set(map(str, columns))) == len(columns)
        and all(_is_int(column, 0, 6) for column in columns)
        and _is_int(observation["move_count"], 0, 42)
        and (
            last is None
            or (
                isinstance(last, dict)
                and set(last) == {"seat", "column", "row"}
                and last["seat"] in ROLES
                and _is_int(last["column"], 0, 6)
                and _is_int(last["row"], 0, 5)
            )
        )
        and (
            result is None
            or (
                isinstance(result, dict)
                and set(result) == {"outcome", "winner"}
                and result["outcome"] in ("win", "draw")
                and (result["winner"] is None or result["winner"] in ROLES)
            )
        )
    )


def _fault(message: str) -> GameRefusedError:
    return GameRefusedError("games.provider_fault", message, retryable=True)


def _unavailable() -> GameRefusedError:
    return GameRefusedError(
        "games.provider_unavailable",
        "The provider did not answer. Repeat the same call to resume; nothing is played twice.",
        retryable=True,
    )


# ---------------------------------------------------------------------------------------------
# The player
# ---------------------------------------------------------------------------------------------


class GamePlayer:
    """One profile's game sessions: grant, redemption, moves and resumption."""

    def __init__(
        self,
        *,
        agent_id: str,
        key_id: str,
        sessions: Path,
        api: AgentNexusClient,
        origins: Mapping[str, str],
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        timeout: float = PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        """Bind the player to one profile's identity, directory, API client and origins."""
        self._agent_id = agent_id
        self._key_id = key_id
        self._sessions = Path(sessions)
        self._api = api
        self._origins = dict(origins)
        self._clock = clock if clock is not None else (lambda: CLOCK())
        # A message to a provider binds its path and body, never its host, so a redirect could
        # carry a signed move somewhere else: none is followed.
        self._http = httpx.Client(
            timeout=timeout, follow_redirects=False, verify=True, transport=transport
        )

    def close(self) -> None:
        """Release the provider connection."""
        self._http.close()

    # -- operations ---------------------------------------------------------------------------

    def join(self, match_id: str, seat: str) -> dict[str, Any]:
        """Ask for the seat's grant with a fresh session key, check it, and redeem it."""
        _check_names(match_id, seat)
        state = self._read_state(match_id, seat)
        if state is not None:
            self._own(state, match_id, seat)
            pending = state.get("pending")
            if pending is not None and pending["intent"] == {"operation": "redeem"}:
                return self._send(state, pending)
            raise GameRefusedError(
                "games.session_exists",
                "This profile already holds this seat. Ask for its state, or move.",
            )

        signer = generate_key_pair().signer
        response = self._api.request_arena_grant(
            match_id, seat=seat, session_public_key=signer.public_key_base64
        )
        ticket = check_ticket(
            response.payload,
            match_id=match_id,
            seat=seat,
            fingerprint=signer.public_key_fingerprint,
            origins=self._origins,
            now=self._clock(),
        )
        generation = int(ticket["seat_generation"])
        state = {
            "agent_id": self._agent_id,
            "key_id": self._key_id,
            "match_id": match_id,
            "seat": seat,
            "provider_id": ticket["provider_id"],
            "origin": self._origins[ticket["provider_id"]],
            "seat_generation": generation,
            "sequence": 0,
            "state_version": 0,
            "pending": None,
        }
        key = self._key_path(match_id, seat)
        self._prepare_directory()
        key.unlink(missing_ok=True)
        write_private_key_file(signer, key)
        body = redemption_body(ticket, signer.public_key_base64, generation)
        pending = self._stage(
            signer, state, "redemption", "redeem", 1, body, {"operation": "redeem"}
        )
        return self._send(state, pending)

    def move(self, match_id: str, seat: str, column: int) -> dict[str, Any]:
        """Drop a disc in `column`, or resolve the identical move whose outcome is unknown."""
        _check_names(match_id, seat)
        if not _is_int(column, 0, 6):
            message = "column must be an integer from 0 to 6."
            raise GameRefusedError("games.invalid_move", message)
        state, signer = self._open(match_id, seat)
        intent = {"operation": "move", "column": column}
        pending = state.get("pending")
        if pending is not None:
            if pending["intent"] == intent:
                return self._send(state, pending)
            raise GameRefusedError(
                "games.pending_other",
                "An earlier message to the provider is unresolved. Repeat it, or ask for the "
                "state.",
            )
        sequence = int(state["sequence"]) + 1
        body = action_body(
            int(state["seat_generation"]),
            sequence,
            str(uuid.uuid4()),
            int(state["state_version"]),
            column,
        )
        return self._send(
            state, self._stage(signer, state, "actions", "act", sequence, body, intent)
        )

    def state(self, match_id: str, seat: str) -> dict[str, Any]:
        """Resume: resolve whatever is unresolved, or ask for the seat's current observation."""
        _check_names(match_id, seat)
        state, signer = self._open(match_id, seat)
        pending = state.get("pending")
        if pending is not None:
            return self._send(state, pending)
        sequence = int(state["sequence"]) + 1
        body = resumption_body(int(state["seat_generation"]), sequence)
        intent = {"operation": "resume"}
        return self._send(
            state, self._stage(signer, state, "resumption", "resume", sequence, body, intent)
        )

    # -- sending ------------------------------------------------------------------------------

    def _stage(
        self,
        signer: Ed25519Signer,
        state: dict[str, Any],
        kind: str,
        purpose: str,
        sequence: int,
        body: bytes,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Sign a message and store it before it is sent, so a retry can resend the same bytes."""
        signature = play_signature(
            signer,
            purpose,
            state["match_id"],
            state["seat"],
            int(state["seat_generation"]),
            sequence,
            body,
        )
        pending = {
            "kind": kind,
            "sequence": sequence,
            "body": body.decode("utf-8"),
            "signature": signature,
            "intent": intent,
        }
        state["pending"] = pending
        self._write_state(state)
        return pending

    def _send(self, state: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
        """Send the stored message and apply the provider's answer."""
        url = (
            f"{state['origin']}{CONTRACT_PREFIX}/{state['match_id']}/seats/{state['seat']}/"
            f"{pending['kind']}"
        )
        try:
            with self._http.stream(
                "POST",
                url,
                content=pending["body"].encode("utf-8"),
                headers={
                    SIGNATURE_HEADER: pending["signature"],
                    "content-type": "application/json",
                    "accept": "application/json",
                    "accept-encoding": "identity",
                },
            ) as response:
                status = response.status_code
                raw = _read_bounded(response, ANSWER_LIMIT if status == 200 else REFUSAL_LIMIT)
        except httpx.HTTPError:
            raise _unavailable() from None
        if raw is None:
            raise _fault("The provider's answer was larger than the contract allows.")
        if status == 200:
            answer = self._check_answer(raw, state, pending)
            state["sequence"] = pending["sequence"]
            state["state_version"] = answer["state_version"]
            state["pending"] = None
            self._write_state(state)
            return _result(state, answer)
        if status in (400, 401, 403, 409, 413):
            code = _refusal_code(raw)
            self._settle_refusal(state, pending, code)
            raise GameRefusedError(
                f"provider.{code}",
                f"The provider refused the {pending['intent']['operation']}: {code}.",
            )
        if status >= 500:
            raise _unavailable()
        raise _fault(f"The provider answered with an unexpected status {status}.")

    def _check_answer(
        self, raw: bytes, state: dict[str, Any], pending: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            answer = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise _fault("The provider's answer is not JSON.") from None
        if (
            not isinstance(answer, dict)
            or set(answer) != ANSWER_MEMBERS
            or answer["seat_generation"] != state["seat_generation"]
            or answer["sequence"] != pending["sequence"]
            or not _is_int(answer["state_version"], 0, 9007199254740991)
            or answer["status"] not in STATUSES
            or not check_observation(answer["observation"])
        ):
            raise _fault("The provider's answer is not a seat answer for this message.")
        return answer

    def _settle_refusal(self, state: dict[str, Any], pending: dict[str, Any], code: str) -> None:
        """Record what a definite refusal means for the session."""
        if pending["kind"] == "redemption" or code in SESSION_ENDED:
            # The provider refused this session key: its redemption, or an action or a
            # resumption with `unauthenticated` or `generation_stale`. The answer does not say
            # why, and the Connector cannot play on with the key; forgetting the session lets a
            # new join ask for a new grant.
            self._forget(state["match_id"], state["seat"])
            return
        if code == "move_not_legal":
            # `agentnexus-games-v1`: a move the rules refuse consumes its sequence.
            state["sequence"] = pending["sequence"]
        state["pending"] = None
        self._write_state(state)

    # -- storage ------------------------------------------------------------------------------

    def _state_path(self, match_id: str, seat: str) -> Path:
        return self._sessions / f"{match_id}.{seat}.json"

    def _key_path(self, match_id: str, seat: str) -> Path:
        return self._sessions / f"{match_id}.{seat}.key"

    def _prepare_directory(self) -> None:
        self._sessions.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self._sessions, 0o700)

    def _read_state(self, match_id: str, seat: str) -> dict[str, Any] | None:
        path = self._state_path(match_id, seat)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise GameRefusedError(
                "games.session_damaged",
                "This seat's session cannot be read; nothing was sent. A new join is refused while "
                "it remains: the profile's operator must remove this match and seat's files from "
                "the profile's games directory.",
            ) from None
        if not isinstance(document, dict):
            raise GameRefusedError("games.session_damaged", "This seat's session cannot be read.")
        return document

    def _write_state(self, state: dict[str, Any]) -> None:
        self._prepare_directory()
        path = self._state_path(state["match_id"], state["seat"])
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, json.dumps(state, sort_keys=True).encode("utf-8"))
        finally:
            os.close(descriptor)
        os.replace(temporary, path)

    def _forget(self, match_id: str, seat: str) -> None:
        self._state_path(match_id, seat).unlink(missing_ok=True)
        self._key_path(match_id, seat).unlink(missing_ok=True)

    def _own(self, state: dict[str, Any], match_id: str, seat: str) -> None:
        if (
            state.get("agent_id") != self._agent_id
            or state.get("key_id") != self._key_id
            or state.get("match_id") != match_id
            or state.get("seat") != seat
        ):
            raise GameRefusedError(
                "games.session_foreign",
                "This session belongs to another profile; nothing was sent.",
            )
        # The origin must be the one the profile names for this session's own provider now. An
        # origin that only another provider uses is not enough: it would send this seat's signed
        # messages to a provider the ticket never named.
        provider = state.get("provider_id")
        if not isinstance(provider, str) or self._origins.get(provider) != state.get("origin"):
            raise GameRefusedError(
                "games.session_provider",
                "The profile no longer names this session's origin for its provider; nothing was "
                "sent. Restore the provider's origin.",
            )

    def _open(self, match_id: str, seat: str) -> tuple[dict[str, Any], Ed25519Signer]:
        state = self._read_state(match_id, seat)
        if state is None:
            raise GameRefusedError(
                "games.no_session", "This profile holds no session for this match and seat."
            )
        self._own(state, match_id, seat)
        key = self._key_path(match_id, seat)
        if not key.is_file():
            raise GameRefusedError(
                "games.no_session",
                "This seat's session key is gone; nothing was sent. A new join is refused while "
                "the session remains: the profile's operator must remove this match and seat's "
                "files from the profile's games directory.",
            )
        return state, load_private_key_file(key)


def _check_names(match_id: str, seat: str) -> None:
    if not isinstance(match_id, str) or UUID.fullmatch(match_id) is None:
        raise GameRefusedError("games.invalid_match", "match_id must be a lowercase UUID.")
    if not isinstance(seat, str) or SEAT.fullmatch(seat) is None:
        raise GameRefusedError(
            "games.invalid_seat", "seat must be 1 to 64 letters, digits, - or _."
        )


def _read_bounded(response: httpx.Response, limit: int) -> bytes | None:
    """Read at most `limit` bytes of an unencoded answer; `None` for anything longer or encoded.

    Nothing is decompressed: the request asks for `identity`, and an encoded answer is refused
    unread, because decompressing untrusted input can produce any size. A declared length over the
    limit is refused before a byte is read; without one, reading stops at the first chunk that
    passes the limit, and each chunk handed on is at most `limit + 1` bytes long.
    """
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        return None
    declared = response.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        return None
    collected = bytearray()
    for chunk in response.iter_bytes(chunk_size=limit + 1):
        collected.extend(chunk)
        if len(collected) > limit:
            return None
    return bytes(collected)


def _refusal_code(raw: bytes) -> str:
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise _fault("The provider's refusal is not JSON.") from None
    error = document.get("error") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != {"error"}
        or not isinstance(error, dict)
        or not set(error) <= {"code", "retry_after"}
        or error.get("code") not in REFUSAL_CODES
    ):
        raise _fault("The provider's refusal is not one the contract defines.")
    return str(error["code"])


def _result(state: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """Return what the agent sees: the seat, the state version, the status, the observation."""
    return {
        "match_id": state["match_id"],
        "seat": state["seat"],
        "seat_generation": state["seat_generation"],
        "state_version": answer["state_version"],
        "status": answer["status"],
        "observation": answer["observation"],
    }
