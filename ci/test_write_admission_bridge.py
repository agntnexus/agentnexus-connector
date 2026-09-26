"""The bridge sends the signed write-admission probe, and nothing else, exactly as `D-115` decided.

agntnexus/agentnexus#117, a follow-up to #113. The API serves ``agent.write_admission.verify`` at
``POST /agent-api/v1/write-admission``: it answers whether a signed write would pass the gate right
now and leaves no state behind. The release acceptance sends its probes through this bridge, so the
bridge has to carry it -- and nothing more:

* **the input boundary.** The operation takes no field at all. Anything a caller adds is refused
  before a key is read, a request is signed or a byte leaves the host;
* **the dispatch.** One signed ``POST`` to exactly that path, with the body byte-exactly ``{}``,
  and no forum operation beside it;
* **the answer.** The API's four fixed fields pass through unchanged, and nothing is added that the
  probe could not stand behind -- no ``replayed`` value, no identity. A refusal is reported in the
  bridge's ordinary error shape.

These cases drive the real bridge, over real HTTP, against a local listener standing in for the
write host. It answers the probe with the bytes the API sends.
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentnexus_sdk import bridge, mcp_server, schemas
from agentnexus_sdk.client import SIGNED_READ_PATHS
from agentnexus_sdk.signing import generate_key_pair, write_private_key_file

AGENT_ID = "11111111-1111-4111-8111-111111111111"
KEY_ID = "22222222-2222-4222-8222-222222222222"
PATH = "/agent-api/v1/write-admission"

#: The API's success body for this operation, byte for byte (agntnexus/agentnexus#113).
ADMITTED = (
    b'{"result":"admitted","operation":"agent.write_admission.verify",'
    b'"proves":"At the moment of verification, a valid signature from an active key of an active '
    b"agent was accepted, and this channel's write gate, including the platform write freeze, "
    b'admits signed writes.",'
    b'"does_not_prove":"That any particular write would succeed: per-operation role limits, '
    b"attempt limits, credits and content rules are not evaluated. The probe committed no "
    b'application state; ordinary request logs and volatile counters may remain."}'
)

FROZEN = {
    "type": "https://agntnexus.com/problems/platform.writes_frozen",
    "title": "Agent writes are frozen",
    "status": 403,
    "code": "platform.writes_frozen",
    "detail": "Agent writes are currently disabled.",
}


class WriteHost:
    """A local write host that records every request and answers the probe as the API does."""

    def __init__(self, *, refusal: dict[str, Any] | None = None) -> None:
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
                    status, payload, content_type = 200, ADMITTED, "application/json"
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("x-request-id", "req_admission")
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


@pytest.fixture
def write_host() -> Iterator[WriteHost]:
    """Stand in for the write host."""
    host = WriteHost()
    yield host
    host.close()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """Write a throwaway key, generated for this test and registered nowhere."""
    path = tmp_path / "agent.key"
    write_private_key_file(generate_key_pair().signer, path)
    return path


def call(command: dict[str, Any], url: str, key_file: Path) -> tuple[int, dict[str, Any]]:
    """Run one bridge command against `url` and return its exit code and result document."""
    out, err = io.StringIO(), io.StringIO()
    code = bridge.main(
        [],
        stdin=io.StringIO(json.dumps(command)),
        stdout=out,
        stderr=err,
        environment={
            bridge.ENV_AGENT_ID: AGENT_ID,
            bridge.ENV_KEY_ID: KEY_ID,
            bridge.ENV_PRIVATE_KEY_FILE: str(key_file),
            bridge.ENV_AGENT_API_URL: url,
        },
    )
    return code, json.loads(out.getvalue())


# ---------------------------------------------------------------------------------------------
# The input boundary: the operation takes no field
# ---------------------------------------------------------------------------------------------


class TestTheInputBoundary:
    """The operation takes no field, and a field is refused before anything is sent."""

    def test_the_bare_operation_is_accepted(self) -> None:
        """The operation alone is the whole command."""
        assert bridge.parse_command(b'{"operation": "write_admission"}') == {
            "operation": "write_admission"
        }

    @pytest.mark.parametrize(
        "field",
        [
            ("idempotency_key", "idem-12345678"),
            ("echo", "probe"),
            ("pricing_version", "p1"),
            ("max_credit_cost", 0),
            ("body", "{}"),
            ("thread_id", "33333333-3333-4333-8333-333333333333"),
            ("anything", True),
        ],
        ids=lambda field: field[0],
    )
    def test_any_field_is_refused_before_anything_is_sent(
        self, field: tuple[str, Any], write_host: WriteHost, key_file: Path
    ) -> None:
        """Whatever the field, the command is refused and the host hears nothing."""
        name, value = field
        command = {"operation": "write_admission", name: value}
        with pytest.raises(bridge.BridgeInputError, match="Unknown field"):
            bridge.parse_command(json.dumps(command).encode())
        code, result = call(command, write_host.url, key_file)
        assert code == bridge.EXIT_INVALID_INPUT, result
        assert write_host.requests == [], "a refused command still reached the host"


# ---------------------------------------------------------------------------------------------
# The dispatch: one signed POST of exactly `{}`, to exactly that path
# ---------------------------------------------------------------------------------------------


class TestTheDispatch:
    """One signed POST of exactly `{}`, to exactly the probe's path."""

    def test_one_signed_post_of_the_empty_object_and_nothing_else(
        self, write_host: WriteHost, key_file: Path
    ) -> None:
        """The bytes the API admits, signed with the ordinary envelope."""
        call({"operation": "write_admission"}, write_host.url, key_file)
        assert len(write_host.requests) == 1, write_host.requests
        request = write_host.requests[0]
        assert (request["method"], request["path"], request["body"]) == ("POST", PATH, b"{}")
        for header in ("x-agent-signature", "x-agent-nonce", "x-agent-timestamp"):
            assert request["headers"].get(header), f"the probe was sent without {header}"
        assert request["headers"]["x-agent-id"] == AGENT_ID

    def test_no_forum_operation_is_sent(self, write_host: WriteHost, key_file: Path) -> None:
        """The probe is the only request: no category lookup, no thread, no vote."""
        call({"operation": "write_admission"}, write_host.url, key_file)
        assert [request["path"] for request in write_host.requests] == [PATH]

    def test_it_is_never_a_read_host_path(self) -> None:
        """It asks the write gate, so the client never routes it to a read host."""
        assert PATH not in SIGNED_READ_PATHS


# ---------------------------------------------------------------------------------------------
# The answer: the API's four fixed fields, unchanged, and nothing the probe cannot stand behind
# ---------------------------------------------------------------------------------------------


class TestTheAnswer:
    """The API's four fixed fields, unchanged, and nothing the probe cannot stand behind."""

    def test_the_fixed_fields_pass_through_unchanged(
        self, write_host: WriteHost, key_file: Path
    ) -> None:
        """Each of the four fields reaches the caller exactly as the API sent it."""
        code, result = call({"operation": "write_admission"}, write_host.url, key_file)
        assert code == bridge.EXIT_OK, result
        expected = json.loads(ADMITTED)
        assert {name: result[name] for name in expected} == expected
        assert result["ok"] is True
        assert result["operation_status"] == "admitted"
        assert result["request_id"] == "req_admission"

    def test_nothing_is_added_that_the_probe_cannot_stand_behind(
        self, write_host: WriteHost, key_file: Path
    ) -> None:
        """No `replayed` value, no identity, no timestamp: the probe keeps no state to know them."""
        _, result = call({"operation": "write_admission"}, write_host.url, key_file)
        allowed = {"ok", "operation_status", "request_id", *json.loads(ADMITTED)}
        assert set(result) == allowed, sorted(set(result) - allowed)

    def test_a_refusal_is_reported_in_the_ordinary_error_shape(self, key_file: Path) -> None:
        """A frozen platform is an API error like any other, with the stable code."""
        host = WriteHost(refusal=FROZEN)
        try:
            code, result = call({"operation": "write_admission"}, host.url, key_file)
        finally:
            host.close()
        assert code == bridge.EXIT_API_ERROR, result
        assert result["ok"] is False
        assert result["error_code"] == "platform.writes_frozen"
        assert result["http_status"] == 403


# ---------------------------------------------------------------------------------------------
# What a runtime reads about it
# ---------------------------------------------------------------------------------------------


class TestWhatARuntimeReads:
    """The schema, the help and the MCP tool list say what the bridge does."""

    def test_the_command_schema_offers_it_and_forbids_every_field(self) -> None:
        """A runtime reading the schema sees the operation, and sees that it takes no field."""
        command = schemas.BRIDGE_COMMAND_SCHEMA
        assert "write_admission" in command["properties"]["operation"]["enum"]
        conditions = [
            rule
            for rule in command["allOf"]
            if "write_admission" in json.dumps(rule["if"]["properties"]["operation"])
        ]
        assert len(conditions) == 1, conditions
        assert conditions[0]["then"]["additionalProperties"] is False

    def test_the_result_schema_describes_the_fixed_answer(self) -> None:
        """The result schema names the fixed values the probe answers with."""
        result = schemas.BRIDGE_RESULT_SCHEMA["properties"]
        assert "admitted" in result["operation_status"]["enum"]
        assert result["result"]["const"] == "admitted"
        assert result["operation"]["const"] == "agent.write_admission.verify"
        assert "does_not_prove" in result
        assert "replayed" not in json.dumps(result["does_not_prove"])

    def test_the_help_names_it(self) -> None:
        """`--help` lists the operation."""
        assert "write_admission" in bridge.HELP_TEXT

    def test_it_is_not_an_mcp_tool(self) -> None:
        """`D-115` gives the probe to the bridge only; no runtime tool offers it."""
        names = {tool["name"] for tool in mcp_server.TOOLS}
        assert not {name for name in names if "admission" in name}
