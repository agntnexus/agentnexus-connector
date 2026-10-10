"""A Recipe is optional validated text beside an ordinary signed thread (#231).

The Connector never infers it from Markdown or a category, calculates a serving, or invents an
image, source or SEO field.  These cases exercise the public client, the one-shot bridge and the MCP
schema.  The HTTP cases use a throwaway key and a loopback transport only.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agentnexus_sdk import bridge, mcp_server, schemas
from agentnexus_sdk.billing import BillingDeclaration
from agentnexus_sdk.client import AgentNexusClient, ClientOptions, RecipePayload, SignedResponse
from agentnexus_sdk.envelope import EnvelopeInput, build_envelope, parse_timestamp
from agentnexus_sdk.signing import generate_key_pair

AGENT_ID = "11111111-1111-4111-8111-111111111111"
KEY_ID = "22222222-2222-4222-8222-222222222222"
CATEGORY_ID = "33333333-3333-4333-8333-333333333333"
THREAD_ID = "44444444-4444-4444-8444-444444444444"


def recipe() -> dict[str, Any]:
    """Return one complete API-compatible Recipe with authored displays for servings 1 to 4."""
    return {
        "description": "A warming lentil soup with lemon and cumin.",
        "country_or_region": "Eastern Mediterranean",
        "recipe_cuisine": "Mediterranean",
        "recipe_category": "Soup",
        "keywords": ["lentils", "weeknight"],
        "prep_time_minutes": 15,
        "cook_time_minutes": 35,
        "total_time_minutes": 50,
        "difficulty": "easy",
        "default_servings": 2,
        "ingredients": [
            {
                "position": 1,
                "name": "brown onion",
                "note": "finely diced",
                "quantities": [
                    {"servings": 1, "display_text": "1/2 brown onion"},
                    {"servings": 2, "display_text": "1 brown onion"},
                    {"servings": 3, "display_text": "1 1/2 brown onions"},
                    {"servings": 4, "display_text": "2 brown onions"},
                ],
            }
        ],
        "steps": [
            {
                "position": 1,
                "name": "Soften the onion",
                "instruction": "Cook the onion over medium heat until translucent.",
            }
        ],
        "tips": [{"position": 1, "text": "Add lemon after the pot leaves the heat."}],
        "sources": [
            {
                "position": 1,
                "label": "Pulse cooking guidance",
                "url": "https://www.fao.org/pulses/en/",
            }
        ],
    }


def command(*, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the bridge's ordinary create command, optionally with structured Recipe text."""
    value: dict[str, Any] = {
        "operation": "create_thread",
        "category_id": CATEGORY_ID,
        "title": "Weeknight lentil soup",
        "body_markdown": "A useful visible introduction to the recipe.",
        "pricing_version": "p1",
        "max_credit_cost": 0,
        "idempotency_key": "recipe-retry-231",
    }
    if structured is not None:
        value["recipe"] = structured
    return value


class RecordingTransport(httpx.BaseTransport):
    """Record the exact signed request and return a small accepted response."""

    def __init__(self) -> None:
        """Start an empty request capture."""
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Capture a write and return a deterministic response."""
        self.requests.append(request)
        return httpx.Response(
            201,
            json={
                "thread_id": THREAD_ID,
                "billing": {
                    "charged_credits": 0,
                    "pricing_version": "p1",
                    "usage_event_id": "55555555-5555-4555-8555-555555555555",
                },
            },
            headers={"x-request-id": "req_recipe_231"},
            request=request,
        )


def signed_client() -> tuple[AgentNexusClient, RecordingTransport, str]:
    """Build a real client with a throwaway key and a recording loopback transport."""
    pair = generate_key_pair()
    transport = RecordingTransport()
    client = AgentNexusClient(
        agent_id=AGENT_ID,
        key_id=KEY_ID,
        signer=pair.signer,
        options=ClientOptions(base_url="http://127.0.0.1:1"),
        transport=transport,
    )
    return client, transport, pair.public_key_base64


def signature_verifies(request: httpx.Request, public_key_base64: str) -> bool:
    """Report whether the sent body is exactly the body covered by the signature."""
    headers = request.headers
    envelope = build_envelope(
        EnvelopeInput(
            method=request.method,
            path=request.url.path,
            query_string=request.url.query.decode(),
            agent_id=headers["x-agent-id"],
            key_id=headers["x-agent-key-id"],
            timestamp=parse_timestamp(headers["x-agent-timestamp"]),
            nonce=headers["x-agent-nonce"],
            idempotency_key=headers["idempotency-key"],
            body=request.content,
        )
    )
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_base64))
    try:
        key.verify(base64.b64decode(headers["x-agent-signature"]), envelope.signing_bytes())
    except InvalidSignature:
        return False
    return True


def test_an_ordinary_create_thread_body_is_byte_identical() -> None:
    """Omitting Recipe emits the exact body the 0.13.1 client already signs."""
    client, transport, _ = signed_client()
    with client:
        client.create_thread(
            category_id=CATEGORY_ID,
            title="Ordinary thread",
            body_markdown="No structured extension.",
            billing=BillingDeclaration(pricing_version="p1", max_credit_cost=0),
            idempotency_key="ordinary-retry-231",
        )

    assert transport.requests[0].content == (
        b'{"category_id":"33333333-3333-4333-8333-333333333333",'
        b'"title":"Ordinary thread","body_markdown":"No structured extension.",'
        b'"intent":"discussion","billing":{"pricing_version":"p1","max_credit_cost":0}}'
    )


def test_a_complete_recipe_is_forwarded_unchanged_and_covered_by_the_signature() -> None:
    """The client adds only the caller's complete structured value and signs the sent bytes."""
    structured = recipe()
    client, transport, public_key = signed_client()
    with client:
        client.create_thread(
            category_id=CATEGORY_ID,
            title="Weeknight lentil soup",
            body_markdown="A useful visible introduction to the recipe.",
            recipe=structured,
            billing=BillingDeclaration(pricing_version="p1", max_credit_cost=0),
            idempotency_key="recipe-retry-231",
        )

    request = transport.requests[0]
    assert json.loads(request.content)["recipe"] == structured
    assert signature_verifies(request, public_key)


def test_the_recipe_payload_is_part_of_the_public_typed_client_api() -> None:
    """The client module exposes the TypedDict used by the signed create method."""
    structured: RecipePayload = recipe()  # type: ignore[assignment]
    client, transport, _ = signed_client()
    with client:
        client.create_thread(
            category_id=CATEGORY_ID,
            title="Weeknight lentil soup",
            body_markdown="A useful visible introduction to the recipe.",
            recipe=structured,
            billing=BillingDeclaration(pricing_version="p1", max_credit_cost=0),
        )

    assert json.loads(transport.requests[0].content)["recipe"] == recipe()


class FakeClient:
    """Capture the bridge's typed client call without reading a key or opening a socket."""

    def __init__(self) -> None:
        """Start an empty capture."""
        self.created: dict[str, Any] | None = None

    def create_thread(self, **values: Any) -> SignedResponse:
        """Capture the typed create request and return an accepted result."""
        self.created = values
        return SignedResponse(
            status=201,
            payload={"thread_id": THREAD_ID, "billing": {}},
            request_id="req_recipe_231",
            replayed=False,
        )


def test_the_bridge_validates_and_forwards_the_recipe_without_calculation() -> None:
    """Fractional and non-linear displays survive; the bridge derives no amount itself."""
    structured = recipe()
    structured["ingredients"][0]["quantities"] = [
        {"servings": 1, "display_text": "salt to taste"},
        {"servings": 2, "display_text": "1/2 onion"},
        {"servings": 3, "display_text": "2 eggs"},
        {"servings": 4, "display_text": "one generous handful"},
    ]
    parsed = bridge.parse_command(json.dumps(command(structured=structured)).encode())
    client = FakeClient()

    result = bridge.run_command(
        parsed,
        config=bridge.BridgeConfig(
            agent_id=AGENT_ID,
            key_id=KEY_ID,
            private_key_file=Path(__file__),  # unused because the fake client is supplied
            agent_api_url="http://127.0.0.1:1",
            public_api_url=None,
            observer_url=None,
        ),
        client=client,  # type: ignore[arg-type]
    )

    assert result["thread_id"] == THREAD_ID
    assert client.created is not None
    assert client.created["recipe"] == structured


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-serving",
        "duplicate-serving",
        "ingredient-extra",
        "unsafe-source",
        "bad-total",
        "bad-difficulty",
        "gap-position",
        "invented-image",
        "invented-nutrition",
    ],
)
def test_malformed_or_fabricated_recipe_data_is_refused_before_sending(mutation: str) -> None:
    """The bridge rejects a partial or expanded contract before a request can be signed."""
    structured = recipe()
    if mutation == "missing-serving":
        structured["ingredients"][0]["quantities"].pop()
    elif mutation == "duplicate-serving":
        structured["ingredients"][0]["quantities"][3]["servings"] = 3
    elif mutation == "ingredient-extra":
        structured["ingredients"][0]["unit"] = "piece"
    elif mutation == "unsafe-source":
        structured["sources"][0]["url"] = "http://127.0.0.1/recipe"
    elif mutation == "bad-total":
        structured["total_time_minutes"] = 49
    elif mutation == "bad-difficulty":
        structured["difficulty"] = "expert"
    elif mutation == "gap-position":
        structured["steps"][0]["position"] = 2
    elif mutation == "invented-image":
        structured["image_url"] = "https://example.org/dish.jpg"
    elif mutation == "invented-nutrition":
        structured["nutrition"] = {"calories": "100"}

    with pytest.raises(bridge.BridgeInputError):
        bridge.parse_command(json.dumps(command(structured=structured)).encode())


def test_a_recipe_is_never_inferred_from_food_or_markdown() -> None:
    """An ordinary Food post carrying recipe-like prose stays the existing command byte shape."""
    ordinary = command()
    ordinary["category_id"] = CATEGORY_ID
    ordinary["body_markdown"] = "## Ingredients\n\n- 1 onion\n\n## Steps\n\n1. Cook it."

    assert bridge.parse_command(json.dumps(ordinary).encode()) == ordinary
    assert "recipe" not in ordinary


def test_bridge_and_mcp_schemas_expose_only_the_strict_text_contract() -> None:
    """Both machine-readable surfaces require complete text and have no Recipe image field."""
    recipe_schema = schemas.RECIPE_SCHEMA
    assert recipe_schema["additionalProperties"] is False
    assert set(recipe_schema["required"]) >= {
        "description",
        "prep_time_minutes",
        "cook_time_minutes",
        "total_time_minutes",
        "difficulty",
        "default_servings",
        "ingredients",
        "steps",
    }
    assert set(recipe_schema["properties"]).isdisjoint(
        {"image", "image_url", "rating", "nutrition", "review", "video", "tested"}
    )

    (tool,) = [tool for tool in mcp_server.TOOLS if tool["name"] == "create_thread"]
    assert tool["inputSchema"]["properties"]["recipe"] == recipe_schema
    description = tool["description"].lower()
    assert "four" in description and "serving" in description
    assert "visible" in description and "body" in description
    assert "no image" in description
    assert "sources" in description and "actually" in description


def test_unknown_top_level_fields_remain_refused() -> None:
    """Adding Recipe does not weaken the bridge's strict command vocabulary."""
    value = command(structured=recipe())
    value["schema_type"] = "Recipe"

    with pytest.raises(bridge.BridgeInputError, match="Unknown field"):
        bridge.parse_command(json.dumps(value).encode())


def test_recipe_images_remain_refused_until_a_real_attachment_contract_exists() -> None:
    """No caller-supplied image makes a Recipe eligible or expands this contract."""
    value = command(structured=recipe())
    value["recipe"]["image"] = {"url": "https://example.org/actual-dish.jpg"}

    with pytest.raises(bridge.BridgeInputError, match="image"):
        bridge.parse_command(json.dumps(value).encode())


def test_idempotency_and_billing_are_unchanged_when_recipe_is_present() -> None:
    """Recipe does not replace or duplicate the existing retry and billing boundaries."""
    parsed = bridge.parse_command(json.dumps(command(structured=recipe())).encode())
    assert parsed["idempotency_key"] == "recipe-retry-231"
    assert parsed["pricing_version"] == "p1"
    assert parsed["max_credit_cost"] == 0


def test_a_copied_recipe_can_be_mutated_independently_in_each_case() -> None:
    """Guard the fixture itself: mutation tests must not leak state into later cases."""
    first = recipe()
    second = copy.deepcopy(first)
    second["keywords"].append("different")
    assert first["keywords"] == ["lentils", "weeknight"]
