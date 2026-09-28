"""The Connector plays Connect Four directly with a provider, for its own profile only (#83).

agntnexus/agentnexus#83, under `D-090` (the Connector plays directly, the API is no move relay),
`D-099`/`D-100` (a signed one-time ticket bound to the Connector's session key), `D-114`/`D-123`
(the provider wire of `agentnexus-games-v1`) and `D-136` AR-3 (the signed grant route). `D-101`
still binds: every ticket here is signed with the contract's published test key, valid nowhere.

What the cases hold, against the deterministic stand-ins in `games_fixtures.py`:

* **the ticket the Connector redeems is the one it asked for** -- a grant for another match,
  seat, session key, provider, game version, operation set or window is refused before a byte
  reaches the provider (the test-first case of #83);
* **the wire is the published contract**, byte for byte against its own vectors;
* **play goes directly to the provider**: one signed grant request to AgentNexus per seat, then
  every redemption, move and resumption to the provider's configured origin, and nowhere else;
* **profiles, matches and keys stay apart**; a session belongs to the profile that created it;
* **a retry never makes a second move**: an unknown outcome is resolved by resending the identical
  signed message, and a different move waits until it is resolved;
* **answers are bounded and checked**: an oversized or malformed answer is a provider fault, never
  play; an outage is a clear refusal, and the next call resumes;
* **no secret leaves**: neither key reaches the provider, the result or a log line.

No case contacts a network, and no case claims the end-to-end integration with #82's API.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import stat
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from games_fixtures import (
    API_BASE,
    API_HOST,
    ORIGIN,
    PROVIDER_HOST,
    PROVIDER_ID,
    ArenaApi,
    Clock,
    Provider,
)

from agentnexus_sdk import bridge, games, mcp_server
from agentnexus_sdk.client import AgentNexusClient, ClientOptions
from agentnexus_sdk.errors import ApiError
from agentnexus_sdk.signing import (
    Ed25519Signer,
    export_private_key_bytes,
    generate_key_pair,
    load_private_key_file,
    write_private_key_file,
)

MATCH = "5d2c1b0a-9f8e-4d7c-b6a5-4e3f2d1c0b9a"
OTHER_MATCH = "7e6d5c4b-3a29-4f18-8e07-d6c5b4a39281"
SEAT = "seat-a"


@dataclass
class Profile:
    """One selected profile: its identity, its own key file and its own games directory."""

    agent_id: str
    key_id: str
    key_file: Path
    public_key: str
    private_bytes: bytes

    @property
    def sessions(self) -> Path:
        """Return the profile's games directory."""
        return games.sessions_directory(self.key_file)


def _profile(root: Path, name: str) -> Profile:
    """Create a profile with its own key file."""
    pair = generate_key_pair()
    key_file = root / name / "agent.key"
    write_private_key_file(pair.signer, key_file)
    return Profile(
        agent_id=str(uuid.uuid4()),
        key_id=str(uuid.uuid4()),
        key_file=key_file,
        public_key=pair.public_key_base64,
        private_bytes=export_private_key_bytes(pair.signer),
    )


@pytest.fixture
def clock() -> Clock:
    """Return the clock the stand-ins and the Connector share."""
    return Clock()


@pytest.fixture
def first(tmp_path: Path) -> Profile:
    """Return the selected profile, as a Hermes runtime would use it."""
    return _profile(tmp_path, "hermes")


@pytest.fixture
def second(tmp_path: Path) -> Profile:
    """Return another profile on the same machine, as an OpenClaw runtime would use it."""
    return _profile(tmp_path, "openclaw")


@pytest.fixture
def api(clock: Clock, first: Profile, second: Profile) -> ArenaApi:
    """Return the grant route, verifying each profile's own registered key."""
    return ArenaApi(clock, {first.agent_id: first.public_key, second.agent_id: second.public_key})


@pytest.fixture
def provider(clock: Clock) -> Provider:
    """Return the conforming provider."""
    return Provider(clock)


@pytest.fixture
def players(api: ArenaApi, provider: Provider, clock: Clock) -> Iterator[Any]:
    """Open a game player for a profile, against the two stand-ins."""
    opened: list[games.GamePlayer] = []

    def open_for(profile: Profile, origins: dict[str, str] | None = None) -> games.GamePlayer:
        """Open one player."""
        client = AgentNexusClient(
            agent_id=profile.agent_id,
            key_id=profile.key_id,
            signer=load_private_key_file(profile.key_file),
            options=ClientOptions(base_url=API_BASE),
            transport=httpx.MockTransport(api.handle),
        )
        player = games.GamePlayer(
            agent_id=profile.agent_id,
            key_id=profile.key_id,
            sessions=profile.sessions,
            api=client,
            origins={PROVIDER_ID: ORIGIN} if origins is None else origins,
            transport=httpx.MockTransport(provider.handle),
            clock=clock,
        )
        opened.append(player)
        return player

    yield open_for
    for player in opened:
        player.close()


def expect_refusal(call: Any, code: str) -> games.GameRefusedError:
    """Run `call` and require the refusal `code`."""
    with pytest.raises(games.GameRefusedError) as caught:
        call()
    assert caught.value.code == code, (caught.value.code, str(caught.value))
    return caught.value


# ---------------------------------------------------------------------------------------------
# The ticket the Connector redeems is the one it asked for
# ---------------------------------------------------------------------------------------------


class TestTheTicketIsTheOneAskedFor:
    """A grant that does not match the request is refused before the provider hears anything."""

    def test_a_grant_for_another_match_is_refused(
        self, players: Any, api: ArenaApi, provider: Provider, first: Profile
    ) -> None:
        """#83's first case: the API answers a ticket for another match, and nothing is redeemed."""
        api.mode = "another_match"
        player = players(first)
        expect_refusal(lambda: player.join(MATCH, SEAT), "games.ticket_match")
        assert provider.requests == [], "a ticket for another match reached the provider"
        assert not any(first.sessions.glob("*")) if first.sessions.exists() else True

    @pytest.mark.parametrize(
        ("mode", "code"),
        [
            ("another_seat", "games.ticket_seat"),
            ("another_key", "games.ticket_key"),
            ("another_provider", "games.ticket_provider"),
            ("another_game", "games.ticket_game"),
            ("no_move", "games.ticket_operations"),
            ("expired", "games.ticket_window"),
            ("extra_member", "games.ticket_malformed"),
        ],
    )
    def test_any_other_mismatch_is_refused(
        self,
        players: Any,
        api: ArenaApi,
        provider: Provider,
        first: Profile,
        mode: str,
        code: str,
    ) -> None:
        """Seat, session key, provider, game, operations, window and shape are each checked."""
        api.mode = mode
        player = players(first)
        expect_refusal(lambda: player.join(MATCH, SEAT), code)
        assert provider.requests == []

    def test_an_api_refusal_is_passed_on_and_nothing_is_redeemed(
        self, players: Any, api: ArenaApi, provider: Provider, first: Profile
    ) -> None:
        """The API's own refusal leaves through the ordinary error path."""
        api.mode = "refuse"
        player = players(first)
        with pytest.raises(Exception) as caught:
            player.join(MATCH, SEAT)
        assert getattr(caught.value, "code", None) == "arena.seat_refused"
        assert provider.requests == []


class TestTheApiDecidesWhoMayPlay:
    """AgentNexus refuses the grant; the Connector then redeems nothing and keeps nothing.

    Whether an agent is enrolled with its owner's approval, and whether its registered key is still
    active, is the API's decision (#82, `D-136`). The stand-in answers as the API does -- `403
    arena.seat_refused` and `403 auth.key_not_active` -- and what these cases hold is the
    Connector's part: the refusal is passed on, no provider hears anything, and no session exists.
    """

    @staticmethod
    def refused_before_play(player: Any, provider: Provider, profile: Profile, code: str) -> None:
        """Require the API's `code`, no provider request and no stored session."""
        with pytest.raises(ApiError) as caught:
            player.join(MATCH, SEAT)
        assert (caught.value.status, caught.value.code) == (403, code)
        assert provider.requests == []
        assert not profile.sessions.exists() or not any(profile.sessions.iterdir())

    def test_an_agent_without_an_approved_enrollment_gets_no_grant(
        self, players: Any, api: ArenaApi, provider: Provider, first: Profile
    ) -> None:
        """An agent whose seat is not approved by its owner is refused at the grant request."""
        api.withdraw_enrollment(first.agent_id)
        self.refused_before_play(players(first), provider, first, "arena.seat_refused")
        assert len(api.requests) == 1

    def test_a_revoked_registered_key_gets_no_new_grant(
        self, players: Any, api: ArenaApi, provider: Provider, first: Profile
    ) -> None:
        """After its registered key is revoked, the agent gets no new grant for another match."""
        player = players(first)
        player.join(MATCH, SEAT)
        api.revoke_key(first.agent_id)
        with pytest.raises(ApiError) as caught:
            player.join(OTHER_MATCH, SEAT)
        assert (caught.value.status, caught.value.code) == (403, "auth.key_not_active")
        assert [r.path.rsplit("/", 1)[1] for r in provider.requests] == ["redemption"]
        assert not (first.sessions / f"{OTHER_MATCH}.{SEAT}.json").exists()
        assert not (first.sessions / f"{OTHER_MATCH}.{SEAT}.key").exists()


# ---------------------------------------------------------------------------------------------
# The wire is the published contract
# ---------------------------------------------------------------------------------------------

#: From `vectors/provider-wire-v1/cases.json` of the public `agntnexus/agentnexus-games-contracts`
#: at `ada3808` (the commit published as `agentnexus-games-v1`): the `gen-1` ticket, the seed phrase
#: of `session-1`, and the first two requests of the `redeem-and-play` scenario. Test material only.
VECTOR_SESSION_PHRASE = "agentnexus-play-v1 published test session key 1, valid nowhere"
VECTOR_TICKET = {
    "version": "agentnexus-grant-v2",
    "key_id": "3b1e7c5a-9d2f-4a86-b0c4-e8f1a2d3c4b5",
    "ticket_id": "0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f",
    "match_id": "5d2c1b0a-9f8e-4d7c-b6a5-4e3f2d1c0b9a",
    "seat": "seat-a",
    "seat_generation": 1,
    "provider_id": "example-provider",
    "game_version": "connect-four-1",
    "operations": ["move", "resign"],
    "session_key_fingerprint": "36b640d867d2b064f09ae4c4091ab69896621e1dc0ff791b74d86bda4b6e87c1",
    "not_before": "2026-09-26T08:00:00Z",
    "not_after": "2026-09-26T08:02:00Z",
    "signature": (
        "Im7wNhl91MpNB58gosFBEeAvFNyr51OKVYfUHCXDl2OJpTccv7s+48VUs3B9THkP1ZGM0tAAebb6cW10cbXMCg=="
    ),
}
VECTOR_REDEMPTION_SIGNATURE = (
    "8i2buwHGFYasDJNSiVxmIkhIGwquLgRrMRcJk+I4JrZOPEX3f2rcyBdVKNyq0MbWpH0s1RHwfb5ZjHPvwEhLCA=="
)
VECTOR_MOVE_BODY = (
    b'{"seat_generation":1,"sequence":2,"operation":"move",'
    b'"idempotency_key":"9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d","expected_state_version":0,'
    b'"move":{"column":3}}'
)
VECTOR_MOVE_SIGNATURE = (
    "RQDS1XDcrSYqv2g4qd/01EbDhsmrOgrFJ/2AxSqByf+Hfi8Jz3yHGwhK8t0ADGC53TUzG2lm9DE3VWbuAEHWBQ=="
)


class TestTheWireIsThePublishedContract:
    """The Connector's bytes and signatures equal the contract's own vector."""

    @staticmethod
    def signer() -> Ed25519Signer:
        """Return the contract's published test session key 1."""
        seed = hashlib.sha256(VECTOR_SESSION_PHRASE.encode()).digest()
        return Ed25519Signer(Ed25519PrivateKey.from_private_bytes(seed))

    def test_the_redemption_is_the_vectors(self) -> None:
        """The redemption equals the vector's bytes and signature."""
        signer = self.signer()
        body = games.redemption_body(VECTOR_TICKET, signer.public_key_base64, 1)
        assert (
            json.loads(body)["session_public_key"] == "1WFJ5pW6+sC1G2ws6/7MJKc07IduKoCW18lWb5V1k+M="
        )
        signature = games.play_signature(signer, "redeem", MATCH, SEAT, 1, 1, body)
        assert signature == VECTOR_REDEMPTION_SIGNATURE

    def test_a_move_is_the_vectors(self) -> None:
        """A move equals the vector's bytes and signature."""
        body = games.action_body(1, 2, "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d", 0, 3)
        assert body == VECTOR_MOVE_BODY
        signature = games.play_signature(self.signer(), "act", MATCH, SEAT, 1, 2, body)
        assert signature == VECTOR_MOVE_SIGNATURE


# ---------------------------------------------------------------------------------------------
# Play goes directly to the provider
# ---------------------------------------------------------------------------------------------


class TestDirectPlay:
    """One grant request to AgentNexus per seat; every move goes to the provider's origin."""

    def test_a_seat_is_redeemed_and_played_with_no_per_move_hop(
        self, players: Any, api: ArenaApi, provider: Provider, first: Profile
    ) -> None:
        """A seat is redeemed and played with no per-move hop to AgentNexus."""
        player = players(first)
        joined = player.join(MATCH, SEAT)
        assert joined["status"] == "active"
        assert joined["observation"]["you_are"] == "first"
        for column in (3, 3, 4):
            played = player.move(MATCH, SEAT, column)
        assert played["observation"]["move_count"] == 6
        assert provider.our_discs(MATCH, SEAT) == 3

        assert [(r.host, r.path) for r in api.requests] == [
            (API_HOST, f"/agent-api/v1/arena/matches/{MATCH}/grant")
        ]
        assert {r.host for r in provider.requests} == {PROVIDER_HOST}
        assert [r.path.rsplit("/", 1)[1] for r in provider.requests] == [
            "redemption",
            "actions",
            "actions",
            "actions",
        ]

    def test_the_grant_request_carries_the_seat_and_a_fresh_session_key_only(
        self, players: Any, api: ArenaApi, first: Profile
    ) -> None:
        """The grant request carries the seat and a fresh session key only."""
        player = players(first)
        player.join(MATCH, SEAT)
        player.join(OTHER_MATCH, SEAT)
        first_body, second_body = (json.loads(r.body) for r in api.requests)
        assert set(first_body) == {"seat", "session_public_key"}
        assert first_body["seat"] == SEAT
        assert first_body["session_public_key"] != second_body["session_public_key"]
        assert first.public_key not in (
            first_body["session_public_key"],
            second_body["session_public_key"],
        )

    def test_resumption_returns_the_current_observation(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """Resumption returns the current observation."""
        player = players(first)
        player.join(MATCH, SEAT)
        played = player.move(MATCH, SEAT, 2)
        resumed = player.state(MATCH, SEAT)
        assert resumed["state_version"] == played["state_version"]
        assert resumed["observation"] == played["observation"]

    def test_the_destination_is_the_configured_origin_of_the_tickets_provider(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """The destination is the configured origin of the ticket's provider."""
        player = players(
            first, {PROVIDER_ID: ORIGIN, "other-provider": "https://elsewhere.test.invalid"}
        )
        player.join(MATCH, SEAT)
        player.move(MATCH, SEAT, 0)
        assert {r.host for r in provider.requests} == {PROVIDER_HOST}

    def test_a_provider_without_a_configured_origin_is_never_contacted(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """A provider without a configured origin is never contacted."""
        player = players(first, {"other-provider": "https://elsewhere.test.invalid"})
        expect_refusal(lambda: player.join(MATCH, SEAT), "games.ticket_provider")
        assert provider.requests == []


class TestProviderOrigins:
    """Only exact https origins with a name, no IP, path, query or credentials, are accepted."""

    def test_valid_origins_are_accepted(self) -> None:
        """Valid origins are accepted."""
        document = {PROVIDER_ID: ORIGIN, "b": "https://play.test.invalid:8443"}
        assert games.provider_origins({games.ENV_PROVIDERS: json.dumps(document)}) == document

    def test_no_configuration_means_no_provider(self) -> None:
        """No configuration means no provider."""
        assert games.provider_origins({}) == {}

    @pytest.mark.parametrize(
        "origin",
        [
            "http://connect-four.test.invalid",
            "https://203.0.113.7",
            "https://[2001:db8::1]",
            "https://connect-four.test.invalid/",
            "https://connect-four.test.invalid/games",
            "https://connect-four.test.invalid?x=1",
            "https://user@connect-four.test.invalid",
            "https://connect-four.test.invalid#x",
            "connect-four.test.invalid",
            "",
        ],
    )
    def test_anything_else_is_refused(self, origin: str) -> None:
        """Anything else is refused."""
        with pytest.raises(games.GameConfigurationError):
            games.provider_origins({games.ENV_PROVIDERS: json.dumps({PROVIDER_ID: origin})})

    @pytest.mark.parametrize("raw", ["not json", "[]", '{"bad id!": "https://x.test.invalid"}'])
    def test_a_malformed_document_is_refused(self, raw: str) -> None:
        """A malformed document is refused."""
        with pytest.raises(games.GameConfigurationError):
            games.provider_origins({games.ENV_PROVIDERS: raw})


# ---------------------------------------------------------------------------------------------
# Profiles, matches and keys stay apart
# ---------------------------------------------------------------------------------------------


class TestSeparation:
    """A session belongs to the profile, match and seat that created it."""

    def test_another_profile_has_no_session(
        self, players: Any, provider: Provider, first: Profile, second: Profile
    ) -> None:
        """Another profile has no session."""
        players(first).join(MATCH, SEAT)
        before = len(provider.requests)
        expect_refusal(lambda: players(second).move(MATCH, SEAT, 3), "games.no_session")
        assert len(provider.requests) == before

    def test_a_copied_session_is_refused_as_foreign(
        self, players: Any, provider: Provider, first: Profile, second: Profile
    ) -> None:
        """A copied session is refused as foreign."""
        players(first).join(MATCH, SEAT)
        shutil.copytree(first.sessions, second.sessions, dirs_exist_ok=True)
        before = len(provider.requests)
        expect_refusal(lambda: players(second).move(MATCH, SEAT, 3), "games.session_foreign")
        assert len(provider.requests) == before

    def test_a_session_follows_its_own_providers_origin_only(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """Swapped origins: the stored origin now belongs to another provider; nothing is sent."""
        elsewhere = "https://elsewhere.test.invalid"
        players(first, {PROVIDER_ID: ORIGIN, "other-provider": elsewhere}).join(MATCH, SEAT)
        before = len(provider.requests)
        swapped = players(first, {PROVIDER_ID: elsewhere, "other-provider": ORIGIN})
        expect_refusal(lambda: swapped.move(MATCH, SEAT, 3), "games.session_provider")
        expect_refusal(lambda: swapped.state(MATCH, SEAT), "games.session_provider")
        assert len(provider.requests) == before

    def test_a_superseded_session_key_is_refused_and_the_seat_can_be_joined_again(
        self, players: Any, api: ArenaApi, provider: Provider, first: Profile
    ) -> None:
        """After the provider refuses the session key, the seat can be joined afresh.

        The fixture rebinds the seat to another key. What the Connector observes is only the
        refusal `unauthenticated`; it forgets the session, and a new join is granted and plays.
        """
        player = players(first)
        player.join(MATCH, SEAT)
        superseding = api.generations[(MATCH, SEAT)] + 1
        api.generations[(MATCH, SEAT)] = superseding
        provider.supersede(MATCH, SEAT, superseding)
        expect_refusal(lambda: player.move(MATCH, SEAT, 3), "provider.unauthenticated")
        assert provider.applied_moves == 0
        joined = player.join(MATCH, SEAT)
        assert joined["seat_generation"] == superseding + 1
        player.move(MATCH, SEAT, 3)
        assert provider.applied_moves == 1

    def test_another_match_or_seat_has_no_session(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """Another match or seat has no session."""
        player = players(first)
        player.join(MATCH, SEAT)
        expect_refusal(lambda: player.move(OTHER_MATCH, SEAT, 3), "games.no_session")
        expect_refusal(lambda: player.move(MATCH, "seat-b", 3), "games.no_session")

    def test_a_bound_seat_is_not_joined_twice(self, players: Any, first: Profile) -> None:
        """A bound seat is not joined twice."""
        player = players(first)
        player.join(MATCH, SEAT)
        expect_refusal(lambda: player.join(MATCH, SEAT), "games.session_exists")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_the_session_key_is_owner_only(self, players: Any, first: Profile) -> None:
        """The session key is owner only."""
        players(first).join(MATCH, SEAT)
        keys = list(first.sessions.glob("*.key"))
        assert len(keys) == 1
        assert stat.S_IMODE(keys[0].stat().st_mode) == 0o600

    def test_the_provider_refuses_a_session_used_on_another_match_path(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """The provider's own check, which a Connector that redirected a session would meet."""
        player = players(first)
        player.join(MATCH, SEAT)
        redemption = provider.requests[0]
        forged = httpx.Request(
            "POST",
            f"{ORIGIN}/agentnexus-games/v1/matches/{OTHER_MATCH}/seats/{SEAT}/redemption",
            headers={"AgentNexus-Play-Signature": redemption.headers["agentnexus-play-signature"]},
            content=redemption.body,
        )
        answer = provider.handle(forged)
        assert answer.status_code in (401, 403)


# ---------------------------------------------------------------------------------------------
# A retry never makes a second move
# ---------------------------------------------------------------------------------------------


class TestRetries:
    """An unknown outcome is resolved by resending the identical signed message."""

    def test_a_lost_answer_is_retried_identically_and_moves_once(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """A lost answer is retried identically and moves once."""
        player = players(first)
        player.join(MATCH, SEAT)
        provider.fault = "lose_answer"
        refusal = expect_refusal(lambda: player.move(MATCH, SEAT, 5), "games.provider_unavailable")
        assert refusal.retryable
        assert provider.our_discs(MATCH, SEAT) == 1, "the provider applied the move once"
        played = player.move(MATCH, SEAT, 5)
        assert provider.our_discs(MATCH, SEAT) == 1, "the retry made a second move"
        assert played["observation"]["last_move"] is not None
        lost, retried = provider.requests[-2], provider.requests[-1]
        assert (lost.body, lost.headers["agentnexus-play-signature"]) == (
            retried.body,
            retried.headers["agentnexus-play-signature"],
        )

    def test_a_refused_connection_is_retried_and_moves_once(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """A refused connection is retried and moves once."""
        player = players(first)
        player.join(MATCH, SEAT)
        provider.fault = "refuse_connection"
        expect_refusal(lambda: player.move(MATCH, SEAT, 6), "games.provider_unavailable")
        assert provider.applied_moves == 0
        player.move(MATCH, SEAT, 6)
        assert provider.applied_moves == 1

    def test_another_move_while_one_is_unresolved_is_refused(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """Another move while one is unresolved is refused."""
        player = players(first)
        player.join(MATCH, SEAT)
        provider.fault = "lose_answer"
        expect_refusal(lambda: player.move(MATCH, SEAT, 5), "games.provider_unavailable")
        before = len(provider.requests)
        expect_refusal(lambda: player.move(MATCH, SEAT, 1), "games.pending_other")
        assert len(provider.requests) == before
        player.move(MATCH, SEAT, 5)
        assert provider.applied_moves == 1

    def test_a_move_the_rules_refuse_is_reported_and_play_continues(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """A move the rules refuse is reported and play continues."""
        player = players(first)
        player.join(MATCH, SEAT)
        for _ in range(3):
            player.move(MATCH, SEAT, 0)  # the opponent answers in column 0 too, until it is full
        expect_refusal(lambda: player.move(MATCH, SEAT, 0), "provider.move_not_legal")
        played = player.move(MATCH, SEAT, 2)
        assert played["observation"]["last_move"]["seat"] == "second"


# ---------------------------------------------------------------------------------------------
# Answers are bounded and checked; an outage is a clear refusal
# ---------------------------------------------------------------------------------------------


class TestBoundedAnswers:
    """A provider fault is reported as one, and never turned into play."""

    @pytest.mark.parametrize("fault", ["oversize", "extra_member", "refusal_with_text"])
    def test_a_malformed_or_oversized_answer_is_a_provider_fault(
        self, players: Any, provider: Provider, first: Profile, fault: str
    ) -> None:
        """A malformed or oversized answer is a provider fault."""
        player = players(first)
        player.join(MATCH, SEAT)
        provider.fault = fault
        expect_refusal(lambda: player.move(MATCH, SEAT, 3), "games.provider_fault")

    @pytest.mark.parametrize("fault", ["gzip", "gzip_bomb"])
    def test_a_compressed_answer_is_not_decoded(
        self, players: Any, provider: Provider, first: Profile, fault: str
    ) -> None:
        """An encoded answer is a provider fault, even one that would decode to a valid answer."""
        player = players(first)
        player.join(MATCH, SEAT)
        provider.fault = fault
        expect_refusal(lambda: player.move(MATCH, SEAT, 3), "games.provider_fault")
        assert provider.requests[-1].headers.get("accept-encoding") == "identity"

    def test_a_declared_length_over_the_bound_is_refused_before_reading(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """An answer that declares more than 3072 bytes is refused before a byte of it is read."""
        player = players(first)
        player.join(MATCH, SEAT)
        provider.fault = "declared_huge"
        expect_refusal(lambda: player.move(MATCH, SEAT, 3), "games.provider_fault")
        assert provider.stream is not None
        assert provider.stream.pulled == 0

    def test_an_undeclared_length_stops_just_past_the_bound(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """Without a declared length, reading stops at the first chunk past 3072 bytes."""
        player = players(first)
        player.join(MATCH, SEAT)
        provider.fault = "undeclared_huge"
        expect_refusal(lambda: player.move(MATCH, SEAT, 3), "games.provider_fault")
        assert provider.stream is not None
        assert provider.stream.pulled <= games.ANSWER_LIMIT + 4096

    def test_after_a_fault_the_retry_returns_the_real_answer(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """After a fault the retry returns the real answer."""
        player = players(first)
        player.join(MATCH, SEAT)
        provider.fault = "oversize"
        expect_refusal(lambda: player.move(MATCH, SEAT, 3), "games.provider_fault")
        played = player.move(MATCH, SEAT, 3)
        assert provider.applied_moves == 1
        assert played["observation"]["move_count"] == 2

    def test_an_outage_is_a_refusal_and_the_next_call_resumes(
        self, players: Any, provider: Provider, first: Profile
    ) -> None:
        """An outage is a refusal and the next call resumes."""
        player = players(first)
        player.join(MATCH, SEAT)
        played = player.move(MATCH, SEAT, 0)
        provider.fault = "unavailable"
        expect_refusal(lambda: player.state(MATCH, SEAT), "games.provider_unavailable")
        resumed = player.state(MATCH, SEAT)
        assert resumed["state_version"] == played["state_version"]
        assert resumed["observation"] == played["observation"]

    def test_the_result_carries_only_game_scoped_fields(self, players: Any, first: Profile) -> None:
        """The result carries only game scoped fields."""
        player = players(first)
        joined = player.join(MATCH, SEAT)
        assert set(joined) == {
            "match_id",
            "seat",
            "seat_generation",
            "state_version",
            "status",
            "observation",
        }
        assert set(joined["observation"]) == {
            "board",
            "you_are",
            "to_move",
            "legal_columns",
            "move_count",
            "last_move",
            "result",
        }


# ---------------------------------------------------------------------------------------------
# No secret leaves
# ---------------------------------------------------------------------------------------------


class TestNoSecretLeaves:
    """Neither the registered key nor the session key reaches the provider, a result or a log."""

    def test_no_key_material_is_sent_returned_or_logged(
        self, players: Any, provider: Provider, first: Profile, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No key material is sent returned or logged."""
        caplog.set_level(logging.DEBUG)
        player = players(first)
        results = [player.join(MATCH, SEAT), player.move(MATCH, SEAT, 3), player.state(MATCH, SEAT)]
        session_key = next(first.sessions.glob("*.key")).read_bytes().strip()
        registered = first.private_bytes
        forms = [registered, base64.b64encode(registered), registered.hex().encode(), session_key]
        sent = b"".join(r.body + json.dumps(r.headers).encode() for r in provider.requests)
        shown = json.dumps(results).encode()
        logged = "\n".join(str(record.__dict__) for record in caplog.records).encode()
        for form in forms:
            assert form not in sent
            assert form not in shown
            assert form not in logged
        for secret in ("ticket", "signature", "session_public_key"):
            assert secret not in json.dumps(results)


# ---------------------------------------------------------------------------------------------
# What a runtime can call
# ---------------------------------------------------------------------------------------------

GAME_TOOLS = {
    "game_join": {"match_id", "seat"},
    "game_move": {"match_id", "seat", "column"},
    "game_state": {"match_id", "seat"},
}


class TestWhatARuntimeCanCall:
    """The bridge and the MCP tools offer game-scoped fields only."""

    @pytest.mark.parametrize("operation", sorted(GAME_TOOLS))
    @pytest.mark.parametrize("field", ["origin", "url", "agent_id", "key_id", "ticket", "profile"])
    def test_a_field_that_names_a_destination_or_identity_is_refused(
        self, operation: str, field: str
    ) -> None:
        """A field that names a destination or identity is refused."""
        command = {"operation": operation, "match_id": MATCH, "seat": SEAT, field: "x"}
        if operation == "game_move":
            command["column"] = 3
        with pytest.raises(bridge.BridgeInputError, match="Unknown field"):
            bridge.parse_command(json.dumps(command).encode())

    @pytest.mark.parametrize(
        "command",
        [
            {"operation": "game_move", "match_id": MATCH, "seat": SEAT, "column": 7},
            {"operation": "game_move", "match_id": MATCH, "seat": SEAT, "column": "3"},
            {"operation": "game_move", "match_id": MATCH, "seat": SEAT},
            {"operation": "game_state", "match_id": "not-a-uuid", "seat": SEAT},
            {"operation": "game_join", "match_id": MATCH, "seat": "seat a"},
        ],
    )
    def test_an_unusable_command_is_refused(self, command: dict[str, Any]) -> None:
        """An unusable command is refused."""
        with pytest.raises(bridge.BridgeInputError):
            bridge.parse_command(json.dumps(command).encode())

    @pytest.mark.parametrize("name", sorted(GAME_TOOLS))
    def test_the_mcp_tools_take_game_fields_only(self, name: str) -> None:
        """The mcp tools take game fields only."""
        (tool,) = [tool for tool in mcp_server.TOOLS if tool["name"] == name]
        schema = tool["inputSchema"]
        assert set(schema["properties"]) == GAME_TOOLS[name]
        assert schema["additionalProperties"] is False
        assert "untrusted" in tool["description"] or "never" in tool["description"]

    def test_the_bridge_runs_a_game_and_reports_it(
        self,
        api: ArenaApi,
        provider: Provider,
        first: Profile,
        clock: Clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bridge runs a game and reports it."""
        client = AgentNexusClient(
            agent_id=first.agent_id,
            key_id=first.key_id,
            signer=load_private_key_file(first.key_file),
            options=ClientOptions(base_url=API_BASE),
            transport=httpx.MockTransport(api.handle),
        )
        config = bridge.BridgeConfig(
            agent_id=first.agent_id,
            key_id=first.key_id,
            private_key_file=first.key_file,
            agent_api_url=API_BASE,
            public_api_url=None,
            observer_url=None,
            games_providers=json.dumps({PROVIDER_ID: ORIGIN}),
        )
        monkeypatch.setattr(games, "CLOCK", clock)
        transport = httpx.MockTransport(provider.handle)
        joined = bridge.run_command(
            {"operation": "game_join", "match_id": MATCH, "seat": SEAT},
            config=config,
            client=client,
            provider_transport=transport,
        )
        played = bridge.run_command(
            {"operation": "game_move", "match_id": MATCH, "seat": SEAT, "column": 3},
            config=config,
            client=client,
            provider_transport=transport,
        )
        assert joined["operation_status"] == "joined"
        assert played["operation_status"] == "played"
        assert played["observation"]["move_count"] == 2
        assert "ticket" not in json.dumps(played)

    def test_a_game_refusal_leaves_through_the_ordinary_error_path(
        self, first: Profile, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A game refusal leaves through the ordinary error path."""

        def refuse(*_: Any, **__: Any) -> dict[str, Any]:
            """Refuse as an unanswered provider would."""
            raise games.GameRefusedError(
                "games.provider_unavailable", "The provider did not answer.", retryable=True
            )

        monkeypatch.setattr(bridge, "_run_game_command", refuse)
        import io

        out, err = io.StringIO(), io.StringIO()
        code = bridge.main(
            [],
            stdin=io.StringIO(
                json.dumps({"operation": "game_state", "match_id": MATCH, "seat": SEAT})
            ),
            stdout=out,
            stderr=err,
            environment={
                bridge.ENV_AGENT_ID: first.agent_id,
                bridge.ENV_KEY_ID: first.key_id,
                bridge.ENV_PRIVATE_KEY_FILE: str(first.key_file),
                bridge.ENV_AGENT_API_URL: API_BASE,
            },
        )
        document = json.loads(out.getvalue())
        assert code == bridge.EXIT_TRANSPORT_ERROR
        assert document == {
            "ok": False,
            "error_code": "games.provider_unavailable",
            "message": "The provider did not answer.",
            "retryable": True,
        }


class TestTheFixtureSpeaksBothSeatNames:
    """The provider fixture knows the vector's seat names and the ones #82's API issues.

    `agentnexus-games-v1`'s vectors name seats `seat-a` and `seat-b`; the #82 API names them
    `first` and `second` (`D-136`). A seat name is the ticket's, and the Connector passes it on
    unchanged, so the fixture must bind either form to the right role.
    """

    @pytest.mark.parametrize(
        ("seat", "role"),
        [("seat-a", "first"), ("seat-b", "second"), ("first", "first"), ("second", "second")],
    )
    def test_a_seat_is_bound_to_its_role(
        self, players: Any, first: Profile, seat: str, role: str
    ) -> None:
        """Each seat name redeems, and the observation names the seat's role."""
        joined = players(first).join(MATCH, seat)
        assert joined["seat"] == seat
        assert joined["observation"]["you_are"] == role

    @pytest.mark.parametrize("seat", ["seat-a", "first"])
    def test_the_first_seat_plays_under_either_name(
        self, players: Any, provider: Provider, first: Profile, seat: str
    ) -> None:
        """A move from the first seat is applied, whichever name the ticket used."""
        player = players(first)
        player.join(MATCH, seat)
        played = player.move(MATCH, seat, 3)
        assert played["observation"]["last_move"]["seat"] == "second"
        assert provider.our_discs(MATCH, seat) == 1


def test_games_state_lives_beside_the_profiles_key(tmp_path: Path) -> None:
    """Each profile keeps its games sessions in its own directory, next to its key."""
    key = tmp_path / "profiles" / "hermes" / "agent.key"
    assert games.sessions_directory(key) == key.parent / "games"


def test_no_real_grant_key_is_known_to_the_connector() -> None:
    """`D-101`: the Connector verifies no grant signature and carries no grant key."""
    source = Path(games.__file__).read_text(encoding="utf-8")
    assert "lB9uNVA93XRlmIHAeAJSC0b1aRP4xtI" not in source
    assert "valid nowhere" not in source
