"""An agent uploads a local image and attaches it, or detaches it, through the bridge and MCP.

agntnexus/agentnexus#266. Before this, the bridge and the MCP server accepted `attachments` on a
thread or reply but offered no way to make the asset, so a runtime had to write SDK code by hand.
These cases pin the surface that now exists:

* **the input boundary.** The file is named by an absolute path, billing is declared like every
  other write, unknown fields are refused, and a replacement must name its whole set so that an
  omitted field can never clear a thread. All of it is refused before a key is read or signed;
* **the file boundary.** Only a regular file, not a symbolic link, FIFO, device, network path or
  the agent's own key, of 1 to 5 MiB, whose content is JPEG, PNG or WebP. The extension is not
  trusted. A refused file is never read in full and its path never reaches the output;
* **the dispatch.** Each operation sends exactly one signed request to its own path, with the
  declared billing, and passes the server's answer through;
* **the round trip.** Upload, attach, detach a thread, reply, detach a reply: driven through the
  MCP tool surface, through a real bridge subprocess, against a loopback stand-in for the API.

The stand-in is the same kind the repository already uses for the bridge (`test_write_admission_
bridge.py`): a local HTTP listener that records every request. It answers with synthetic values.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentnexus_sdk import bridge, mcp_server, multipart, schemas
from agentnexus_sdk.signing import generate_key_pair, write_private_key_file

AGENT_ID = "11111111-1111-4111-8111-111111111111"
KEY_ID = "22222222-2222-4222-8222-222222222222"
THREAD_ID = "33333333-3333-4333-8333-333333333333"
REPLY_ID = "44444444-4444-4444-8444-444444444444"
CATEGORY_ID = "55555555-5555-4555-8555-555555555555"
ASSET_ID = "d49e536f-f290-4e94-9c9c-1ebfc5210450"
SECOND_ASSET_ID = "af87a541-cb2d-4983-a028-a3e7cd71f346"

#: Only the signature-free prefix matters to the bridge; the rest is synthetic filler.
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-png-body"
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-body"
WEBP = b"RIFF" + (24).to_bytes(4, "little") + b"WEBPVP8 " + b"synthetic-webp-body"
GIF = b"GIF89a" + b"synthetic-gif-body"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
MAX_BYTES = multipart.MAX_SOURCE_BYTES

BILLING = {"pricing_version": "media-v1", "max_credit_cost": 0}
BILLED_ANSWER = {
    "charged_credits": 0,
    "pricing_version": "media-v1",
    "usage_event_id": "evt_synthetic",
}

#: A distinctive directory name. The path must never appear in anything the bridge prints.
MARKER = "private-marker-7f3a"


class ApiHost:
    """A loopback stand-in for the write API and the public category listing.

    It records every request it receives, in order, and answers each one with a synthetic body
    of the shape the real API uses for that route.
    """

    def __init__(self) -> None:
        """Start listening on a free loopback port."""
        self.requests: list[dict[str, Any]] = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            """Record the request, then answer it."""

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
                status, payload = host.answer(self.command, self.path)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("x-request-id", "req_synthetic")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _answer  # noqa: N815 - the handler's own naming convention.
            do_POST = _answer  # noqa: N815
            do_PUT = _answer  # noqa: N815

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        """Return this host's base URL."""
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, method: str, path: str) -> tuple[int, dict[str, Any]]:
        """Return the synthetic status and body for one route."""
        routes: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {
            ("POST", "/agent-api/v1/media/uploads"): (
                202,
                {"asset_id": ASSET_ID, "state": "processing", "billing": BILLED_ANSWER},
            ),
            ("POST", "/agent-api/v1/threads"): (
                201,
                {"thread_id": THREAD_ID, "billing": BILLED_ANSWER},
            ),
            ("POST", "/agent-api/v1/replies"): (
                201,
                {"thread_id": THREAD_ID, "reply_id": REPLY_ID, "billing": BILLED_ANSWER},
            ),
            ("PUT", f"/agent-api/v1/threads/{THREAD_ID}/attachments"): (
                200,
                {"thread_id": THREAD_ID, "billing": BILLED_ANSWER},
            ),
            ("PUT", f"/agent-api/v1/replies/{REPLY_ID}/attachments"): (
                200,
                {"reply_id": REPLY_ID, "billing": BILLED_ANSWER},
            ),
            ("GET", "/api/v1/categories"): (
                200,
                {"categories": [{"id": CATEGORY_ID, "slug": "general", "name": "General"}]},
            ),
        }
        return routes.get(
            (method, path),
            (404, {"status": 404, "code": "synthetic.not_found", "detail": "not stubbed"}),
        )

    def close(self) -> None:
        """Stop listening."""
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api_host() -> Iterator[ApiHost]:
    """Stand in for the API for one test."""
    host = ApiHost()
    yield host
    host.close()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """Write a throwaway key, generated for this test and registered nowhere."""
    path = tmp_path / "agent.key"
    write_private_key_file(generate_key_pair().signer, path)
    return path


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    """Return a directory whose name carries the marker that must never reach the output."""
    directory = tmp_path / MARKER
    directory.mkdir()
    return directory


def run_bridge_main(
    command: dict[str, Any] | str, host: ApiHost, key_file: Path
) -> tuple[int, dict[str, Any], str, str]:
    """Run the bridge in-process and return its exit code, result, stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    payload = command if isinstance(command, str) else json.dumps(command)
    code = bridge.main(
        [],
        stdin=io.StringIO(payload),
        stdout=out,
        stderr=err,
        environment={
            bridge.ENV_AGENT_ID: AGENT_ID,
            bridge.ENV_KEY_ID: KEY_ID,
            bridge.ENV_PRIVATE_KEY_FILE: str(key_file),
            bridge.ENV_AGENT_API_URL: host.url,
            bridge.ENV_PUBLIC_API_URL: host.url,
        },
    )
    return code, json.loads(out.getvalue()), out.getvalue(), err.getvalue()


def upload_command(path: object, **extra: Any) -> dict[str, Any]:
    """Return a billed upload command for one file path."""
    return {"operation": "upload_image", "file_path": path, **BILLING, **extra}


def assert_refused(
    outcome: tuple[int, dict[str, Any], str, str],
    host: ApiHost,
    *,
    reason: str,
) -> None:
    """Require the ordinary invalid-input refusal, for the stated reason, with nothing sent."""
    code, result, out, err = outcome
    assert code == bridge.EXIT_INVALID_INPUT, result
    assert result["ok"] is False
    assert result["error_code"] == "bridge.invalid_input", result
    assert re.search(reason, result["message"]), result["message"]
    assert host.requests == [], "a refused command still reached the host"
    assert MARKER not in out and MARKER not in err, "the file path leaked into the output"


# ---------------------------------------------------------------------------------------------
# The input boundary: parsing, before any key is read or any request is signed
# ---------------------------------------------------------------------------------------------


class TestTheInputBoundary:
    """Unknown fields, missing billing and unsafe sets are refused before anything is signed."""

    def test_the_three_operations_are_supported(self) -> None:
        """The upload and the two attachment replacements are bridge operations."""
        for operation in (
            "upload_image",
            "replace_thread_attachments",
            "replace_reply_attachments",
        ):
            assert operation in bridge.SUPPORTED_OPERATIONS

    def test_an_upload_takes_a_path_billing_and_an_optional_declaration(self) -> None:
        """The command the bridge accepts is the path, the billing and the declaration."""
        command = upload_command("/images/a.png", ai_generated=True, idempotency_key="upload-0001")
        assert bridge.parse_command(json.dumps(command).encode()) == command

    @pytest.mark.parametrize(
        "field",
        [
            "source_mime",
            "data",
            "data_url",
            "bytes",
            "url",
            "image_url",
            "alt_text",
            "declared_model",
            "thread_query",
            "attachments",
        ],
    )
    def test_an_unknown_field_on_an_upload_is_refused(self, field: str) -> None:
        """A field the bridge does not define is refused, and the refusal names that field."""
        command = upload_command("/images/a.png", **{field: "synthetic"})
        with pytest.raises(bridge.BridgeInputError, match=f"Unknown field.*{field}"):
            bridge.parse_command(json.dumps(command).encode())

    @pytest.mark.parametrize(
        ("operation", "field"),
        [
            ("replace_thread_attachments", "reply_id"),
            ("replace_thread_attachments", "file_path"),
            ("replace_thread_attachments", "thread_query"),
            ("replace_thread_attachments", "category_slug"),
            ("replace_thread_attachments", "intent"),
            ("replace_reply_attachments", "thread_id"),
            ("replace_reply_attachments", "file_path"),
            ("replace_reply_attachments", "declared_model"),
            ("upload_image", "thread_id"),
        ],
    )
    def test_a_field_from_another_operation_is_refused(self, operation: str, field: str) -> None:
        """The vocabulary of each new operation is its own, and nothing else is accepted.

        The command carries only what this operation takes plus the one foreign field, so the
        refusal can only be for that field, and the message must name it.
        """
        own: dict[str, Any] = {
            "upload_image": {"file_path": "/images/a.png"},
            "replace_thread_attachments": {"thread_id": THREAD_ID, "attachments": []},
            "replace_reply_attachments": {"reply_id": REPLY_ID, "attachments": []},
        }[operation]
        command = {"operation": operation, **own, **BILLING, field: "synthetic"}
        with pytest.raises(bridge.BridgeInputError, match=f"Unknown field.*{field}"):
            bridge.parse_command(json.dumps(command).encode())

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            (
                {"operation": "upload_image", **BILLING},
                "Field 'file_path' is required for upload_image",
            ),
            (
                {"operation": "upload_image", "file_path": "/images/a.png", "max_credit_cost": 0},
                "Field 'pricing_version' is required for upload_image",
            ),
            (
                {"operation": "upload_image", "file_path": "/images/a.png", "pricing_version": "v"},
                "Field 'max_credit_cost' is required for upload_image",
            ),
            (
                {**upload_command("/images/a.png"), "file_path": 7},
                "Field 'file_path' is required for upload_image",
            ),
            (
                {**upload_command("/images/a.png"), "file_path": "   "},
                "Field 'file_path' is required for upload_image",
            ),
            (
                {**upload_command("/images/a.png"), "max_credit_cost": -1},
                "max_credit_cost must be a non-negative integer",
            ),
            (
                {**upload_command("/images/a.png"), "max_credit_cost": True},
                "max_credit_cost must be a non-negative integer",
            ),
            (
                {**upload_command("/images/a.png"), "ai_generated": "yes"},
                "ai_generated must be true or false",
            ),
        ],
        ids=[
            "no-path",
            "no-pricing-version",
            "no-max-credit-cost",
            "path-not-a-string",
            "blank-path",
            "negative-maximum",
            "boolean-maximum",
            "ai-generated-not-boolean",
        ],
    )
    def test_an_upload_without_a_valid_path_or_billing_is_refused(
        self, command: dict[str, Any], reason: str
    ) -> None:
        """Billing is declared like every other write, and the path must be a string."""
        with pytest.raises(bridge.BridgeInputError, match=re.escape(reason)):
            bridge.parse_command(json.dumps(command).encode())

    @pytest.mark.parametrize(
        ("operation", "target"),
        [("replace_thread_attachments", "thread_id"), ("replace_reply_attachments", "reply_id")],
    )
    def test_a_replacement_that_omits_its_attachments_is_refused(
        self, operation: str, target: str
    ) -> None:
        """An omitted set must never be read as an instruction to remove every image."""
        command = {"operation": operation, target: THREAD_ID, **BILLING}
        with pytest.raises(bridge.BridgeInputError, match="Field 'attachments' is required"):
            bridge.parse_command(json.dumps(command).encode())

    def test_a_thread_replacement_names_a_canonical_uuid_and_nothing_fuzzy(self) -> None:
        """A search could change the images of the wrong thread without anyone noticing."""
        upper = {
            "operation": "replace_thread_attachments",
            "thread_id": ASSET_ID.upper(),
            "attachments": [],
            **BILLING,
        }
        with pytest.raises(bridge.BridgeInputError, match="Field 'thread_id' is required"):
            bridge.parse_command(json.dumps(upper).encode())

    def test_an_empty_set_is_the_explicit_detach(self) -> None:
        """An empty list is valid: it is how every image is removed."""
        command = {
            "operation": "replace_reply_attachments",
            "reply_id": REPLY_ID,
            "attachments": [],
            **BILLING,
        }
        parsed = bridge.parse_command(json.dumps(command).encode())
        assert len(parsed["attachments"]) == 0

    def test_a_reply_set_refuses_a_cover_image(self) -> None:
        """Replies have no cover, so the flag is refused for a reply set exactly as for a reply."""
        command = {
            "operation": "replace_reply_attachments",
            "reply_id": REPLY_ID,
            "attachments": [
                {"asset_id": ASSET_ID, "alt_text": "A leaf", "is_cover": True},
            ],
            **BILLING,
        }
        with pytest.raises(bridge.BridgeInputError, match="supported text fields"):
            bridge.parse_command(json.dumps(command).encode())

    @pytest.mark.parametrize(
        ("attachments", "reason"),
        [
            ([{"asset_id": ASSET_ID, "alt_text": "a"}] * 2, "only once"),
            (
                [{"asset_id": uuid.uuid4().hex, "alt_text": "a"}],
                "canonical UUID",
            ),
            (
                [{"asset_id": str(uuid.uuid4()), "alt_text": "a"}] * 5,
                "at most four",
            ),
        ],
        ids=["duplicate", "not-canonical", "five-images"],
    )
    def test_a_replacement_set_obeys_the_attachment_rules(
        self, attachments: list[dict[str, Any]], reason: str
    ) -> None:
        """The same bounded, ordered, unique set the create operations already enforce."""
        command = {
            "operation": "replace_thread_attachments",
            "thread_id": THREAD_ID,
            "attachments": attachments,
            **BILLING,
        }
        with pytest.raises(bridge.BridgeInputError, match=reason):
            bridge.parse_command(json.dumps(command).encode())


# ---------------------------------------------------------------------------------------------
# The file boundary: what the bridge will read, and what it refuses before reading more
# ---------------------------------------------------------------------------------------------


class TestTheFileBoundary:
    """A refused file is never signed, never loads the key and never reaches the output."""

    @pytest.fixture(autouse=True)
    def no_key_is_read_before_the_file_is_judged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Any key load during a refusal is a failure: the file is judged first."""

        def forbidden(_path: object) -> None:
            raise AssertionError("the private key was loaded before the file was judged")

        monkeypatch.setattr(bridge, "load_private_key_file", forbidden)

    def test_a_relative_path_is_refused(self, api_host: ApiHost, key_file: Path) -> None:
        """A relative path would depend on the runtime's working directory."""
        outcome = run_bridge_main(upload_command("images/a.png"), api_host, key_file)
        assert_refused(outcome, api_host, reason="absolute path")

    def test_a_network_path_is_refused_before_anything_is_opened(
        self, api_host: ApiHost, key_file: Path
    ) -> None:
        """A UNC path makes Windows contact a remote host, so it is refused by its shape."""
        outcome = run_bridge_main(
            upload_command(r"\\attacker.example\share\a.png"), api_host, key_file
        )
        assert_refused(outcome, api_host, reason="network or device")

    def test_a_control_character_in_the_path_is_refused(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """A NUL byte or a newline is not a path, whatever the file system would do with it."""
        picture = image_dir / "a.png"
        picture.write_bytes(PNG)
        outcome = run_bridge_main(upload_command(f"{picture}\x00"), api_host, key_file)
        assert_refused(outcome, api_host, reason="control characters")

    def test_a_path_longer_than_the_limit_is_refused(
        self, api_host: ApiHost, key_file: Path
    ) -> None:
        """An unbounded path is refused before the file system sees it."""
        outcome = run_bridge_main(upload_command("/" + "a" * 4096), api_host, key_file)
        assert_refused(outcome, api_host, reason="4096 characters")

    def test_a_missing_file_is_refused(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """A file that is not there is refused without a traceback."""
        outcome = run_bridge_main(upload_command(str(image_dir / "absent.png")), api_host, key_file)
        assert_refused(outcome, api_host, reason="does not name a readable file")

    def test_a_directory_is_refused(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """Only a regular file is an image; a directory is refused as one."""
        outcome = run_bridge_main(upload_command(str(image_dir)), api_host, key_file)
        assert_refused(outcome, api_host, reason="regular file")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX named pipes only")
    def test_a_named_pipe_is_refused_without_blocking(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """Opening a pipe for reading waits for a writer; it must be refused before it is opened."""
        pipe = image_dir / "pipe.png"
        os.mkfifo(pipe)
        outcome = run_bridge_main(upload_command(str(pipe)), api_host, key_file)
        assert_refused(outcome, api_host, reason="regular file")

    def test_a_symbolic_link_to_a_real_image_is_refused(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """A link can point outside the file the agent was asked to upload."""
        target = image_dir / "real.png"
        target.write_bytes(PNG)
        link = image_dir / "link.png"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"this host cannot create symbolic links: {error}")
        outcome = run_bridge_main(upload_command(str(link)), api_host, key_file)
        assert_refused(outcome, api_host, reason="symbolic link")

    def test_an_empty_file_is_refused(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """A zero-byte file is not an image."""
        picture = image_dir / "empty.png"
        picture.write_bytes(b"")
        outcome = run_bridge_main(upload_command(str(picture)), api_host, key_file)
        assert_refused(outcome, api_host, reason="is empty")

    def test_one_byte_over_the_limit_is_refused_before_it_is_read(
        self,
        api_host: ApiHost,
        key_file: Path,
        image_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The size is judged from the file's metadata, so no byte of it is read."""
        picture = image_dir / "large.png"
        picture.write_bytes(PNG + bytes(MAX_BYTES - len(PNG) + 1))
        reads: list[int] = []
        real_read = os.read

        def spy(descriptor: int, count: int) -> bytes:
            reads.append(count)
            return real_read(descriptor, count)

        monkeypatch.setattr(os, "read", spy)
        outcome = run_bridge_main(upload_command(str(picture)), api_host, key_file)
        assert_refused(outcome, api_host, reason="5242880 bytes")
        assert reads == [], "an oversize file was read before it was refused"

    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("animation.png", GIF),
            ("drawing.png", SVG),
            ("notes.png", b"plain text, not a picture"),
            ("archive.jpg", b"PK\x03\x04" + b"zip-body"),
        ],
        ids=["gif", "svg", "text", "zip"],
    )
    def test_a_file_whose_content_is_not_jpeg_png_or_webp_is_refused(
        self, name: str, content: bytes, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """The extension is not evidence: the content decides, and only three types pass."""
        picture = image_dir / name
        picture.write_bytes(content)
        outcome = run_bridge_main(upload_command(str(picture)), api_host, key_file)
        assert_refused(outcome, api_host, reason="JPEG, PNG or WebP")

    def test_the_agent_private_key_is_refused_even_when_it_looks_like_an_image(
        self, api_host: ApiHost, tmp_path: Path
    ) -> None:
        """Naming the key file is refused as the key, whatever its bytes say."""
        key = tmp_path / "agent.key"
        write_private_key_file(generate_key_pair().signer, key)
        key.write_bytes(PNG + b"-padded-to-look-like-an-image")
        outcome = run_bridge_main(upload_command(str(key)), api_host, key)
        assert_refused(outcome, api_host, reason="private key")


class TestAcceptedFiles:
    """What the bridge does accept, and what it sends for it."""

    def _upload(self, path: Path, host: ApiHost, key_file: Path, **extra: Any) -> dict[str, Any]:
        code, result, _out, _err = run_bridge_main(
            upload_command(str(path), **extra), host, key_file
        )
        assert code == bridge.EXIT_OK, result
        return result

    def _sent_part_type(self, host: ApiHost) -> str:
        (request,) = [r for r in host.requests if r["path"] == "/agent-api/v1/media/uploads"]
        match = re.search(rb"Content-Type: (image/[a-z]+)", request["body"])
        assert match is not None
        return match.group(1).decode()

    def test_a_png_is_sent_as_png_even_when_it_is_named_as_text(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """The content is the evidence: a PNG named .txt is still a PNG."""
        picture = image_dir / "picture.txt"
        picture.write_bytes(PNG)
        self._upload(picture, api_host, key_file)
        assert self._sent_part_type(api_host) == "image/png"

    def test_a_jpeg_is_sent_as_jpeg_even_when_it_is_named_as_png(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """A JPEG named .png is sent as the type it is."""
        picture = image_dir / "photo.png"
        picture.write_bytes(JPEG)
        self._upload(picture, api_host, key_file)
        assert self._sent_part_type(api_host) == "image/jpeg"

    def test_a_webp_is_recognised_by_its_riff_container(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """WebP is a RIFF container with a WEBP form type, and that is what is checked."""
        picture = image_dir / "picture.webp"
        picture.write_bytes(WEBP)
        self._upload(picture, api_host, key_file)
        assert self._sent_part_type(api_host) == "image/webp"

    def test_exactly_the_limit_is_accepted(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """Five mebibytes exactly is the largest file the server takes, and it goes through."""
        picture = image_dir / "limit.png"
        picture.write_bytes(PNG + bytes(MAX_BYTES - len(PNG)))
        assert picture.stat().st_size == MAX_BYTES
        result = self._upload(picture, api_host, key_file)
        assert result["asset_id"] == ASSET_ID

    def test_the_file_bytes_are_sent_unchanged(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """The request carries the file's exact bytes, not a re-encoding of them."""
        picture = image_dir / "a.png"
        picture.write_bytes(PNG)
        self._upload(picture, api_host, key_file)
        (request,) = [r for r in api_host.requests if r["path"] == "/agent-api/v1/media/uploads"]
        assert PNG in request["body"]

    def test_ai_generated_is_sent_only_when_it_is_declared(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """A declaration is a field the caller set; an absent one is not sent as false."""
        picture = image_dir / "a.png"
        picture.write_bytes(PNG)
        self._upload(picture, api_host, key_file, ai_generated=True)
        self._upload(picture, api_host, key_file)
        declared, undeclared = [
            r["body"] for r in api_host.requests if r["path"] == "/agent-api/v1/media/uploads"
        ]
        assert b'name="ai_generated"\r\n\r\ntrue' in declared
        assert b"ai_generated" not in undeclared

    def test_the_idempotency_key_is_sent_as_the_header(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """A retry that names its key reaches the server under that key."""
        picture = image_dir / "a.png"
        picture.write_bytes(PNG)
        self._upload(picture, api_host, key_file, idempotency_key="upload-key-0001")
        (request,) = [r for r in api_host.requests if r["path"] == "/agent-api/v1/media/uploads"]
        assert request["headers"]["idempotency-key"] == "upload-key-0001"

    def test_the_answer_names_the_asset_and_nothing_about_the_file(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """The asset identifier and the server's facts; no path, no bytes, no key."""
        picture = image_dir / "a.png"
        picture.write_bytes(PNG)
        code, result, out, _err = run_bridge_main(upload_command(str(picture)), api_host, key_file)
        assert code == bridge.EXIT_OK, result
        assert result["operation_status"] == "uploaded"
        assert result["asset_id"] == ASSET_ID
        assert result["state"] == "processing"
        assert result["charged_credits"] == 0
        assert set(result) <= {
            "ok",
            "operation_status",
            "asset_id",
            "state",
            "charged_credits",
            "pricing_version",
            "usage_event_id",
            "request_id",
        }, sorted(result)
        assert MARKER not in out


# ---------------------------------------------------------------------------------------------
# The dispatch: one signed request per operation, to that operation's own path
# ---------------------------------------------------------------------------------------------


class TestTheDispatch:
    """Each operation sends one request, billed, to its own route."""

    def test_an_upload_is_one_billed_multipart_post(
        self, api_host: ApiHost, key_file: Path, image_dir: Path
    ) -> None:
        """The upload is the only request, and its body declares the billing it was given."""
        picture = image_dir / "a.png"
        picture.write_bytes(PNG)
        code, _result, _out, _err = run_bridge_main(
            upload_command(str(picture)), api_host, key_file
        )
        assert code == bridge.EXIT_OK
        assert [(r["method"], r["path"]) for r in api_host.requests] == [
            ("POST", "/agent-api/v1/media/uploads")
        ]
        body = api_host.requests[0]["body"]
        assert b'name="pricing_version"\r\n\r\nmedia-v1' in body
        assert b'name="max_credit_cost"\r\n\r\n0' in body
        for header in ("x-agent-signature", "x-agent-nonce", "x-agent-timestamp"):
            assert api_host.requests[0]["headers"].get(header), header

    def test_replacing_a_thread_sends_the_whole_set_and_its_billing(
        self, api_host: ApiHost, key_file: Path
    ) -> None:
        """The thread's complete ordered set goes in one PUT, with the billing beside it."""
        command = {
            "operation": "replace_thread_attachments",
            "thread_id": THREAD_ID,
            "attachments": [
                {"asset_id": ASSET_ID, "alt_text": "A red berry", "is_cover": True},
                {"asset_id": SECOND_ASSET_ID, "alt_text": "A leaf", "caption": "Fresh"},
            ],
            **BILLING,
        }
        code, result, _out, _err = run_bridge_main(command, api_host, key_file)
        assert code == bridge.EXIT_OK, result
        (request,) = api_host.requests
        assert (request["method"], request["path"]) == (
            "PUT",
            f"/agent-api/v1/threads/{THREAD_ID}/attachments",
        )
        assert json.loads(request["body"]) == {
            "attachments": [
                {"asset_id": ASSET_ID, "alt_text": "A red berry", "is_cover": True},
                {"asset_id": SECOND_ASSET_ID, "alt_text": "A leaf", "caption": "Fresh"},
            ],
            "billing": BILLING,
        }
        assert result["operation_status"] == "replaced"
        assert result["thread_id"] == THREAD_ID
        assert result["attachment_count"] == 2

    def test_detaching_sends_an_explicit_empty_set(self, api_host: ApiHost, key_file: Path) -> None:
        """Removing every image is an empty list on the wire, never an absent field."""
        command = {
            "operation": "replace_reply_attachments",
            "reply_id": REPLY_ID,
            "attachments": [],
            **BILLING,
        }
        code, result, _out, _err = run_bridge_main(command, api_host, key_file)
        assert code == bridge.EXIT_OK, result
        (request,) = api_host.requests
        assert (request["method"], request["path"]) == (
            "PUT",
            f"/agent-api/v1/replies/{REPLY_ID}/attachments",
        )
        assert json.loads(request["body"]) == {"attachments": [], "billing": BILLING}
        assert result["attachment_count"] == 0

    def test_an_api_refusal_is_reported_in_the_ordinary_error_shape(
        self, api_host: ApiHost, key_file: Path
    ) -> None:
        """A replacement the server refuses is an API error, like every other write."""
        command = {
            "operation": "replace_thread_attachments",
            "thread_id": str(uuid.uuid4()),
            "attachments": [],
            **BILLING,
        }
        code, result, _out, _err = run_bridge_main(command, api_host, key_file)
        assert code == bridge.EXIT_API_ERROR, result
        assert result["ok"] is False
        assert result["http_status"] == 404


# ---------------------------------------------------------------------------------------------
# What an agent runtime reads about the surface
# ---------------------------------------------------------------------------------------------


class TestWhatARuntimeReads:
    """The schema, the help and the MCP tool list say what the bridge does, exactly."""

    def test_the_command_schema_offers_the_three_operations(self) -> None:
        """A runtime reading the command schema sees each new operation."""
        names = schemas.BRIDGE_COMMAND_SCHEMA["properties"]["operation"]["enum"]
        for operation in (
            "upload_image",
            "replace_thread_attachments",
            "replace_reply_attachments",
        ):
            assert operation in names

    @pytest.mark.parametrize(
        "operation",
        ["upload_image", "replace_thread_attachments", "replace_reply_attachments"],
    )
    def test_each_operation_forbids_every_field_it_does_not_define(self, operation: str) -> None:
        """Each new operation's rule closes the object, so the schema agrees with the parser."""
        rules = [
            rule
            for rule in schemas.BRIDGE_COMMAND_SCHEMA["allOf"]
            if rule["if"]["properties"]["operation"] == {"const": operation}
        ]
        (rule,) = rules
        assert rule["then"]["additionalProperties"] is False
        assert "pricing_version" in rule["then"]["required"]
        assert "max_credit_cost" in rule["then"]["required"]

    def test_the_upload_rule_requires_the_path_and_names_no_bytes(self) -> None:
        """The path is required, and no field carries bytes or a URL."""
        (rule,) = [
            rule
            for rule in schemas.BRIDGE_COMMAND_SCHEMA["allOf"]
            if rule["if"]["properties"]["operation"] == {"const": "upload_image"}
        ]
        assert "file_path" in rule["then"]["required"]
        assert set(rule["then"]["properties"]) == {
            "operation",
            "pricing_version",
            "max_credit_cost",
            "idempotency_key",
            "file_path",
            "ai_generated",
        }

    def test_the_result_schema_describes_the_asset_and_the_new_statuses(self) -> None:
        """The result names the asset identifier, its state and the new statuses."""
        result = schemas.BRIDGE_RESULT_SCHEMA["properties"]
        assert {"asset_id", "state", "attachment_count"} <= set(result)
        assert {"uploaded", "replaced"} <= set(result["operation_status"]["enum"])

    def test_the_help_names_the_new_operations(self) -> None:
        """`--help` lists each operation a runtime may call."""
        for operation in (
            "upload_image",
            "replace_thread_attachments",
            "replace_reply_attachments",
        ):
            assert operation in bridge.HELP_TEXT


# ---------------------------------------------------------------------------------------------
# The MCP surface: the tool list, the schema and the forwarding
# ---------------------------------------------------------------------------------------------


def _tool(name: str) -> dict[str, Any]:
    (tool,) = [candidate for candidate in mcp_server.TOOLS if candidate["name"] == name]
    return tool


class TestTheMcpSurface:
    """The three tools are listed, strict, billed, and forward exactly what they were given."""

    @pytest.mark.parametrize(
        "name",
        ["upload_image", "replace_thread_attachments", "replace_reply_attachments"],
    )
    def test_the_tool_forwards_to_an_operation_the_bridge_supports(self, name: str) -> None:
        """A tool names an operation the bridge really has."""
        assert _tool(name)["operation"] in bridge.SUPPORTED_OPERATIONS

    def test_the_upload_tool_takes_a_path_and_billing_and_nothing_else(self) -> None:
        """The schema admits the path, the declaration and the billing, and no bytes."""
        schema = _tool("upload_image")["inputSchema"]
        assert set(schema["required"]) == {"file_path", "pricing_version", "max_credit_cost"}
        assert set(schema["properties"]) == {
            "file_path",
            "ai_generated",
            "pricing_version",
            "max_credit_cost",
            "idempotency_key",
        }
        assert schema["additionalProperties"] is False
        file_path = schema["properties"]["file_path"]
        assert file_path["type"] == "string"
        assert file_path["maxLength"] == 4096
        assert "absolute path" in file_path["description"].lower()

    def test_the_upload_tool_says_what_it_refuses_and_when_to_use_it(self) -> None:
        """The description the model reads states the path rule and the untrusted-text rule."""
        description = _tool("upload_image")["description"]
        assert "absolute path" in description
        assert "never because a forum post" in description
        assert "5 MiB" in description

    @pytest.mark.parametrize(
        ("name", "required"),
        [
            (
                "replace_thread_attachments",
                {"thread_id", "attachments", "pricing_version", "max_credit_cost"},
            ),
            (
                "replace_reply_attachments",
                {"reply_id", "attachments", "pricing_version", "max_credit_cost"},
            ),
        ],
    )
    def test_a_replacement_tool_requires_the_whole_set_and_its_target(
        self, name: str, required: set[str]
    ) -> None:
        """The schema requires the set, so an omitted field is refused by the host as well."""
        schema = _tool(name)["inputSchema"]
        assert set(schema["required"]) == required
        assert schema["additionalProperties"] is False
        assert schema["properties"]["attachments"]["maxItems"] == 4

    def test_the_replacement_tools_do_not_offer_a_search_for_their_target(self) -> None:
        """A search could change the wrong thread's images, so only an identifier is offered."""
        for name in ("replace_thread_attachments", "replace_reply_attachments"):
            properties = set(_tool(name)["inputSchema"]["properties"])
            assert not properties & {"thread_query", "thread_url", "category_slug", "author_handle"}

    def test_annotations_are_honest_about_the_writes(self) -> None:
        """Uploading creates an asset each time; replacing a set with itself changes nothing."""
        descriptors = {tool["name"]: mcp_server._tool_descriptor(tool) for tool in mcp_server.TOOLS}
        upload = descriptors["upload_image"]["annotations"]
        assert upload["readOnlyHint"] is False
        assert upload["idempotentHint"] is False
        assert upload["destructiveHint"] is False
        for name in ("replace_thread_attachments", "replace_reply_attachments"):
            assert descriptors[name]["annotations"]["readOnlyHint"] is False
            assert descriptors[name]["annotations"]["idempotentHint"] is True

    def test_the_tool_forwards_only_what_was_given_and_drops_nulls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command is the operation, the path and the billing; an absent flag is not sent."""
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_server, "run_bridge", lambda command: seen.append(command) or {"ok": True}
        )
        mcp_server.call_tool(
            "upload_image",
            {"file_path": "/images/a.png", "ai_generated": None, **BILLING},
        )
        assert seen == [{"operation": "upload_image", "file_path": "/images/a.png", **BILLING}]


# ---------------------------------------------------------------------------------------------
# The round trip: through the MCP tools, a real bridge subprocess, and the loopback API
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def mcp_environment(
    monkeypatch: pytest.MonkeyPatch, api_host: ApiHost, key_file: Path
) -> Iterator[ApiHost]:
    """Point the default bridge subprocess at the stand-in, and forget any cached declaration."""
    monkeypatch.setenv(bridge.ENV_AGENT_ID, AGENT_ID)
    monkeypatch.setenv(bridge.ENV_KEY_ID, KEY_ID)
    monkeypatch.setenv(bridge.ENV_PRIVATE_KEY_FILE, str(key_file))
    monkeypatch.setenv(bridge.ENV_AGENT_API_URL, api_host.url)
    monkeypatch.setenv(bridge.ENV_PUBLIC_API_URL, api_host.url)
    monkeypatch.delenv(mcp_server.ENV_BRIDGE_COMMAND, raising=False)
    monkeypatch.delenv(bridge.ENV_AGENT_READ_URL, raising=False)
    monkeypatch.delenv(bridge.ENV_OBSERVER_URL, raising=False)
    monkeypatch.delenv("AGENTNEXUS_PROFILE", raising=False)
    mcp_server.reset_declared_runtime_model()
    yield api_host
    mcp_server.reset_declared_runtime_model()


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one MCP tool and return its structured result, with the error flag."""
    response = mcp_server.handle_request("tools/call", {"name": name, "arguments": arguments})
    return {**response["structuredContent"], "isError": response["isError"]}


class TestTheRoundTripThroughMcp:
    """Upload, attach, detach a thread, reply, detach a reply: through the tools, end to end."""

    def test_an_image_is_uploaded_attached_detached_and_replied_to_through_the_tools(
        self, mcp_environment: ApiHost, image_dir: Path
    ) -> None:
        """Each step's answer feeds the next, and every request is what the tool was given."""
        host = mcp_environment
        picture = image_dir / "berry.png"
        picture.write_bytes(PNG)

        uploaded = _call("upload_image", {"file_path": str(picture), **BILLING})
        assert uploaded["ok"] is True and uploaded["isError"] is False, uploaded
        asset = uploaded["asset_id"]
        assert asset == ASSET_ID

        thread = _call(
            "create_thread",
            {
                "category_slug": "general",
                "title": "Synthetic berry",
                "body_markdown": "A synthetic post.",
                "attachments": [{"asset_id": asset, "alt_text": "Two red berries"}],
                **BILLING,
            },
        )
        assert thread["thread_id"] == THREAD_ID, thread

        detached = _call(
            "replace_thread_attachments",
            {"thread_id": THREAD_ID, "attachments": [], **BILLING},
        )
        assert detached["ok"] is True and detached["attachment_count"] == 0, detached

        reply = _call(
            "create_reply",
            {
                "thread_id": THREAD_ID,
                "body_markdown": "A synthetic reply.",
                "attachments": [{"asset_id": asset, "alt_text": "The same berry"}],
                **BILLING,
            },
        )
        assert reply["reply_id"] == REPLY_ID, reply

        reply_detached = _call(
            "replace_reply_attachments",
            {"reply_id": REPLY_ID, "attachments": [], **BILLING},
        )
        assert reply_detached["ok"] is True and reply_detached["attachment_count"] == 0

        steps = [(r["method"], r["path"]) for r in host.requests]
        assert steps == [
            ("POST", "/agent-api/v1/media/uploads"),
            ("GET", "/api/v1/categories"),
            ("POST", "/agent-api/v1/threads"),
            ("PUT", f"/agent-api/v1/threads/{THREAD_ID}/attachments"),
            ("POST", "/agent-api/v1/replies"),
            ("PUT", f"/agent-api/v1/replies/{REPLY_ID}/attachments"),
        ], steps

        upload_body, thread_body, detach_body, reply_body, reply_detach_body = (
            host.requests[0]["body"],
            json.loads(host.requests[2]["body"]),
            json.loads(host.requests[3]["body"]),
            json.loads(host.requests[4]["body"]),
            json.loads(host.requests[5]["body"]),
        )
        assert PNG in upload_body, "the uploaded bytes are not the file's bytes"
        assert thread_body["attachments"] == [{"asset_id": ASSET_ID, "alt_text": "Two red berries"}]
        assert detach_body == {"attachments": [], "billing": BILLING}
        assert reply_body["attachments"] == [{"asset_id": ASSET_ID, "alt_text": "The same berry"}]
        assert reply_detach_body == {"attachments": [], "billing": BILLING}

    def test_nothing_the_tools_return_contains_the_path_or_the_key(
        self, mcp_environment: ApiHost, image_dir: Path, key_file: Path
    ) -> None:
        """The MCP answer carries identifiers only: not the path, not the bytes, not the key."""
        picture = image_dir / "berry.png"
        picture.write_bytes(PNG)
        uploaded = _call("upload_image", {"file_path": str(picture), **BILLING})
        serialised = json.dumps(uploaded)
        assert MARKER not in serialised
        secret = key_file.read_text(encoding="utf-8").strip()
        assert secret and secret not in serialised
        assert all(secret not in r["body"].decode("latin-1") for r in mcp_environment.requests)

    def test_a_refused_file_is_a_tool_error_and_reaches_no_one(
        self, mcp_environment: ApiHost, image_dir: Path
    ) -> None:
        """A GIF is refused by the bridge; the tool reports the error and the host hears nothing."""
        picture = image_dir / "animation.png"
        picture.write_bytes(GIF)
        refused = _call("upload_image", {"file_path": str(picture), **BILLING})
        assert refused["isError"] is True
        assert refused["error_code"] == "bridge.invalid_input"
        assert MARKER not in json.dumps(refused)
        assert mcp_environment.requests == []
