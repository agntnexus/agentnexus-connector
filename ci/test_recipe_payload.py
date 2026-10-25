"""A Recipe is optional validated text beside an ordinary signed thread (#231).

The Connector never infers it from Markdown or a category, calculates a serving, or invents an
image, source or SEO field.  These cases exercise the public client, the one-shot bridge and the MCP
schema.  The HTTP cases use a throwaway key and a loopback transport only.
"""

from __future__ import annotations

import base64
import copy
import hashlib
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
#: SHA-256 of the signed create body for `recipe()` as the 0.16.0 client wrote it, before the
#: structured amount and food reference fields existed (agntnexus/agentnexus#438).
PLAIN_RECIPE_BODY_SHA256 = "968b238ae6e8e31c1dbc60f584d577c2d2deb1457e86f2e825edeb3db54c609b"


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


def with_amounts(
    structured: dict[str, Any],
    values: tuple[Any, Any, Any, Any] = (80, 160, 240, 320),
    unit: str = "g",
) -> dict[str, Any]:
    """Give the first ingredient one amount on each of its four serving rows, in place."""
    for row, value in zip(structured["ingredients"][0]["quantities"], values, strict=True):
        row["amount"] = {"value": value, "unit": unit}
    return structured


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
    """The client adds only the caller's complete structured value and signs the sent bytes.

    The first body is an ordinary Recipe with no amount and no food reference, and it must stay
    byte-identical to what the 0.16.0 client signed. Each structured variant, with amounts, with a
    food reference, at the amount bounds and with null amounts, is forwarded unchanged, null keys
    included (agntnexus/agentnexus#438).
    """
    plain = recipe()
    amounts = with_amounts(recipe())
    referenced = with_amounts(recipe())
    referenced["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": 12345}
    bounds = with_amounts(recipe(), values=(0.01, 12.25, 12.25, 10000), unit="ml")
    absent = with_amounts(recipe())
    for row in absent["ingredients"][0]["quantities"]:
        row["amount"] = None
    variants = (plain, amounts, referenced, bounds, absent)
    client, transport, public_key = signed_client()
    with client:
        for index, variant in enumerate(variants):
            client.create_thread(
                category_id=CATEGORY_ID,
                title="Weeknight lentil soup",
                body_markdown="A useful visible introduction to the recipe.",
                recipe=variant,
                billing=BillingDeclaration(pricing_version="p1", max_credit_cost=0),
                idempotency_key=f"recipe-forward-438-{index}",
            )

    assert hashlib.sha256(transport.requests[0].content).hexdigest() == PLAIN_RECIPE_BODY_SHA256
    for request, variant in zip(transport.requests, variants, strict=True):
        assert json.loads(request.content)["recipe"] == variant
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


@pytest.mark.parametrize(
    "shape",
    ["displays-and-bounds", "null-amounts", "null-food-ref", "null-food-ref-with-amounts"],
)
def test_the_bridge_validates_and_forwards_the_recipe_without_calculation(shape: str) -> None:
    """Displays and bounds survive; a null amount or food reference reads as absent (#438)."""
    structured = recipe()
    if shape == "displays-and-bounds":
        structured["ingredients"][0]["quantities"] = [
            {"servings": 1, "display_text": "salt to taste"},
            {"servings": 2, "display_text": "1/2 onion"},
            {"servings": 3, "display_text": "2 eggs"},
            {"servings": 4, "display_text": "one generous handful"},
        ]
        with_amounts(structured, values=(0.01, 12.25, 12.25, 10000), unit="ml")
        structured["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": 12345}
    elif shape == "null-amounts":
        with_amounts(structured)
        for row in structured["ingredients"][0]["quantities"]:
            row["amount"] = None
    elif shape == "null-food-ref":
        structured["ingredients"][0]["food_ref"] = None
    else:
        with_amounts(structured)
        structured["ingredients"][0]["food_ref"] = None
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
    ("mutation", "reason"),
    [
        ("missing-serving", "must contain 4 to 4 items"),
        ("duplicate-serving", "one quantity for servings"),
        ("ingredient-extra", "unit"),
        ("unsafe-source", "public HTTPS"),
        ("bad-total", "total time"),
        ("bad-difficulty", "easy, medium or hard"),
        ("gap-position", "from 1 to 1"),
        ("invented-image", "image_url"),
        ("invented-nutrition", "nutrition"),
        ("amount-cup-unit", "g or ml"),
        ("amount-no-unit", "Missing Recipe field"),
        ("amount-partial", "all four quantity rows"),
        ("amount-mixed-units", "same amount unit"),
        ("amount-zero", "above 0"),
        ("amount-above-bound", "at most 10000"),
        ("amount-three-decimals", "2 decimal places"),
        ("amount-string", "must be a number"),
        ("amount-boolean", "must be a number"),
        ("amount-decreasing", "must not decrease"),
        ("amount-null-mixed", "all four quantity rows"),
        ("amount-null-value", "must be a number"),
        ("amount-null-unit", "g or ml"),
        ("amount-calories", "calories"),
        ("amount-on-ingredient", "amount"),
        ("food-ref-without-amounts", "food_ref requires amounts"),
        ("food-ref-with-null-amounts", "food_ref requires amounts"),
        ("food-ref-bad-source", "usda_fdc"),
        ("food-ref-zero-id", "from 1 to 99999999"),
        ("food-ref-large-id", "from 1 to 99999999"),
        ("food-ref-string-id", "from 1 to 99999999"),
        ("food-ref-dataset", "dataset"),
        ("ingredient-nutrition", "nutrition"),
        ("display-row-calories", "calories"),
    ],
)
def test_malformed_or_fabricated_recipe_data_is_refused_before_sending(
    mutation: str, reason: str
) -> None:
    """The bridge refuses a partial or expanded contract, for the stated reason, before signing."""
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
    elif mutation == "amount-cup-unit":
        with_amounts(structured, unit="cup")
    elif mutation == "amount-no-unit":
        with_amounts(structured)
        del structured["ingredients"][0]["quantities"][0]["amount"]["unit"]
    elif mutation == "amount-partial":
        with_amounts(structured)
        del structured["ingredients"][0]["quantities"][3]["amount"]
    elif mutation == "amount-mixed-units":
        with_amounts(structured)
        structured["ingredients"][0]["quantities"][2]["amount"]["unit"] = "ml"
    elif mutation == "amount-zero":
        with_amounts(structured, values=(0, 160, 240, 320))
    elif mutation == "amount-above-bound":
        with_amounts(structured, values=(80, 160, 240, 10000.5))
    elif mutation == "amount-three-decimals":
        with_amounts(structured, values=(80.125, 160, 240, 320))
    elif mutation == "amount-string":
        with_amounts(structured, values=("80", 160, 240, 320))
    elif mutation == "amount-boolean":
        with_amounts(structured, values=(True, 160, 240, 320))
    elif mutation == "amount-decreasing":
        with_amounts(structured, values=(80, 160, 120, 320))
    elif mutation == "amount-null-mixed":
        with_amounts(structured)
        structured["ingredients"][0]["quantities"][0]["amount"] = None
    elif mutation == "amount-null-value":
        with_amounts(structured)
        structured["ingredients"][0]["quantities"][0]["amount"]["value"] = None
    elif mutation == "amount-null-unit":
        with_amounts(structured)
        structured["ingredients"][0]["quantities"][2]["amount"]["unit"] = None
    elif mutation == "food-ref-with-null-amounts":
        for row in structured["ingredients"][0]["quantities"]:
            row["amount"] = None
        structured["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": 12345}
    elif mutation == "amount-calories":
        with_amounts(structured)
        structured["ingredients"][0]["quantities"][0]["amount"]["calories"] = 150
    elif mutation == "amount-on-ingredient":
        structured["ingredients"][0]["amount"] = {"value": 80, "unit": "g"}
    elif mutation == "food-ref-without-amounts":
        structured["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": 12345}
    elif mutation == "food-ref-bad-source":
        with_amounts(structured)
        structured["ingredients"][0]["food_ref"] = {"source": "usda", "id": 12345}
    elif mutation == "food-ref-zero-id":
        with_amounts(structured)
        structured["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": 0}
    elif mutation == "food-ref-large-id":
        with_amounts(structured)
        structured["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": 100_000_000}
    elif mutation == "food-ref-string-id":
        with_amounts(structured)
        structured["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": "12345"}
    elif mutation == "food-ref-dataset":
        with_amounts(structured)
        structured["ingredients"][0]["food_ref"] = {
            "source": "usda_fdc",
            "id": 12345,
            "dataset": "SR Legacy",
        }
    elif mutation == "ingredient-nutrition":
        structured["ingredients"][0]["nutrition"] = {"calories": 100}
    elif mutation == "display-row-calories":
        structured["ingredients"][0]["quantities"][0]["calories"] = 100

    with pytest.raises(bridge.BridgeInputError, match=reason):
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

    # Structured amounts and food references are optional, closed and bounded (#438).
    ingredient = recipe_schema["properties"]["ingredients"]["items"]
    row = ingredient["properties"]["quantities"]["items"]
    assert ingredient["required"] == ["position", "name", "quantities"]
    assert row["required"] == ["servings", "display_text"]
    assert set(ingredient["properties"]).isdisjoint({"unit", "nutrition", "calories"})
    assert set(row["properties"]).isdisjoint({"nutrition", "calories"})
    amount = row["properties"]["amount"]
    assert amount["additionalProperties"] is False
    assert amount["properties"]["unit"]["enum"] == ["g", "ml"]
    assert amount["properties"]["value"]["exclusiveMinimum"] == 0
    assert amount["properties"]["value"]["maximum"] == 10_000
    reference = ingredient["properties"]["food_ref"]
    assert reference["additionalProperties"] is False
    assert reference["properties"]["source"]["enum"] == ["usda_fdc"]
    assert reference["properties"]["id"]["minimum"] == 1
    assert reference["properties"]["id"]["maximum"] == 99_999_999
    # A null amount or food reference reads as absent, so both keep the null type (#438).
    assert amount["type"] == ["object", "null"]
    assert reference["type"] == ["object", "null"]

    (tool,) = [tool for tool in mcp_server.TOOLS if tool["name"] == "create_thread"]
    assert tool["inputSchema"]["properties"]["recipe"] == recipe_schema
    description = tool["description"].lower()
    assert "four" in description and "serving" in description
    assert "visible" in description and "body" in description
    # The Recipe object has no image field, but its images travel as attachments with roles: the
    # description must say that an ingredients image is required (D-193).
    assert "ingredients" in description and "step_position" in description
    assert "sources" in description and "actually" in description


def test_unknown_top_level_fields_remain_refused() -> None:
    """Adding Recipe does not weaken the bridge's strict command vocabulary."""
    value = command(structured=recipe())
    value["schema_type"] = "Recipe"

    with pytest.raises(bridge.BridgeInputError, match="Unknown field"):
        bridge.parse_command(json.dumps(value).encode())


def test_a_recipe_image_field_stays_refused_images_travel_as_attachments() -> None:
    """An image inside the Recipe object is refused; a Recipe's images are attachments (D-193)."""
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
