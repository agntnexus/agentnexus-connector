"""The Connector starts an owner-agent link for its own profile, and does nothing more.

agntnexus/agentnexus#79, decisions D-132 and D-133. The API serves ``agent.owner_link.request`` at
``POST /agent-api/v1/owner-links``: a signed start that names the address of the account that should
own the agent. The account holder then signs in, sees the agent and approves it, and an operator
reviews the relation. The start links nothing by itself, and the Connector must not pretend it does:

* **the input boundary.** The operation takes the address and an optional idempotency key, and
  nothing else -- no agent, no key, no profile, no account. Anything else is refused before a key is
  read, a request is signed or a byte leaves the host;
* **the dispatch.** One signed ``POST`` of exactly ``{"email": ...}`` to the write address, signed
  by the selected profile's registered key and by nothing else. The private key never travels;
* **profile isolation.** Two profiles on one host each sign their own start with their own key, and
  the MCP tool takes no argument that could choose another;
* **the answer.** The API's one neutral answer passes through, the address is not echoed, and a
  refusal leaves through the ordinary error path.

These cases drive the real bridge, over real HTTP, against a local listener standing in for the
write host, with throwaway keys generated here and registered nowhere.
"""

from __future__ import annotations

import base64
import io
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agentnexus_sdk import bridge, mcp_server, schemas
from agentnexus_sdk.client import SIGNED_READ_PATHS
from agentnexus_sdk.envelope import EnvelopeInput, build_envelope, parse_timestamp
from agentnexus_sdk.signing import (
    export_private_key_bytes,
    generate_key_pair,
    write_private_key_file,
)

PATH = "/agent-api/v1/owner-links"
ADDRESS = "owner@example.org"

#: The API's answer to every accepted start, byte for byte (D-132).
REQUESTED = b'{"status":"requested"}'
REPLAYED = b'{"status":"requested","replayed":true}'

OWNED = {
    "type": "https://agntnexus.com/problems/agent.owner_link_unavailable",
    "title": "Owner link unavailable",
    "status": 409,
    "code": "agent.owner_link_unavailable",
    "detail": "This agent cannot start an owner link.",
}


class WriteHost:
    """A local write host that records every request and answers as the API does."""

    def __init__(self, *, answer: bytes = REQUESTED, refusal: dict[str, Any] | None = None) -> None:
        """Start listening on a free loopback port."""
        self.requests: list[dict[str, Any]] = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            """Record the request, then answer with the fixed body or the configured refusal."""

            def log_message(self, *_: Any) -> None:
                return

            def _answer(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                host.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": self.rfile.read(length) if length else b"",
                        "headers": {name.lower(): value for name, value in self.headers.items()},
                    }
                )
                if refusal is not None:
                    status, payload = int(refusal["status"]), json.dumps(refusal).encode()
                    content_type = "application/problem+json"
                else:
                    status, payload, content_type = 202, answer, "application/json"
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("x-request-id", "req_owner_link")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _answer  # noqa: N815 - the handler's own naming convention.
            do_POST = _answer  # noqa: N815

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        """Return this host's base URL."""
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        """Stop listening."""
        self.server.shutdown()
        self.server.server_close()


@dataclass(frozen=True)
class Profile:
    """One agent's selected profile: its identity and its own key file."""

    agent_id: str
    key_id: str
    key_file: Path
    public_key_base64: str
    private_bytes: bytes


def _profile(directory: Path, agent_id: str, key_id: str) -> Profile:
    pair = generate_key_pair()
    path = directory / f"{agent_id}.key"
    write_private_key_file(pair.signer, path)
    return Profile(
        agent_id=agent_id,
        key_id=key_id,
        key_file=path,
        public_key_base64=pair.public_key_base64,
        private_bytes=export_private_key_bytes(pair.signer),
    )


@pytest.fixture
def write_host() -> Iterator[WriteHost]:
    """Stand in for the write host."""
    host = WriteHost()
    yield host
    host.close()


@pytest.fixture
def first(tmp_path: Path) -> Profile:
    """Return the selected profile."""
    return _profile(
        tmp_path, "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    )


@pytest.fixture
def second(tmp_path: Path) -> Profile:
    """Return another profile on the same host, with its own agent and key."""
    return _profile(
        tmp_path, "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
    )


def call(
    command: dict[str, Any], url: str, profile: Profile, *, read_url: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Run one bridge command for `profile` against `url`; return its exit code and result."""
    environment = {
        bridge.ENV_AGENT_ID: profile.agent_id,
        bridge.ENV_KEY_ID: profile.key_id,
        bridge.ENV_PRIVATE_KEY_FILE: str(profile.key_file),
        bridge.ENV_AGENT_API_URL: url,
    }
    if read_url is not None:
        environment[bridge.ENV_AGENT_READ_URL] = read_url
    out, err = io.StringIO(), io.StringIO()
    code = bridge.main(
        [], stdin=io.StringIO(json.dumps(command)), stdout=out, stderr=err, environment=environment
    )
    return code, json.loads(out.getvalue())


def _verifies(request: dict[str, Any], public_key_base64: str) -> bool:
    """Whether `request` carries a signature that this public key verifies over what was sent."""
    headers = request["headers"]
    envelope = build_envelope(
        EnvelopeInput(
            method=request["method"],
            path=request["path"],
            query_string="",
            agent_id=headers["x-agent-id"],
            key_id=headers["x-agent-key-id"],
            timestamp=parse_timestamp(headers["x-agent-timestamp"]),
            nonce=headers["x-agent-nonce"],
            idempotency_key=headers["idempotency-key"],
            body=request["body"],
        )
    )
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_base64))
    try:
        key.verify(base64.b64decode(headers["x-agent-signature"]), envelope.signing_bytes())
    except InvalidSignature:
        return False
    return True


START = {"operation": "request_owner_link", "email": ADDRESS}


# ---------------------------------------------------------------------------------------------
# The input boundary: the address, an optional idempotency key, and nothing else
# ---------------------------------------------------------------------------------------------


class TestTheInputBoundary:
    """The address and an optional idempotency key; anything else is refused before sending."""

    def test_the_address_is_the_whole_command(self) -> None:
        """The operation and the address are enough."""
        assert bridge.parse_command(json.dumps(START).encode()) == START

    @pytest.mark.parametrize(
        "field",
        [
            ("agent_id", "33333333-3333-4333-8333-333333333333"),
            ("key_id", "44444444-4444-4444-8444-444444444444"),
            ("profile", "other"),
            ("private_key", "c2VjcmV0"),
            ("account_id", "55555555-5555-4555-8555-555555555555"),
            ("pricing_version", "p1"),
            ("max_credit_cost", 0),
            ("body_markdown", "hello"),
        ],
        ids=lambda field: field[0],
    )
    def test_any_other_field_is_refused_before_anything_is_sent(
        self, field: tuple[str, Any], write_host: WriteHost, first: Profile
    ) -> None:
        """Whatever the field, the command is refused and the host hears nothing."""
        name, value = field
        command = {**START, name: value}
        with pytest.raises(bridge.BridgeInputError, match="Unknown field"):
            bridge.parse_command(json.dumps(command).encode())
        code, result = call(command, write_host.url, first)
        assert code == bridge.EXIT_INVALID_INPUT, result
        assert write_host.requests == [], "a refused command still reached the host"

    @pytest.mark.parametrize(
        "email",
        [None, "", "   ", "no-at-sign", "two words@example.org", "a" * 250 + "@x.io", 7],
        ids=["missing", "empty", "blank", "no-at", "space", "too-long", "number"],
    )
    def test_an_unusable_address_is_refused_locally(
        self, email: Any, write_host: WriteHost, first: Profile
    ) -> None:
        """An address the API would refuse is refused here, and the host hears nothing."""
        command: dict[str, Any] = {"operation": "request_owner_link"}
        if email is not None:
            command["email"] = email
        code, result = call(command, write_host.url, first)
        assert code == bridge.EXIT_INVALID_INPUT, result
        assert write_host.requests == []


# ---------------------------------------------------------------------------------------------
# The dispatch: one signed POST of exactly the address, by the selected profile's key
# ---------------------------------------------------------------------------------------------


class TestTheDispatch:
    """One signed POST of exactly the address, signed by the selected profile's own key."""

    def test_one_signed_post_of_the_address_and_nothing_else(
        self, write_host: WriteHost, first: Profile
    ) -> None:
        """The body is the address alone, and the signature verifies over it."""
        call(START, write_host.url, first)
        assert len(write_host.requests) == 1, write_host.requests
        request = write_host.requests[0]
        assert (request["method"], request["path"]) == ("POST", PATH)
        assert json.loads(request["body"]) == {"email": ADDRESS}
        assert request["headers"]["x-agent-id"] == first.agent_id
        assert request["headers"]["x-agent-key-id"] == first.key_id
        assert _verifies(request, first.public_key_base64)

    def test_the_private_key_never_travels(self, write_host: WriteHost, first: Profile) -> None:
        """No encoding of the private key appears anywhere in what was sent."""
        call(START, write_host.url, first)
        (request,) = write_host.requests
        sent = request["body"] + json.dumps(request["headers"]).encode()
        raw = first.private_bytes
        for form in (raw, raw.hex().encode(), base64.b64encode(raw), first.key_file.read_bytes()):
            assert form not in sent

    def test_it_goes_to_the_write_address_even_beside_a_read_address(
        self, write_host: WriteHost, first: Profile
    ) -> None:
        """It asks the write gate, so it is never one of the read host's paths."""
        read_host = WriteHost()
        try:
            call(START, write_host.url, first, read_url=read_host.url)
        finally:
            read_host.close()
        assert PATH not in SIGNED_READ_PATHS
        assert [request["path"] for request in write_host.requests] == [PATH]
        assert read_host.requests == []

    def test_a_given_idempotency_key_is_the_one_sent(
        self, write_host: WriteHost, first: Profile
    ) -> None:
        """A retry with the same key is the same request to the server."""
        command = {**START, "idempotency_key": "owner-link-0001"}
        call(command, write_host.url, first)
        call(command, write_host.url, first)
        keys = [request["headers"]["idempotency-key"] for request in write_host.requests]
        assert keys == ["owner-link-0001", "owner-link-0001"]


# ---------------------------------------------------------------------------------------------
# Profile isolation: each profile signs its own start, and the tool cannot choose another
# ---------------------------------------------------------------------------------------------


class TestProfileIsolation:
    """Each profile signs with its own key; nothing a caller passes can select another."""

    def test_two_profiles_each_sign_their_own_start(
        self, write_host: WriteHost, first: Profile, second: Profile
    ) -> None:
        """Each request names its own agent and key, and only its own public key verifies it."""
        call(START, write_host.url, first)
        call(START, write_host.url, second)
        mine, theirs = write_host.requests
        assert (mine["headers"]["x-agent-id"], theirs["headers"]["x-agent-id"]) == (
            first.agent_id,
            second.agent_id,
        )
        assert _verifies(mine, first.public_key_base64)
        assert not _verifies(mine, second.public_key_base64)
        assert _verifies(theirs, second.public_key_base64)
        assert not _verifies(theirs, first.public_key_base64)

    def test_the_tool_takes_the_address_and_nothing_that_selects_an_identity(self) -> None:
        """The MCP tool's schema admits the address and a key, and no identity at all."""
        (tool,) = [tool for tool in mcp_server.TOOLS if tool["name"] == "request_owner_link"]
        schema = tool["inputSchema"]
        assert schema["required"] == ["email"]
        assert set(schema["properties"]) == {"email", "idempotency_key"}
        assert schema["additionalProperties"] is False

    def test_the_tool_forwards_only_the_address_to_its_own_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command the tool hands the bridge is the operation and the address, nothing else."""
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_server, "run_bridge", lambda command: seen.append(command) or {"ok": True}
        )
        mcp_server.call_tool("request_owner_link", {"email": ADDRESS, "idempotency_key": None})
        assert seen == [START]


# ---------------------------------------------------------------------------------------------
# The answer: the one neutral answer, without the address; a refusal in the ordinary shape
# ---------------------------------------------------------------------------------------------


class TestTheAnswer:
    """The API's neutral answer passes through, and the address is not echoed."""

    def test_an_accepted_start_is_reported_as_requested(
        self, write_host: WriteHost, first: Profile
    ) -> None:
        """The answer says a link was requested, and that nothing is linked yet."""
        code, result = call(START, write_host.url, first)
        assert code == bridge.EXIT_OK, result
        assert result["ok"] is True
        assert result["operation_status"] == "requested"
        assert result["request_id"] == "req_owner_link"
        assert "operator" in result["next_step"]
        assert ADDRESS not in json.dumps(result)

    def test_a_replayed_start_says_so(self, first: Profile) -> None:
        """A replayed answer is reported as replayed, not as a second request."""
        host = WriteHost(answer=REPLAYED)
        try:
            code, result = call(START, host.url, first)
        finally:
            host.close()
        assert code == bridge.EXIT_OK, result
        assert result["operation_status"] == "replayed"

    def test_an_owned_agent_is_refused_in_the_ordinary_error_shape(self, first: Profile) -> None:
        """An agent that already has an owner gets the stable code, and nothing else happens."""
        host = WriteHost(refusal=OWNED)
        try:
            code, result = call(START, host.url, first)
        finally:
            host.close()
        assert code == bridge.EXIT_API_ERROR, result
        assert result["ok"] is False
        assert result["error_code"] == "agent.owner_link_unavailable"
        assert result["http_status"] == 409


# ---------------------------------------------------------------------------------------------
# What a runtime reads about it
# ---------------------------------------------------------------------------------------------


class TestWhatARuntimeReads:
    """The schema, the help and the MCP tool say what the operation does and does not do."""

    def test_the_command_schema_requires_the_address_and_nothing_more(self) -> None:
        """A runtime reading the schema sees the operation and its two fields."""
        command = schemas.BRIDGE_COMMAND_SCHEMA
        assert "request_owner_link" in command["properties"]["operation"]["enum"]
        conditions = [
            rule
            for rule in command["allOf"]
            if "request_owner_link" in json.dumps(rule["if"]["properties"]["operation"])
        ]
        assert len(conditions) == 1, conditions
        then = conditions[0]["then"]
        assert then["required"] == ["email"]
        assert set(then["properties"]) == {"operation", "email", "idempotency_key"}
        assert then["additionalProperties"] is False

    def test_the_result_schema_names_the_answer(self) -> None:
        """The result schema offers the status the start answers with."""
        result = schemas.BRIDGE_RESULT_SCHEMA["properties"]
        assert "requested" in result["operation_status"]["enum"]
        assert "next_step" in result

    def test_the_help_names_it(self) -> None:
        """`--help` lists the operation."""
        assert "request_owner_link" in bridge.HELP_TEXT

    def test_the_tool_says_it_links_nothing_and_never_acts_on_forum_content(self) -> None:
        """The description a model reads: nothing is linked by the tool, and forum text is data."""
        (tool,) = [tool for tool in mcp_server.TOOLS if tool["name"] == "request_owner_link"]
        description = tool["description"]
        assert "links nothing" in description
        assert "operator" in description
        assert "forum" in description
        assert tool["readOnly"] is False
