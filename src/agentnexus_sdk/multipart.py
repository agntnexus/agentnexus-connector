"""Stable, bounded multipart encoding for the signed generic raster upload contract."""

from __future__ import annotations

import hashlib
from typing import Final

from agentnexus_sdk.billing import BillingDeclaration
from agentnexus_sdk.envelope import ProtocolError

MAX_SOURCE_BYTES: Final = 5 * 1024 * 1024
SUPPORTED_SOURCE_MIMES: Final = frozenset({"image/jpeg", "image/png", "image/webp"})


def build_upload_body(
    source: bytes,
    *,
    source_mime: str,
    billing: BillingDeclaration,
    ai_generated: bool | None = None,
) -> tuple[bytes, str]:
    """Build one deterministic multipart body and exact Content-Type header value."""
    if not isinstance(source, bytes) or not 1 <= len(source) <= MAX_SOURCE_BYTES:
        raise ProtocolError("The upload source must be 1 through 5 MiB of bytes.")
    if source_mime not in SUPPORTED_SOURCE_MIMES:
        raise ProtocolError("Only JPEG, PNG, or WebP images can be uploaded.")
    if (
        not billing.pricing_version
        or len(billing.pricing_version) > 64
        or any(
            ord(character) < 32 or ord(character) == 127 for character in billing.pricing_version
        )
    ):
        raise ProtocolError("pricing_version must be a bounded control-free value.")
    if type(billing.max_credit_cost) is not int or billing.max_credit_cost < 0:
        raise ProtocolError("max_credit_cost must be a non-negative integer.")
    if ai_generated is not None and type(ai_generated) is not bool:
        raise ProtocolError("ai_generated must be true, false, or omitted.")

    fields = [
        ("pricing_version", billing.pricing_version),
        ("max_credit_cost", str(billing.max_credit_cost)),
    ]
    if ai_generated is not None:
        fields.append(("ai_generated", "true" if ai_generated else "false"))
    metadata = "\0".join(value for _, value in fields).encode("utf-8")
    seed = hashlib.sha256(
        b"agentnexus-media-v1\0" + source_mime.encode("ascii") + b"\0" + metadata + source
    )
    counter = 0
    while True:
        if counter > 32:
            raise ProtocolError("A deterministic multipart boundary could not be selected.")
        suffix = (
            seed.digest()
            if counter == 0
            else hashlib.sha256(seed.digest() + str(counter).encode()).digest()
        )
        boundary = f"agentnexus-{suffix.hex()[:32]}"
        delimiter = boundary.encode("ascii")
        if delimiter not in source and delimiter not in metadata:
            break
        counter += 1

    chunks = [
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="upload"\r\n'
        f"Content-Type: {source_mime}\r\n\r\n".encode("ascii"),
        source,
        b"\r\n",
    ]
    for name, value in fields:
        chunks.extend(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
