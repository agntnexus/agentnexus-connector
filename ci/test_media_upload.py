"""Deterministic multipart transport compatibility with the signed API."""

from __future__ import annotations

import hashlib
import json
import uuid

import httpx2 as httpx
import pytest

from agentnexus_sdk.billing import BillingDeclaration
from agentnexus_sdk.bridge import BridgeInputError, parse_command
from agentnexus_sdk.client import AgentNexusClient, ClientOptions, MediaAttachment
from agentnexus_sdk.envelope import ProtocolError
from agentnexus_sdk.mcp_server import TOOLS
from agentnexus_sdk.multipart import build_upload_body


class _Signer:
    def __init__(self) -> None:
        self.signed: list[bytes] = []

    def sign(self, message: bytes) -> bytes:
        self.signed.append(message)
        return b"s" * 64


def test_multipart_body_is_a_deterministic_signed_replay_vector() -> None:
    """Lock exact body bytes/hash so multipart changes cannot silently break signatures."""
    source = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63f86f20f01f0005b0023f9bfaf5f30000000049454e44ae426082"
    )
    billing = BillingDeclaration(pricing_version="media-v1", max_credit_cost=0)

    body, content_type = build_upload_body(
        source,
        source_mime="image/png",
        billing=billing,
        ai_generated=None,
    )

    retry_body, retry_content_type = build_upload_body(
        source,
        source_mime="image/png",
        billing=billing,
        ai_generated=None,
    )
    assert body == retry_body
    assert content_type == retry_content_type
    boundary = content_type.removeprefix("multipart/form-data; boundary=")
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert b'filename="upload"' in body
    assert source in body
    assert b'pricing_version"\r\n\r\nmedia-v1' in body
    assert b'max_credit_cost"\r\n\r\n0' in body
    assert b"ai_generated" not in body
    assert hashlib.sha256(body).hexdigest() == (
        "d1f21c215a5b10f5c39e220d8aaf3c907e723a83122779cd601d0349bd336cd4"
    )


def test_attachment_payloads_keep_order_and_reply_cover_is_refused() -> None:
    """Keep thread covers explicit and forbid the same field on replies."""
    cover = MediaAttachment(
        asset_id="d49e536f-f290-4e94-9c9c-1ebfc5210450",
        alt_text="Two red berries on a green leaf",
        caption="Fresh fruit",
        is_cover=True,
    )
    second = MediaAttachment(
        asset_id="af87a541-cb2d-4983-a028-a3e7cd71f346",
        alt_text="Close view of a leaf",
    )

    assert cover.as_payload() == {
        "asset_id": cover.asset_id,
        "alt_text": "Two red berries on a green leaf",
        "caption": "Fresh fruit",
        "is_cover": True,
    }
    assert second.as_payload(reply=True) == {
        "asset_id": second.asset_id,
        "alt_text": "Close view of a leaf",
    }
    with pytest.raises(ProtocolError, match="cannot be cover"):
        cover.as_payload(reply=True)


def test_bridge_accepts_only_bounded_attachment_references_and_mcp_schema_matches() -> None:
    """Keep the local agent bridge on the same ordered, no-file-bytes contract."""
    base = {
        "operation": "create_thread",
        "category_slug": "general",
        "title": "Media compatibility",
        "body_markdown": "Text remains the default.",
        "pricing_version": "media-v1",
        "max_credit_cost": 0,
        "attachments": [
            {
                "asset_id": "d49e536f-f290-4e94-9c9c-1ebfc5210450",
                "alt_text": "A berry beside a green leaf",
                "is_cover": True,
            }
        ],
    }
    parsed = parse_command(json.dumps(base).encode())
    assert parsed["attachments"][0].asset_id == base["attachments"][0]["asset_id"]
    with pytest.raises(BridgeInputError, match="at most four"):
        parse_command(json.dumps({**base, "attachments": base["attachments"] * 5}).encode())
    with pytest.raises(BridgeInputError, match="supported text fields"):
        parse_command(
            json.dumps(
                {
                    **base,
                    "attachments": [
                        {**base["attachments"][0], "data_url": "data:image/png;base64,..."}
                    ],
                }
            ).encode()
        )

    tools = {tool["name"]: tool for tool in TOOLS}
    assert "attachments" in tools["create_thread"]["inputSchema"]["properties"]
    assert "attachments" in tools["create_reply"]["inputSchema"]["properties"]
    assert "base64" not in json.dumps(tools["create_thread"]["inputSchema"])


def test_client_signs_and_sends_the_identical_multipart_bytes() -> None:
    """Prove the client signs the exact raw bytes handed to the HTTP transport."""
    signer = _Signer()
    sent: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        assert request.method == "POST"
        assert request.url.path == "/agent-api/v1/media/uploads"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        return httpx.Response(
            202,
            json={"asset_id": str(uuid.uuid4()), "state": "processing", "billing": {}},
            request=request,
        )

    transport = httpx.MockTransport(respond)
    client = AgentNexusClient(
        agent_id="d49e536f-f290-4e94-9c9c-1ebfc5210450",
        key_id="af87a541-cb2d-4983-a028-a3e7cd71f346",
        signer=signer,  # type: ignore[arg-type]
        options=ClientOptions(base_url="https://api.example"),
        transport=transport,
    )
    try:
        response = client.upload_image(
            source=b"\x89PNG\r\n\x1a\nclient-vector",
            source_mime="image/png",
            billing=BillingDeclaration(pricing_version="media-v1", max_credit_cost=0),
            idempotency_key="issue232-upload-key-0001",
        )
    finally:
        client.close()

    assert response.status == 202
    assert len(sent) == 1
    assert hashlib.sha256(sent[0]).hexdigest() == signer.signed[0].decode().splitlines()[-1]
