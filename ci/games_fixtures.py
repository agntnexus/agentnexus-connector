"""Deterministic stand-ins for the AgentNexus grant route and a conforming Connect Four provider.

agntnexus/agentnexus#83. Both run in memory behind `httpx.MockTransport`: nothing here opens a
socket, holds a real key or issues a real match grant (`D-101`).

* **The grant route** answers `POST /agent-api/v1/arena/matches/{match_id}/grant` the way `D-136`
  AR-3 fixes it and the #82 API work answers it: `201` with a version-2 ticket that the `ticket-v2`
  schema of `agentnexus-games-v1` accepts, signed with the contract's **published test grant key**,
  which is valid nowhere. It verifies the registered agent's request signature, so a request signed
  with another profile's key is refused, and it can be told to answer wrongly -- another match,
  seat, session key, provider, game version, an expired window -- which is what the Connector
  must refuse.
* **The provider** implements the provider wire of `agentnexus-games-v1` (`protocol/v1/` of the
  public `agntnexus/agentnexus-games-contracts`) for Connect Four `connect-four-1`: size, schema,
  session-key signature, ticket signature and freshness, the ticket's bindings, then state --
  sequence, idempotency, state version and the rules. Its computer opponent always takes the
  lowest legal column, so every game is reproducible. It can inject one fault at a time: a
  connection refused before or lost after applying, an oversized or malformed answer, or a 503.

Every request either stand-in receives is recorded with its host, so a test can show which hops a
game made.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agentnexus_sdk.envelope import EnvelopeInput, build_envelope, parse_timestamp

PROVIDER_ID = "example-provider"
GAME_VERSION = "connect-four-1"
PROVIDER_HOST = "connect-four.test.invalid"
ORIGIN = f"https://{PROVIDER_HOST}"
API_HOST = "api.agentnexus.test.invalid"
API_BASE = f"https://{API_HOST}"

#: The contract's published test grant key (`vectors/provider-wire-v1/cases.json`,
#: `grant_verification_keys[0]`): the SHA-256 of this phrase is its seed. Valid nowhere.
GRANT_KEY_PHRASE = "agentnexus-grant-v2 published test grant key 1, valid nowhere"
GRANT_KEY_ID = "3b1e7c5a-9d2f-4a86-b0c4-e8f1a2d3c4b5"
GRANT_KEY = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(GRANT_KEY_PHRASE.encode()).digest())
GRANT_PUBLIC_KEY = base64.b64encode(
    GRANT_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode("ascii")

TICKET_LIFETIME = dt.timedelta(seconds=120)
TICKET_SKEW = dt.timedelta(seconds=30)
LIMITS = {"redemption": 4096, "resumption": 1024, "actions": 1024 + 4096}
SEAT_ROLES = {"seat-a": "first", "seat-b": "second"}
ROWS, COLUMNS = 6, 7

GRANT_PATH = re.compile(r"^/agent-api/v1/arena/matches/([0-9a-f-]{36})/grant$")
WIRE_PATH = re.compile(
    r"^/agentnexus-games/v1/matches/([0-9a-f-]{36})/seats/([A-Za-z0-9_-]{1,64})/"
    r"(redemption|resumption|actions)$"
)
PURPOSES = {"redemption": "redeem", "resumption": "resume", "actions": "act"}


def stamp(moment: dt.datetime) -> str:
    """Format an instant as the contract's second-precision UTC time."""
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket_lines(ticket: dict[str, Any]) -> bytes:
    """Return the bytes a version-2 ticket is signed over."""
    return "\n".join(
        [
            "agentnexus-grant-v2",
            ticket["key_id"],
            ticket["ticket_id"],
            ticket["match_id"],
            ticket["seat"],
            str(ticket["seat_generation"]),
            ticket["provider_id"],
            ticket["game_version"],
            ",".join(ticket["operations"]),
            ticket["session_key_fingerprint"],
            ticket["not_before"],
            ticket["not_after"],
        ]
    ).encode("utf-8")


def fingerprint(public_key_base64: str) -> str:
    """Return the lowercase hex SHA-256 of a raw public key given in standard base64."""
    return hashlib.sha256(base64.b64decode(public_key_base64)).hexdigest()


@dataclass
class Clock:
    """A clock the stand-ins and the Connector share, moved only by the test."""

    now: dt.datetime = dt.datetime(2026, 9, 28, 10, 0, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        """Return the current instant."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += dt.timedelta(seconds=seconds)


@dataclass
class Recorded:
    """One request a stand-in received."""

    host: str
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


def _problem(status: int, code: str) -> httpx.Response:
    body = {
        "type": f"https://agntnexus.com/problems/{code}",
        "title": code,
        "status": status,
        "code": code,
        "detail": "Refused by the fixture.",
    }
    return httpx.Response(status, json=body, headers={"content-type": "application/problem+json"})


class ArenaApi:
    """The signed grant route of `D-136` AR-3, answering as the #82 API work does."""

    def __init__(self, clock: Clock, registered: dict[str, str]) -> None:
        """Serve grants; each agent ID in `registered` maps to the key its requests must verify."""
        self.clock = clock
        self.registered = registered
        self.requests: list[Recorded] = []
        self.mode = "honest"
        self.generations: dict[tuple[str, str], int] = {}

    def _verifies(self, request: httpx.Request, body: bytes) -> bool:
        headers = {name.lower(): value for name, value in request.headers.items()}
        public = self.registered.get(headers.get("x-agent-id", ""))
        if public is None:
            return False
        try:
            envelope = build_envelope(
                EnvelopeInput(
                    method=request.method,
                    path=request.url.path,
                    query_string="",
                    agent_id=headers["x-agent-id"],
                    key_id=headers["x-agent-key-id"],
                    timestamp=parse_timestamp(headers["x-agent-timestamp"]),
                    nonce=headers["x-agent-nonce"],
                    idempotency_key=headers["idempotency-key"],
                    body=body,
                )
            )
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public)).verify(
                base64.b64decode(headers["x-agent-signature"]), envelope.signing_bytes()
            )
        except (KeyError, ValueError, InvalidSignature):
            return False
        return True

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request."""
        body = request.read()
        self.requests.append(
            Recorded(
                request.url.host, request.method, request.url.path, dict(request.headers), body
            )
        )
        found = GRANT_PATH.match(request.url.path)
        if request.method != "POST" or found is None:
            return _problem(404, "not_found")
        if not self._verifies(request, body):
            return _problem(401, "auth.signature_invalid")
        if self.mode == "refuse":
            return _problem(403, "arena.seat_refused")
        document = json.loads(body)
        if set(document) != {"seat", "session_public_key"}:
            return _problem(422, "request.validation_failed")
        match_id, seat = found.group(1), document["seat"]
        generation = self.generations.get((match_id, seat), 0) + 1
        self.generations[(match_id, seat)] = generation
        now = self.clock().replace(microsecond=0)
        not_before, not_after = now, now + TICKET_LIFETIME
        other = Ed25519PrivateKey.from_private_bytes(bytes(32)).public_key()
        other_key = base64.b64encode(other.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        ticket: dict[str, Any] = {
            "version": "agentnexus-grant-v2",
            "key_id": GRANT_KEY_ID,
            "ticket_id": str(uuid.uuid4()),
            "match_id": str(uuid.uuid4()) if self.mode == "another_match" else match_id,
            "seat": "seat-b" if self.mode == "another_seat" else seat,
            "seat_generation": generation,
            "provider_id": "other-provider" if self.mode == "another_provider" else PROVIDER_ID,
            "game_version": "chess-1" if self.mode == "another_game" else GAME_VERSION,
            "operations": ["resign"] if self.mode == "no_move" else ["move", "resign"],
            "session_key_fingerprint": fingerprint(
                other_key if self.mode == "another_key" else document["session_public_key"]
            ),
            "not_before": stamp(not_before - dt.timedelta(minutes=10))
            if self.mode == "expired"
            else stamp(not_before),
            "not_after": stamp(not_after - dt.timedelta(minutes=10))
            if self.mode == "expired"
            else stamp(not_after),
        }
        ticket["signature"] = base64.b64encode(GRANT_KEY.sign(ticket_lines(ticket))).decode()
        if self.mode == "extra_member":
            ticket["origin"] = "https://elsewhere.test.invalid"
        return httpx.Response(201, json=ticket)


@dataclass
class Binding:
    """One seat's binding at the provider."""

    public_key: str
    generation: int
    ticket_id: str
    sequence: int = 1
    last: tuple[bytes, str] | None = None
    last_answer: tuple[int, bytes] = (200, b"")
    moves: dict[str, tuple[int, tuple[int, bytes]]] = field(default_factory=dict)


@dataclass
class Game:
    """A Connect Four board; row 0 is the bottom."""

    board: list[list[str | None]] = field(
        default_factory=lambda: [[None] * COLUMNS for _ in range(ROWS)]
    )
    to_move: str | None = "first"
    move_count: int = 0
    last_move: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    state_version: int = 0

    def legal(self) -> list[int]:
        """Return the columns with room left, if the game still runs."""
        if self.result is not None:
            return []
        return [column for column in range(COLUMNS) if self.board[ROWS - 1][column] is None]

    def play(self, role: str, column: int) -> None:
        """Drop a disc; the caller checked legality."""
        row = next(r for r in range(ROWS) if self.board[r][column] is None)
        self.board[row][column] = role
        self.move_count += 1
        self.last_move = {"seat": role, "column": column, "row": row}
        self.state_version += 1
        self.to_move = "second" if role == "first" else "first"
        if not self.legal():
            self.result = {"outcome": "draw", "winner": None}
            self.to_move = None

    def observation(self, role: str) -> dict[str, Any]:
        """Return the seat's observation, as `connect-four-1`'s schema shapes it."""
        return {
            "board": [list(row) for row in self.board],
            "you_are": role,
            "to_move": self.to_move,
            "legal_columns": self.legal(),
            "move_count": self.move_count,
            "last_move": self.last_move,
            "result": self.result,
        }


class Provider:
    """A conforming `agentnexus-games-v1` provider for `connect-four-1`, in memory."""

    def __init__(self, clock: Clock) -> None:
        """Start with no binding and no game."""
        self.clock = clock
        self.requests: list[Recorded] = []
        self.bindings: dict[tuple[str, str], Binding] = {}
        self.spent: set[str] = set()
        self.games: dict[str, Game] = {}
        self.fault: str | None = None
        self.applied_moves = 0

    # -- answers ------------------------------------------------------------------------------

    @staticmethod
    def _refusal(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, content=json.dumps({"error": {"code": code}}).encode())

    def _answer(self, match_id: str, seat: str, binding: Binding) -> tuple[int, bytes]:
        game = self.games[match_id]
        document = {
            "seat_generation": binding.generation,
            "sequence": binding.sequence,
            "state_version": game.state_version,
            "status": "ended" if game.result is not None else "active",
            "observation": game.observation(SEAT_ROLES[seat]),
        }
        return 200, json.dumps(document, separators=(",", ":")).encode()

    # -- the wire -----------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request, injecting the configured fault once."""
        body = request.read()
        self.requests.append(
            Recorded(
                request.url.host, request.method, request.url.path, dict(request.headers), body
            )
        )
        fault, self.fault = self.fault, None
        if fault == "refuse_connection":
            raise httpx.ConnectError("connection refused", request=request)
        if fault == "unavailable":
            return httpx.Response(503, content=b"")
        status, content = self._serve(request, body)
        if fault == "lose_answer":
            raise httpx.ReadTimeout("the answer was lost", request=request)
        if fault == "oversize":
            return httpx.Response(200, content=content[:-1] + b" " * 4000 + content[-1:])
        if fault == "extra_member":
            document = json.loads(content)
            document["observation"]["hint"] = "play column 0"
            return httpx.Response(200, content=json.dumps(document).encode())
        if fault == "refusal_with_text":
            return httpx.Response(409, content=b'{"error":{"code":"move_not_legal"},"note":"x"}')
        return httpx.Response(status, content=content)

    def _serve(self, request: httpx.Request, body: bytes) -> tuple[int, bytes]:
        found = WIRE_PATH.match(request.url.path)
        if request.url.host != PROVIDER_HOST or request.method != "POST" or found is None:
            return 404, b""
        match_id, seat, kind = found.groups()
        if len(body) > LIMITS[kind]:
            return self._code(413, "too_large")
        try:
            document = json.loads(body)
        except ValueError:
            return self._code(400, "malformed_body")
        if not isinstance(document, dict):
            return self._code(400, "malformed_body")
        signature = request.headers.get("agentnexus-play-signature", "")
        if kind == "redemption":
            return self._redeem(match_id, seat, document, body, signature)
        binding = self.bindings.get((match_id, seat))
        if binding is None or not self._signed(
            binding.public_key, kind, match_id, seat, document, body, signature
        ):
            return self._code(401, "unauthenticated")
        return self._continue(match_id, seat, kind, document, body, signature, binding)

    @staticmethod
    def _code(status: int, code: str) -> tuple[int, bytes]:
        return status, json.dumps({"error": {"code": code}}).encode()

    @staticmethod
    def _signed(
        public: str,
        kind: str,
        match_id: str,
        seat: str,
        document: dict[str, Any],
        body: bytes,
        signature: str,
    ) -> bool:
        try:
            lines = "\n".join(
                [
                    "agentnexus-play-v1",
                    PURPOSES[kind],
                    match_id,
                    seat,
                    str(document["seat_generation"]),
                    str(document["sequence"]),
                    hashlib.sha256(body).hexdigest(),
                ]
            ).encode()
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public)).verify(
                base64.b64decode(signature), lines
            )
        except (KeyError, ValueError, InvalidSignature):
            return False
        return True

    def _redeem(
        self, match_id: str, seat: str, document: dict[str, Any], body: bytes, signature: str
    ) -> tuple[int, bytes]:
        if set(document) != {"ticket", "session_public_key", "seat_generation", "sequence"}:
            return self._code(400, "malformed_body")
        ticket, public = document["ticket"], document["session_public_key"]
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(GRANT_PUBLIC_KEY)).verify(
                base64.b64decode(ticket["signature"]), ticket_lines(ticket)
            )
            key_ok = (
                ticket["key_id"] == GRANT_KEY_ID
                and fingerprint(public) == ticket["session_key_fingerprint"]
            )
        except (KeyError, ValueError, TypeError, InvalidSignature):
            return self._code(401, "unauthenticated")
        if not key_ok or not self._signed(
            public, "redemption", match_id, seat, document, body, signature
        ):
            return self._code(401, "unauthenticated")
        not_before, not_after = (
            parse_timestamp(ticket["not_before"]),
            parse_timestamp(ticket["not_after"]),
        )
        if not_after - not_before > TICKET_LIFETIME:
            return self._code(401, "ticket_lifetime")
        now = self.clock()
        if now < not_before - TICKET_SKEW or now > not_after + TICKET_SKEW:
            return self._code(401, "ticket_clock")
        if ticket["provider_id"] != PROVIDER_ID:
            return self._code(403, "ticket_provider")
        if ticket["match_id"] != match_id:
            return self._code(403, "ticket_match")
        if ticket["seat"] != seat:
            return self._code(403, "ticket_seat")
        held = self.bindings.get((match_id, seat))
        if ticket["ticket_id"] in self.spent:
            if held is not None and held.last == (body, signature) and held.sequence == 1:
                return held.last_answer
            return self._code(409, "ticket_spent")
        if held is not None and ticket["seat_generation"] <= held.generation:
            return self._code(409, "generation_stale")
        self.spent.add(ticket["ticket_id"])
        self.games.setdefault(match_id, Game())
        binding = Binding(public, ticket["seat_generation"], ticket["ticket_id"])
        self.bindings[(match_id, seat)] = binding
        binding.last = (body, signature)
        binding.last_answer = self._answer(match_id, seat, binding)
        return binding.last_answer

    def _continue(
        self,
        match_id: str,
        seat: str,
        kind: str,
        document: dict[str, Any],
        body: bytes,
        signature: str,
        binding: Binding,
    ) -> tuple[int, bytes]:
        if binding.last == (body, signature):
            return binding.last_answer
        sequence = document.get("sequence")
        if not isinstance(sequence, int) or document.get("seat_generation") != binding.generation:
            return self._code(400, "malformed_body")
        if sequence == binding.sequence:
            return self._code(409, "sequence_conflict")
        if sequence < binding.sequence:
            return self._code(409, "sequence_stale")
        if sequence > binding.sequence + 1:
            return self._code(409, "sequence_gap")
        game = self.games[match_id]
        if kind == "resumption":
            if set(document) != {"seat_generation", "sequence"}:
                return self._code(400, "malformed_body")
            return self._accept(match_id, seat, binding, body, signature)
        expected = {
            "seat_generation",
            "sequence",
            "operation",
            "idempotency_key",
            "expected_state_version",
            "move",
        }
        if set(document) != expected or document["operation"] != "move":
            return self._code(400, "malformed_body")
        move = document["move"]
        if (
            not isinstance(move, dict)
            or set(move) != {"column"}
            or not isinstance(move["column"], int)
            or not 0 <= move["column"] <= 6
        ):
            return self._code(400, "malformed_body")
        key = document["idempotency_key"]
        if key in binding.moves:
            column, answer = binding.moves[key]
            if column != move["column"]:
                return self._code(409, "idempotency_conflict")
            binding.sequence = sequence
            binding.last, binding.last_answer = (body, signature), answer
            return answer
        if document["expected_state_version"] != game.state_version:
            return self._code(409, "state_version_stale")
        if game.result is not None:
            return self._code(409, "match_not_running")
        role = SEAT_ROLES[seat]
        if game.to_move != role or move["column"] not in game.legal():
            binding.sequence = sequence
            answer = self._code(409, "move_not_legal")
            binding.last, binding.last_answer = (body, signature), answer
            return answer
        game.play(role, move["column"])
        self.applied_moves += 1
        opponent = "second" if role == "first" else "first"
        if game.result is None and game.to_move == opponent:
            game.play(opponent, game.legal()[0])
        answer = self._accept(match_id, seat, binding, body, signature, sequence)
        binding.moves[key] = (move["column"], answer)
        return answer

    def _accept(
        self,
        match_id: str,
        seat: str,
        binding: Binding,
        body: bytes,
        signature: str,
        sequence: int | None = None,
    ) -> tuple[int, bytes]:
        binding.sequence = sequence if sequence is not None else binding.sequence + 1
        binding.last = (body, signature)
        binding.last_answer = self._answer(match_id, seat, binding)
        return binding.last_answer

    def our_discs(self, match_id: str, seat: str) -> int:
        """Count the seat's discs on the board."""
        role = SEAT_ROLES[seat]
        return sum(cell == role for row in self.games[match_id].board for cell in row)
