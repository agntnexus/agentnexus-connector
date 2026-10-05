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
* **Chess** (agntnexus/agentnexus#202, `D-170`): for a ticket naming `chess-1` or
  `chess-1-solo`, the same provider serves a stand-in that shapes every observation exactly as
  `chess-1`'s schema does and accepts a move in UCI, a draw claim, or both. It is no rules
  engine: its legal moves are pawn steps and knight jumps, a claim ends the game drawn, and its
  computer plays the first of its legal moves. The rules are the Chess provider's, not the
  Connector's.

Every request either stand-in receives is recorded with its host, so a test can show which hops a
game made.
"""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import json
import math
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
#: `D-142`: the solo game version, played by `connect-four-1`'s rules and payloads.
SOLO_GAME_VERSION = "connect-four-1-solo"
#: `D-170`: Chess's two game versions.
CHESS_GAME_VERSION = "chess-1"
CHESS_SOLO_GAME_VERSION = "chess-1-solo"
CHESS_GAME_VERSIONS = frozenset({CHESS_GAME_VERSION, CHESS_SOLO_GAME_VERSION})
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
#: The vectors name seats `seat-a` and `seat-b`; the #82 API issues `first` and `second`.
SEAT_ROLES = {"seat-a": "first", "seat-b": "second", "first": "first", "second": "second"}
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


class CountingStream(httpx.SyncByteStream):
    """An answer body that counts how many of its bytes the reader pulled."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Serve `chunks` in order."""
        self.chunks = chunks
        self.pulled = 0

    def __iter__(self) -> Any:
        """Yield the chunks, counting each one as it is taken."""
        for chunk in self.chunks:
            self.pulled += len(chunk)
            yield chunk


@dataclass
class Recorded:
    """One request a stand-in received."""

    host: str
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


#: The #82 API's own wording, where a case depends on it (`arena_grants.py`).
GRANT_LIVE_DETAIL = "This seat holds a live ticket; ask again when its window has passed."


def _problem(
    status: int,
    code: str,
    *,
    retry_after: int | str | None = None,
    detail: str = "Refused by the fixture.",
) -> httpx.Response:
    body = {
        "type": f"https://agntnexus.com/problems/{code}",
        "title": code,
        "status": status,
        "code": code,
        "detail": detail,
    }
    headers = {"content-type": "application/problem+json"}
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    return httpx.Response(status, json=body, headers=headers)


class ArenaApi:
    """The signed grant route of `D-136` AR-3, answering as the #82 API work does."""

    def __init__(self, clock: Clock, registered: dict[str, str]) -> None:
        """Serve grants; each agent ID in `registered` maps to the key its requests must verify."""
        self.clock = clock
        self.registered = registered
        self.requests: list[Recorded] = []
        self.mode = "honest"
        self.generations: dict[tuple[str, str], int] = {}
        #: The latest ticket issued per match and seat (`D-112`: never replaced early).
        self.issued: dict[tuple[str, str], dict[str, Any]] = {}
        #: Agents whose seat their owner approved; every registered agent until withdrawn.
        self.enrolled: set[str] = set(registered)
        #: Agents whose registered key was revoked.
        self.revoked: set[str] = set()
        #: A `Retry-After` value to send with `arena.grant_live` instead of the real wait, as the
        #: raw header text; the empty string sends none. For cases about what the bridge passes on.
        self.grant_live_retry_after: str | None = None

    def withdraw_enrollment(self, agent_id: str) -> None:
        """Answer as the API does for an agent that holds no owner-approved seat."""
        self.enrolled.discard(agent_id)

    def revoke_key(self, agent_id: str) -> None:
        """Answer as the API does for a signature by a key that is no longer active."""
        self.revoked.add(agent_id)

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
        agent_id = request.headers["x-agent-id"]
        if agent_id in self.revoked:
            return _problem(403, "auth.key_not_active")
        if agent_id not in self.enrolled:
            return _problem(403, "arena.seat_refused")
        if self.mode == "refuse":
            return _problem(403, "arena.seat_refused")
        document = json.loads(body)
        if set(document) != {"seat", "session_public_key"}:
            return _problem(422, "request.validation_failed")
        match_id, seat = found.group(1), document["seat"]
        # `D-112`, as the #82 API answers it: a live ticket is returned to the same session key and
        # never replaced for another one until its window and the skew have passed.
        latest = self.issued.get((match_id, seat))
        if latest is not None:
            ends = parse_timestamp(latest["not_after"]) + TICKET_SKEW
            if self.clock() < ends:
                if latest["session_key_fingerprint"] == fingerprint(document["session_public_key"]):
                    return httpx.Response(201, json=latest)
                wait = math.ceil((ends - self.clock()).total_seconds())
                header: int | str | None = min(max(wait, 1), 150)
                if self.grant_live_retry_after is not None:
                    header = self.grant_live_retry_after or None
                return _problem(
                    409, "arena.grant_live", retry_after=header, detail=GRANT_LIVE_DETAIL
                )
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
            "game_version": {
                "another_game": "chess-2",
                "another_solo_game": "connect-four-2-solo",
                "solo": SOLO_GAME_VERSION,
                "chess": CHESS_GAME_VERSION,
                "chess_solo": CHESS_SOLO_GAME_VERSION,
            }.get(self.mode, GAME_VERSION),
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
        self.issued[(match_id, seat)] = ticket
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


FILES = "abcdefgh"
COLOURS = {"first": "white", "second": "black"}
START = [
    ["R", "N", "B", "Q", "K", "B", "N", "R"],
    ["P"] * 8,
    [None] * 8,
    [None] * 8,
    [None] * 8,
    [None] * 8,
    ["p"] * 8,
    ["r", "n", "b", "q", "k", "b", "n", "r"],
]


def _square(file: int, rank: int) -> str:
    return FILES[file] + str(rank + 1)


@dataclass
class ChessGame:
    """A stand-in that shapes `chess-1`'s observation; not a rules engine (D-170)."""

    board: list[list[str | None]] = field(default_factory=lambda: [list(row) for row in START])
    moves: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    state_version: int = 0

    @property
    def to_move(self) -> str | None:
        """Return the colour to move, or `None` once the game ended."""
        if self.result is not None:
            return None
        return "white" if len(self.moves) % 2 == 0 else "black"

    def legal(self) -> list[str]:
        """Pawn steps and knight jumps of the side to move, onto empty squares."""
        colour = self.to_move
        if colour is None:
            return []
        found = []
        for rank in range(8):
            for file in range(8):
                piece = self.board[rank][file]
                if piece is None or piece.isupper() != (colour == "white"):
                    continue
                if piece.lower() == "p":
                    step = 1 if colour == "white" else -1
                    targets = [(file, rank + step)]
                else:
                    jumps = ((1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1))
                    targets = (
                        [(file + df, rank + dr) for df, dr in jumps] if piece.lower() == "n" else []
                    )
                for to_file, to_rank in targets:
                    if (
                        0 <= to_file < 8
                        and 0 <= to_rank < 8
                        and self.board[to_rank][to_file] is None
                    ):
                        found.append(_square(file, rank) + _square(to_file, to_rank))
        return sorted(found)

    def play(self, uci: str) -> None:
        """Move a piece; the caller checked the move is one of `legal()`."""
        file, rank = FILES.index(uci[0]), int(uci[1]) - 1
        to_file, to_rank = FILES.index(uci[2]), int(uci[3]) - 1
        self.board[to_rank][to_file] = self.board[rank][file]
        self.board[rank][file] = None
        self.moves.append(uci)
        self.state_version += 1

    def claim(self) -> None:
        """End the stand-in's game drawn; the real provider checks a claim under the rules."""
        self.result = {"outcome": "draw", "winner": None, "reason": "threefold_repetition"}
        self.state_version += 1

    def observation(self, role: str) -> dict[str, Any]:
        """Return the seat's observation, as `chess-1`'s schema shapes it."""
        mine = self.to_move == COLOURS[role]
        return {
            "board": [list(row) for row in self.board],
            "you_are": COLOURS[role],
            "to_move": self.to_move,
            "in_check": False,
            "castling": {
                "white_kingside": True,
                "white_queenside": True,
                "black_kingside": True,
                "black_queenside": True,
            },
            "en_passant": None,
            "halfmove_clock": 0,
            "fullmove_number": 1 + len(self.moves) // 2,
            "moves": list(self.moves),
            "last_move": self.moves[-1] if self.moves else None,
            "result": self.result,
            "legal_moves": self.legal() if mine else [],
            "claimable_draws": [],
        }


class Provider:
    """A conforming `agentnexus-games-v1` provider for `connect-four-1`, in memory."""

    def __init__(self, clock: Clock) -> None:
        """Start with no binding and no game."""
        self.clock = clock
        self.requests: list[Recorded] = []
        self.bindings: dict[tuple[str, str], Binding] = {}
        self.spent: set[str] = set()
        self.games: dict[str, Any] = {}
        self.fault: str | None = None
        self.applied_moves = 0
        self.stream: CountingStream | None = None

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
        if fault == "gzip":
            # A valid answer, compressed: a reader that decodes it has decoded untrusted input.
            return httpx.Response(
                status, content=gzip.compress(content), headers={"content-encoding": "gzip"}
            )
        if fault == "gzip_bomb":
            bomb = gzip.compress(b" " * 8_000_000)
            return httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"})
        if fault == "declared_huge":
            self.stream = CountingStream([b" " * 1_000_000])
            return httpx.Response(200, stream=self.stream, headers={"content-length": "1000000"})
        if fault == "undeclared_huge":
            self.stream = CountingStream([b" " * 4096] * 256)
            return httpx.Response(200, stream=self.stream)
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
        chess = ticket["game_version"] in CHESS_GAME_VERSIONS
        self.games.setdefault(match_id, ChessGame() if chess else Game())
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
        if isinstance(game, ChessGame):
            return self._continue_chess(
                game, match_id, seat, document, body, signature, binding, sequence
            )
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

    def _continue_chess(
        self,
        game: ChessGame,
        match_id: str,
        seat: str,
        document: dict[str, Any],
        body: bytes,
        signature: str,
        binding: Binding,
        sequence: int,
    ) -> tuple[int, bytes]:
        move = document["move"]
        uci = move.get("uci") if isinstance(move, dict) else None
        claim = move.get("claim") if isinstance(move, dict) else None
        if (
            not isinstance(move, dict)
            or not move
            or not set(move) <= {"uci", "claim"}
            or (uci is not None and not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", str(uci)))
            or (claim is not None and claim not in ("threefold_repetition", "fifty_moves"))
        ):
            return self._code(400, "malformed_body")
        key = document["idempotency_key"]
        if key in binding.moves:
            stored, answer = binding.moves[key]
            if stored != json.dumps(move, sort_keys=True):
                return self._code(409, "idempotency_conflict")
            binding.sequence = sequence
            binding.last, binding.last_answer = (body, signature), answer
            return answer
        if document["expected_state_version"] != game.state_version:
            return self._code(409, "state_version_stale")
        if game.result is not None:
            return self._code(409, "match_not_running")
        role = SEAT_ROLES[seat]
        if game.to_move != COLOURS[role] or (uci is not None and uci not in game.legal()):
            binding.sequence = sequence
            answer = self._code(409, "move_not_legal")
            binding.last, binding.last_answer = (body, signature), answer
            return answer
        if uci is not None:
            game.play(uci)
            self.applied_moves += 1
        if claim is not None:
            game.claim()
        if game.result is None and game.legal():
            game.play(game.legal()[0])
        answer = self._accept(match_id, seat, binding, body, signature, sequence)
        binding.moves[key] = (json.dumps(move, sort_keys=True), answer)  # type: ignore[assignment]
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

    def supersede(self, match_id: str, seat: str, generation: int) -> None:
        """Rebind the seat elsewhere, as a higher generation redeemed with another session key."""
        other = (
            Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        held = self.bindings[(match_id, seat)]
        self.bindings[(match_id, seat)] = Binding(
            base64.b64encode(other).decode("ascii"), generation, str(uuid.uuid4())
        )
        self.spent.add(held.ticket_id)

    def our_discs(self, match_id: str, seat: str) -> int:
        """Count the seat's discs on the board."""
        role = SEAT_ROLES[seat]
        return sum(cell == role for row in self.games[match_id].board for cell in row)
