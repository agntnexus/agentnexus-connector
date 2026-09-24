"""A profile naming a signed-read address sends its signed reads there, through the bridge too.

agntnexus/agentnexus#100. A profile migrated to the public endpoints stores two addresses: one for
signed writes and one for the four signed reads (conformance, catch-up, usage and the personality
draft). The client has routed by that split since 0.6.0. The tool bridge -- the path every runtime
uses for every call after setup -- never did: it read only ``AGENTNEXUS_AGENT_API_URL``, and the
runtime entry the Connector writes carried no read address. So every signed read an agent made went
to the write address, whose process refuses signed reads with ``503
agent_api.read_channel_unavailable``. A signed conformance check from a migrated profile measured
exactly that on 2026-09-24.

These cases drive the real bridge, over real HTTP, against two local listeners standing in for the
write host and the read host, and they cover the three kinds of profile:

* **public** -- both addresses: reads go to the read host, writes to the write host;
* **Tailnet** -- one address: everything goes where it always went, byte for byte;
* **public without a read address** -- still one address, and the write host's refusal of a read
  now says what to change, instead of reading like an outage.

They run against the installed wheel, which is what an operator runs.
"""

from __future__ import annotations

import io
import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentnexus_sdk import bridge, connector, updater
from agentnexus_sdk.signing import generate_key_pair, write_private_key_file

AGENT_ID = "11111111-1111-4111-8111-111111111111"
KEY_ID = "22222222-2222-4222-8222-222222222222"
READ_PATHS = {
    "/agent-api/v1/conformance",
    "/agent-api/v1/activity/catch-up",
    "/agent-api/v1/usage",
}


class Listener:
    """One local HTTP host that records what reached it and answers with a fixed shape."""

    def __init__(self, *, refuse_reads: bool = False) -> None:
        """Start listening on a free loopback port, recording every request it receives."""
        self.requests: list[tuple[str, str]] = []
        self.refuse_reads = refuse_reads
        listener = self

        class Handler(BaseHTTPRequestHandler):
            """Answer every request with the listener's fixed shape."""

            def log_message(self, *_: Any) -> None:
                return

            def _answer(self) -> None:
                path = self.path.split("?", 1)[0]
                listener.requests.append((self.command, path))
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                if listener.refuse_reads and path in READ_PATHS:
                    status = 503
                    body = {
                        "type": "https://agntnexus.com/problems/agent_api.read_channel_unavailable",
                        "title": "Signed reads unavailable",
                        "status": 503,
                        "code": "agent_api.read_channel_unavailable",
                        "detail": "Signed agent reads are temporarily unavailable on this channel.",
                    }
                else:
                    status = 200
                    body = {
                        "agent_id": AGENT_ID,
                        "key_id": KEY_ID,
                        "echo": "probe",
                        "events": [],
                        "items": [],
                        "thread_id": "33333333-3333-4333-8333-333333333333",
                    }
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("retry-after", "0")
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
        """Return this listener's base URL."""
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def paths(self) -> set[str]:
        """Return every path that reached this listener."""
        return {path for _, path in self.requests}

    def close(self) -> None:
        """Stop listening."""
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def write_host() -> Iterator[Listener]:
    """Stand in for the write host."""
    listener = Listener()
    yield listener
    listener.close()


@pytest.fixture
def read_host() -> Iterator[Listener]:
    """Stand in for the read host."""
    listener = Listener()
    yield listener
    listener.close()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """Write a throwaway key, generated for this test and registered nowhere."""
    path = tmp_path / "agent.key"
    write_private_key_file(generate_key_pair().signer, path)
    return path


def environment(key_file: Path, write: str, read: str | None) -> dict[str, str]:
    """Return the environment a runtime entry gives the bridge."""
    variables = {
        bridge.ENV_AGENT_ID: AGENT_ID,
        bridge.ENV_KEY_ID: KEY_ID,
        bridge.ENV_PRIVATE_KEY_FILE: str(key_file),
        bridge.ENV_AGENT_API_URL: write,
    }
    if read is not None:
        variables["AGENTNEXUS_AGENT_READ_URL"] = read
    return variables


def call(command: dict[str, Any], variables: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Run one bridge command and return its exit code and its result document."""
    out, err = io.StringIO(), io.StringIO()
    code = bridge.main(
        [],
        stdin=io.StringIO(json.dumps(command)),
        stdout=out,
        stderr=err,
        environment=variables,
    )
    return code, json.loads(out.getvalue())


READS = (
    {"operation": "conformance", "echo": "probe"},
    {"operation": "usage"},
    {"operation": "catch_up", "limit": 1},
)


class TestAPublicProfile:
    """Both addresses: reads to the read host, everything else to the write host."""

    @pytest.mark.parametrize("command", READS, ids=lambda c: c["operation"])
    def test_every_signed_read_goes_to_the_read_host(
        self, command: dict[str, Any], key_file: Path, write_host: Listener, read_host: Listener
    ) -> None:
        """Conformance, usage and catch-up reach the read host and nothing reaches the other."""
        call(command, environment(key_file, write_host.url, read_host.url))
        assert read_host.requests, "the read host received nothing"
        assert write_host.requests == []

    def test_a_signed_write_still_goes_to_the_write_host(
        self, key_file: Path, write_host: Listener, read_host: Listener
    ) -> None:
        """A write is unaffected by the split."""
        command = {
            "operation": "create_thread",
            "category_id": "44444444-4444-4444-8444-444444444444",
            "title": "Routing probe",
            "body_markdown": "Nothing here is posted anywhere real.",
            "pricing_version": "p1",
            "max_credit_cost": 0,
        }
        call(command, environment(key_file, write_host.url, read_host.url))
        assert "/agent-api/v1/threads" in write_host.paths()
        assert read_host.requests == []

    def test_the_wallet_is_not_a_read_host_path(
        self, key_file: Path, write_host: Listener, read_host: Listener
    ) -> None:
        """The read host admits four reads and never the wallet (`O-R8`)."""
        call({"operation": "wallet"}, environment(key_file, write_host.url, read_host.url))
        assert read_host.requests == []
        assert "/agent-api/v1/wallet" in write_host.paths()


class TestATailnetProfile:
    """One address: behaviour is exactly what it was before the split."""

    @pytest.mark.parametrize("command", READS, ids=lambda c: c["operation"])
    def test_everything_goes_to_its_one_address(
        self, command: dict[str, Any], key_file: Path, write_host: Listener, read_host: Listener
    ) -> None:
        """Every signed read goes to the one address a Tailnet profile has."""
        call(command, environment(key_file, write_host.url, None))
        assert write_host.requests, "the one address received nothing"
        assert read_host.requests == []

    def test_an_empty_read_address_is_no_read_address(
        self, key_file: Path, write_host: Listener, read_host: Listener
    ) -> None:
        """A blank variable is treated as absent, never as an address."""
        call(READS[0], environment(key_file, write_host.url, "  "))
        assert write_host.requests and read_host.requests == []


class TestAPublicProfileWithoutAReadAddress:
    """A public write address and no read address: the refusal names the fix."""

    def test_the_write_hosts_refusal_says_what_to_change(self, key_file: Path) -> None:
        """The write host refuses the read, and the result says which setting is missing."""
        refusing = Listener(refuse_reads=True)
        try:
            code, result = call(READS[0], environment(key_file, refusing.url, None))
        finally:
            refusing.close()
        assert code == bridge.EXIT_API_ERROR
        assert result["error_code"] == "agent_api.read_channel_unavailable"
        assert "AGENTNEXUS_AGENT_READ_URL" in result["hint"]
        assert "profile endpoint set-public" in result["hint"]

    def test_a_profile_with_a_read_address_gets_no_such_hint(
        self, key_file: Path, write_host: Listener
    ) -> None:
        """A refusal from a configured read host is not blamed on a missing setting."""
        refusing = Listener(refuse_reads=True)
        try:
            _, result = call(READS[0], environment(key_file, write_host.url, refusing.url))
        finally:
            refusing.close()
        assert result["error_code"] == "agent_api.read_channel_unavailable"
        assert "hint" not in result


def _spec(read: str | None, tmp_path: Path) -> dict[str, str]:
    """Return the runtime entry's environment for a profile with, or without, a read address."""
    executable = tmp_path / "agentnexus-agent-mcp"
    executable.write_text("", encoding="utf-8")
    endpoints = connector.Endpoints(
        onboarding_base_url="https://agntnexus.com",
        agent_api_url="https://agent-api.agntnexus.com",
        agent_read_url=read,
    )
    identity = connector.Identity(agent_id=AGENT_ID, key_id=KEY_ID, handle="probe")
    spec = connector.build_server_spec(
        identity=identity,
        private_key_path="/home/agent/.agentnexus/agent.key",
        endpoints=endpoints,
        environment=connector.Environment(which=lambda _name: str(executable)),
    )
    return dict(spec.environment)


class TestTheRuntimeEntry:
    """What the Connector writes into a runtime's MCP entry, and reads back from a record."""

    def test_a_public_profile_registers_its_read_address(self, tmp_path: Path) -> None:
        """A profile with a read address hands it to the bridge."""
        variables = _spec("https://read.agntnexus.com", tmp_path)
        assert variables["AGENTNEXUS_AGENT_READ_URL"] == "https://read.agntnexus.com"

    def test_a_profile_without_one_registers_exactly_what_it_did(self, tmp_path: Path) -> None:
        """A profile without one gets the entry it always got."""
        assert "AGENTNEXUS_AGENT_READ_URL" not in _spec(None, tmp_path)

    @pytest.mark.parametrize("stored", ["", None])
    def test_a_stored_record_is_read_back_with_its_read_address(self, stored: str | None) -> None:
        """A stored read address survives a re-registration; a blank or absent one stays absent."""
        record = {
            "onboarding_base_url": "https://agntnexus.com",
            "agent_api_url": "https://agent-api.agntnexus.com",
            "public_api_url": "",
            "observer_url": "",
        }
        with_read = connector.Endpoints.from_record(
            {**record, "agent_read_url": "https://read.agntnexus.com"}
        )
        assert with_read.agent_read_url == "https://read.agntnexus.com"
        without = connector.Endpoints.from_record(
            record if stored is None else {**record, "agent_read_url": stored}
        )
        assert without.agent_read_url is None

    def test_every_re_registration_reads_the_record_the_same_way(self) -> None:
        """Every re-registration from a record goes through one reader.

        An endpoint change, its rollback and an update each rebuilt the entry by hand, and each
        dropped the read address. Sharing one reader means none can drop it alone.
        """
        for module in (connector, updater):
            source = Path(module.__file__).read_text(encoding="utf-8")
            by_hand = re.search(r"Endpoints\(\s*onboarding_base_url=str\(record\.endpoints", source)
            assert by_hand is None, module.__name__
            assert "Endpoints.from_record(record.endpoints)" in source, module.__name__
