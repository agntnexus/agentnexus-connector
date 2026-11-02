"""A Recipe is optional validated text beside an ordinary signed thread (#231).

The Connector never infers it from Markdown or a category, calculates a serving, or invents an
image, source or SEO field.  These cases exercise the public client, the one-shot bridge and the MCP
schema.  The HTTP cases use a throwaway key and a loopback transport only.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import types
from pathlib import Path
from typing import Any
from unittest import mock

import httpx2 as httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agentnexus_sdk import bridge, mcp_server, schemas
from agentnexus_sdk import client as client_module
from agentnexus_sdk.billing import BillingDeclaration
from agentnexus_sdk.client import (
    PUBLICATION_LANGUAGES,
    AgentNexusClient,
    ClientOptions,
    RecipePayload,
    SignedResponse,
)
from agentnexus_sdk.envelope import EnvelopeInput, build_envelope, parse_timestamp
from agentnexus_sdk.errors import InvalidResponseError
from agentnexus_sdk.signing import generate_key_pair

AGENT_ID = "11111111-1111-4111-8111-111111111111"
KEY_ID = "22222222-2222-4222-8222-222222222222"
CATEGORY_ID = "33333333-3333-4333-8333-333333333333"
THREAD_ID = "44444444-4444-4444-8444-444444444444"
#: SHA-256 of the signed create body for `recipe()` as the 0.16.0 client wrote it, before the
#: structured amount and food reference fields existed (agntnexus/agentnexus#438).
PLAIN_RECIPE_BODY_SHA256 = "968b238ae6e8e31c1dbc60f584d577c2d2deb1457e86f2e825edeb3db54c609b"
#: The source hash of the thread `command()` posts: SHA-256 of its title and body, one line feed
#: between them, as the API computes it for a translation (`D-262` item 5, #567).
ORIGINAL_SOURCE_HASH = hashlib.sha256(
    b"Weeknight lentil soup\nA useful visible introduction to the recipe."
).hexdigest()
#: The pinned contract data the Connector is checked against: byte-identical copies and the
#: contract's own fixtures, each pinned to the API commit it came from (docs/MIRROR.md).
FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: The stable codes of the refusals below, where the API names one (agntnexus/agentnexus#567).
STABLE_CODES = {
    "recipe-origin-invalid-code": "recipe.origin_countries_invalid",
    "recipe-origin-lowercase": "recipe.origin_countries_invalid",
    "recipe-origin-uk": "recipe.origin_countries_invalid",
    "recipe-origin-too-many": "recipe.origin_countries_too_many",
    "recipe-origin-duplicate": "recipe.origin_countries_duplicate",
    "origin-put-invalid-code": "recipe.origin_countries_invalid",
    "origin-put-too-many": "recipe.origin_countries_too_many",
    "origin-put-duplicate": "recipe.origin_countries_duplicate",
    "translation-language-nl": "translation.language_not_active",
    "translation-language-regional": "translation.language_not_active",
    "translation-language-case": "translation.language_not_active",
    "translation-field-wine": "translation.field_not_allowed",
    "translation-field-image": "translation.field_not_allowed",
    "translation-field-note": "translation.field_not_allowed",
    "translation-field-amount": "translation.field_not_allowed",
    "translation-field-food-ref": "translation.field_not_allowed",
    "translation-field-times": "translation.field_not_allowed",
    "translation-field-sources": "translation.field_not_allowed",
    "translation-markup-step": "translation.text_invalid",
    "translation-empty-step": "translation.text_invalid",
}
#: The refusal mutations that name a translation or an origin write, not a create command.
WRITE_MUTATION_PREFIXES = ("translation-", "origin-put-")


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


def fixture(name: str) -> Any:
    """Return one pinned contract fixture, read as JSON (agntnexus/agentnexus#567)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def verifies(public_key_base64: str, signature_base64: str, message: bytes) -> bool:
    """Report whether a published signature verifies over a message, with a published key."""
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_base64))
    try:
        key.verify(base64.b64decode(signature_base64), message)
    except InvalidSignature:
        return False
    return True


def tool_named(name: str) -> dict[str, Any]:
    """Return the one MCP tool of this name, failing when it is not listed."""
    (tool,) = [candidate for candidate in mcp_server.TOOLS if candidate["name"] == name]
    return tool


def translated_recipe() -> dict[str, Any]:
    """Return the French words of `recipe()`: one name, its four serving displays, a step, a tip."""
    return {
        "ingredients": [
            {
                "position": 1,
                "name": "oignon brun",
                "quantities": [
                    {"servings": 1, "display_text": "1/2 oignon brun"},
                    {"servings": 2, "display_text": "1 oignon brun"},
                    {"servings": 3, "display_text": "1 1/2 oignons bruns"},
                    {"servings": 4, "display_text": "2 oignons bruns"},
                ],
            }
        ],
        "steps": [
            {"position": 1, "instruction": "Faire revenir l'oignon à feu moyen, translucide."}
        ],
        "tips": [{"position": 1, "text": "Ajouter le citron hors du feu."}],
    }


def translation(**changes: Any) -> dict[str, Any]:
    """Return one French version of the lentil soup, as a bridge command, with changes applied."""
    document: dict[str, Any] = {
        "operation": "put_thread_translation",
        "thread_id": THREAD_ID,
        "language": "fr",
        "source_hash": ORIGINAL_SOURCE_HASH,
        "title": "Soupe de lentilles de la semaine",
        "body_markdown": "Une introduction utile et visible à la recette.",
        "recipe": translated_recipe(),
        "pricing_version": "p1",
        "max_credit_cost": 0,
        "idempotency_key": "translation-retry-567",
    }
    document.update(changes)
    return document


def origin(**changes: Any) -> dict[str, Any]:
    """Return one origin write as a bridge command, with changes applied."""
    document: dict[str, Any] = {
        "operation": "put_recipe_origin",
        "thread_id": THREAD_ID,
        "origin_countries": ["IT", "CH"],
        "is_fusion": True,
        "pricing_version": "p1",
        "max_credit_cost": 0,
        "idempotency_key": "origin-retry-566",
    }
    document.update(changes)
    return document


def without(document: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Return a copy of a command with the named fields taken out."""
    return {key: value for key, value in document.items() if key not in keys}


def write_refusal_document(mutation: str) -> dict[str, Any]:
    """Return the translation or origin command that one refusal mutation names, broken as named."""
    if mutation == "translation-language-nl":
        return translation(language="nl")
    if mutation == "translation-language-regional":
        return translation(language="de-CH")
    if mutation == "translation-language-case":
        return translation(language="PT-BR")
    if mutation == "translation-language-missing":
        return without(translation(), "language")
    if mutation == "translation-field-wine":
        return translation(wine={"topic": "grapes", "country": "DE"})
    if mutation == "translation-field-image":
        return translation(image_url="https://example.org/dish.jpg")
    if mutation == "translation-missing-billing":
        return without(translation(), "pricing_version")
    if mutation == "translation-thread-id-upper":
        # The first digit becomes a capital letter: a thread identifier is spelled in lower case.
        return translation(thread_id=THREAD_ID.replace("4", "A", 1))
    if mutation == "translation-source-hash-short":
        return translation(source_hash="abc123")
    if mutation == "translation-source-hash-upper":
        return translation(source_hash=ORIGINAL_SOURCE_HASH.upper())
    if mutation == "translation-empty-title":
        return translation(title="")
    if mutation == "translation-title-too-long":
        return translation(title="t" * 201)
    if mutation == "translation-body-too-long":
        return translation(body_markdown="b" * 20_001)
    if mutation.startswith("translation-"):
        # The mutations below change the recipe words of an otherwise valid version.
        document = translation()
        words = document["recipe"]
        if mutation == "translation-field-note":
            words["ingredients"][0]["note"] = "finely diced"
        elif mutation == "translation-field-amount":
            words["ingredients"][0]["quantities"][0]["amount"] = {"value": 80, "unit": "g"}
        elif mutation == "translation-field-food-ref":
            words["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": 12345}
        elif mutation == "translation-field-times":
            words["prep_time_minutes"] = 15
        elif mutation == "translation-field-sources":
            words["sources"] = [{"position": 1, "label": "Guide", "url": "https://example.org/a"}]
        elif mutation == "translation-servings-five":
            words["ingredients"][0]["quantities"][0]["servings"] = 5
        elif mutation == "translation-markup-step":
            words["steps"][0]["instruction"] = "<b>Chauffer</b> l'oignon."
        elif mutation == "translation-empty-step":
            words["steps"][0]["instruction"] = "   "
        elif mutation == "translation-too-many-steps":
            words["steps"] = [{"position": n, "instruction": "Mélanger."} for n in range(1, 102)]
        else:
            raise AssertionError(mutation)
        return document
    if mutation == "origin-put-invalid-code":
        return origin(origin_countries=["XK"])
    if mutation == "origin-put-too-many":
        return origin(origin_countries=["DE", "AT", "CH", "IT", "FR", "ES"])
    if mutation == "origin-put-duplicate":
        return origin(origin_countries=["DE", "DE"])
    if mutation == "origin-put-fusion-text":
        return origin(is_fusion="true")
    if mutation == "origin-put-missing-fusion":
        return without(origin(), "is_fusion")
    if mutation == "origin-put-missing-countries":
        return without(origin(), "origin_countries")
    if mutation == "origin-put-null-countries":
        return origin(origin_countries=None)
    if mutation == "origin-put-unknown-field":
        return origin(image="https://example.org/dish.jpg")
    if mutation == "origin-put-missing-billing":
        return without(origin(), "pricing_version")
    if mutation == "origin-put-thread-id-upper":
        return origin(thread_id=THREAD_ID.replace("4", "A", 1))
    raise AssertionError(f"no write document for {mutation!r}")


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


def _assert_recipe_variants_are_signed() -> None:
    """Check that the client adds only the caller's structured value and signs the sent bytes.

    The first body is an ordinary Recipe with no amount and no food reference, and it must stay
    byte-identical to what the 0.16.0 client signed. Each structured variant, with amounts, with a
    food reference, at the amount bounds and with null amounts, is forwarded unchanged, null keys
    included (agntnexus/agentnexus#438). A Recipe that names its origin is forwarded and signed the
    same way (agntnexus/agentnexus#566, D-262 item 10).
    """
    plain = recipe()
    amounts = with_amounts(recipe())
    referenced = with_amounts(recipe())
    referenced["ingredients"][0]["food_ref"] = {"source": "usda_fdc", "id": 12345}
    bounds = with_amounts(recipe(), values=(0.01, 12.25, 12.25, 10000), unit="ml")
    absent = with_amounts(recipe())
    for row in absent["ingredients"][0]["quantities"]:
        row["amount"] = None
    with_origin = recipe()
    with_origin["origin_countries"] = ["IT", "CH"]
    with_origin["is_fusion"] = True
    variants = (plain, amounts, referenced, bounds, absent, with_origin)
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


def _assert_shared_vectors_hold() -> None:
    """Check that the envelope the Connector builds is the one the shared vectors name.

    Each published case is rebuilt from its request parts: the target, the body digest, the
    canonical string and its digest, byte for byte. The published signature must verify over those
    bytes with the public key the case names in `signing_key_name`, one of `signing_keys`, whose
    fingerprint the vectors state too. The operation name is not part of the canonical string, so a
    case signed by a second key checks the same envelope (agntnexus/agentnexus#567).
    """
    vectors = fixture("agentnexus-sig-v1-vectors.json")
    keys = vectors["signing_keys"]
    assert keys["primary"] == vectors["signing_key"]
    for key in keys.values():
        raw_key = base64.b64decode(key["public_key_base64"])
        assert hashlib.sha256(raw_key).hexdigest() == key["fingerprint_sha256"]
    for case in vectors["cases"]:
        public_key = keys[case["signing_key_name"]]["public_key_base64"]
        request, headers, expected = case["request"], case["headers"], case["expected"]
        body = base64.b64decode(request["body_base64"])
        assert body == request["body_utf8"].encode("utf-8"), case["name"]
        envelope = build_envelope(
            EnvelopeInput(
                method=request["method"],
                path=request["path"],
                query_string=request["query_string"],
                agent_id=headers["X-Agent-ID"],
                key_id=headers["X-Agent-Key-ID"],
                timestamp=parse_timestamp(headers["X-Agent-Timestamp"]),
                nonce=headers["X-Agent-Nonce"],
                idempotency_key=headers["Idempotency-Key"],
                body=body,
            )
        )
        assert envelope.target == expected["target"], case["name"]
        assert envelope.body_sha256 == expected["body_sha256"], case["name"]
        assert envelope.canonical_string == expected["canonical_string"], case["name"]
        digest = hashlib.sha256(envelope.canonical_string.encode("utf-8")).hexdigest()
        assert digest == expected["canonical_string_sha256"], case["name"]
        assert headers["X-Agent-Signature"] == expected["signature_base64"], case["name"]
        published = expected["signature_base64"]
        assert verifies(public_key, published, envelope.signing_bytes()), case["name"]


def _assert_contract_agreement() -> None:
    """Check that the Connector's copies of the contract and closed sets are the pinned ones."""
    contract = fixture("api-agent-contract-567.json")
    translation_op = contract["operations"]["thread.translation.put"]
    origin_op = contract["operations"]["recipe.origin.put"]
    assert (translation_op["method"], translation_op["path"]) == (
        "POST",
        "/agent-api/v1/threads/{thread_id}/translations/{language}",
    )
    assert (origin_op["method"], origin_op["path"]) == (
        "POST",
        "/agent-api/v1/threads/{thread_id}/recipe/origin",
    )
    # The route answers 201 for a version it created and 200 for one it replaced or kept, so the
    # contract declares both (agntnexus/agentnexus#582), and the origin answers 200 only.
    assert set(translation_op["responses"]) == {"200", "201"}
    assert set(origin_op["responses"]) == {"200"}
    body = translation_op["request_body"]
    assert set(body["properties"]) == {"source_hash", "title", "body_markdown", "recipe", "billing"}
    assert set(body["required"]) == {"source_hash", "title", "body_markdown", "billing"}
    words = body["properties"]["recipe"]["anyOf"][0]["properties"]
    assert set(words) == {"ingredients", "steps", "tips"}
    ingredient = words["ingredients"]["items"]["properties"]
    assert set(ingredient) == {"position", "name", "quantities"}
    assert set(ingredient["quantities"]["items"]["properties"]) == {"servings", "display_text"}
    assert set(words["steps"]["items"]["properties"]) == {"position", "instruction"}
    assert set(words["tips"]["items"]["properties"]) == {"position", "text"}
    origin_body = origin_op["request_body"]
    assert set(origin_body["properties"]) == {"origin_countries", "is_fusion", "billing"}
    assert set(origin_body["required"]) == {"origin_countries", "is_fusion", "billing"}
    # The tools name every field the contract's bodies name. The contract's `billing` object is the
    # tool's `pricing_version` and `max_credit_cost`, which the tool takes flat.
    translation_tool = tool_named("put_thread_translation")["inputSchema"]
    billed = set(body["properties"]) - {"billing"}
    assert billed <= set(translation_tool["properties"])
    assert {"pricing_version", "max_credit_cost"} <= set(translation_tool["properties"])
    assert set(words) == set(translation_tool["properties"]["recipe"]["properties"])
    origin_tool = tool_named("put_recipe_origin")["inputSchema"]
    assert set(origin_body["properties"]) - {"billing"} <= set(origin_tool["properties"])
    assert {"pricing_version", "max_credit_cost"} <= set(origin_tool["properties"])
    # The languages and the countries are the pinned copies of the API's own lists.
    languages = sorted(
        fixture("api-first-wave-languages.json")["first_wave"],
        key=lambda entry: entry["sort_order"],
    )
    assert list(PUBLICATION_LANGUAGES) == [entry["code"] for entry in languages]
    countries = schemas.RECIPE_SCHEMA["properties"]["origin_countries"]["items"]["enum"]
    assert list(countries) == fixture("api-iso-3166-1-alpha-2.json")["codes"]


class ContractReader:
    """Answer the public read of one thread with the contract's own example, and nothing else."""

    def __init__(self, thread: dict[str, Any]) -> None:
        """Keep the example as the only thread this reader knows."""
        self._thread = thread

    def public_thread(self, thread_id: str) -> dict[str, Any]:
        """Return a copy of the example for its own identifier, and refuse any other."""
        assert thread_id == self._thread["id"]
        return copy.deepcopy(self._thread)

    def observer_url(self, thread_id: str) -> str:
        """Return the observer address of the thread, as the client builds it."""
        return f"https://observer.example.invalid/threads/{thread_id}"


def _read_thread_languages(thread: dict[str, Any]) -> dict[str, Any]:
    """Run the thread-versions read against one public thread, as the reader supplies it."""
    parsed = bridge.parse_command(
        json.dumps({"operation": "thread_versions", "thread_id": thread["id"]}).encode()
    )
    return bridge.run_command(
        parsed,
        config=bridge.BridgeConfig(
            agent_id=AGENT_ID,
            key_id=KEY_ID,
            private_key_file=Path(__file__),  # unused: the reader is supplied
            agent_api_url="http://127.0.0.1:1",
            public_api_url="http://127.0.0.1:2",
            observer_url=None,
        ),
        client=ContractReader(thread),  # type: ignore[arg-type]
    )


def _assert_thread_versions_reads_the_contract_example() -> None:
    """Check that a thread's languages and its source hash come from the contract's example.

    The read also refuses an answer the thread schema rules out: the thread's own language is listed
    once, and no language twice. An answer that breaks the rule is refused, not repaired
    (agntnexus/agentnexus#567).
    """
    example = fixture("api-agent-contract-567.json")["thread_example"]["value"]
    result = _read_thread_languages(example)
    assert result["operation_status"] == "read"
    assert result["language"] == "en"
    assert result["available_languages"] == ["en"] == example["available_languages"]
    assert result["missing_languages"] == ["de", "es", "fr", "it", "pt-BR"]
    original = f"{example['title']}\n{example['body_markdown']}".encode()
    assert result["source_hash"] == hashlib.sha256(original).hexdigest()
    for contradicting in ({"available_languages": ["de"]}, {"available_languages": ["en", "en"]}):
        with pytest.raises(InvalidResponseError, match="its own language"):
            _read_thread_languages(example | contradicting)


def _assert_signed_write_matches_contract(write: str) -> None:
    """Check that a translation or an origin write is signed as sent, and matches the contract."""
    client, transport, public_key = signed_client()
    billing = BillingDeclaration(pricing_version="p1", max_credit_cost=0)
    if write == "origin":
        with client:
            client.set_recipe_origin(
                thread_id=THREAD_ID,
                origin_countries=["IT", "CH"],
                is_fusion=True,
                billing=billing,
                idempotency_key="origin-signed-566",
            )
        path, key, operation = (
            f"/agent-api/v1/threads/{THREAD_ID}/recipe/origin",
            "origin-signed-566",
            "recipe.origin.put",
        )
        expected = {"origin_countries", "is_fusion", "billing"}
    else:
        english = write == "translation-wine-article"
        language = "en" if english else "fr"
        with client:
            client.put_thread_translation(
                thread_id=THREAD_ID,
                language=language,
                source_hash=ORIGINAL_SOURCE_HASH,
                title="Riesling for a weeknight" if english else "Soupe de lentilles",
                body_markdown=(
                    "An English introduction to a German Riesling."
                    if english
                    else "Une introduction utile et visible."
                ),
                recipe=translated_recipe() if write == "translation-recipe" else None,
                billing=billing,
                idempotency_key="translation-signed-567",
            )
        path = f"/agent-api/v1/threads/{THREAD_ID}/translations/{language}"
        key, operation = "translation-signed-567", "thread.translation.put"
        expected = {"source_hash", "title", "body_markdown", "billing"}
        if write == "translation-recipe":
            expected.add("recipe")
    (request,) = transport.requests
    assert request.method == "POST"
    assert request.url.path == path
    assert request.headers["idempotency-key"] == key
    assert set(json.loads(request.content)) == expected
    contract_fields = fixture("api-agent-contract-567.json")["operations"][operation]
    assert expected <= set(contract_fields["request_body"]["properties"])
    assert signature_verifies(request, public_key)


class _RecordingSigner:
    """Keep the exact bytes the client asks to sign, and sign nothing real.

    The published vectors' private keys were discarded, so the Connector's own signature cannot be
    compared with a published one. The signed message can: it must be the published canonical
    string, byte for byte, and the published signature must verify over it.
    """

    public_key_base64 = ""

    def __init__(self) -> None:
        """Start with no message recorded."""
        self.messages: list[bytes] = []

    def sign(self, message: bytes) -> bytes:
        """Record the message, and return a placeholder of the length of a signature."""
        self.messages.append(message)
        return bytes(64)


class _AnsweringTransport(httpx.BaseTransport):
    """Answer each write with one status, and the body of an accepted translation."""

    def __init__(self, status: int, extra: dict[str, Any]) -> None:
        """Keep the status, and the fields added to the accepted body."""
        self.status = status
        self.extra = extra
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Capture the write, and answer with the status and the body this transport holds."""
        self.requests.append(request)
        body = {
            "thread_id": THREAD_ID,
            "language": "de",
            "source_hash": ORIGINAL_SOURCE_HASH,
            "changed": True,
            "billing": {
                "charged_credits": 0,
                "pricing_version": "p1",
                "usage_event_id": "55555555-5555-4555-8555-555555555555",
            },
            **self.extra,
        }
        return httpx.Response(
            self.status, json=body, headers={"x-request-id": "req_translation_567"}, request=request
        )


def _frozen_datetime(instant: dt.datetime) -> types.ModuleType:
    """Return a stand-in for the datetime module whose clock reads `instant`, and nothing else."""

    class FrozenDatetime(dt.datetime):
        """A datetime whose `now()` is the instant it was built for."""

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            """Return the instant the signing clock is frozen at."""
            return instant

    stand_in = types.ModuleType("datetime")
    for name in dir(dt):
        if not name.startswith("_"):
            setattr(stand_in, name, getattr(dt, name))
    stand_in.datetime = FrozenDatetime
    return stand_in


def _assert_new_operations_sign_the_published_vectors() -> None:
    """Check that the Connector signs the translation and origin vectors as the server verifies.

    Each new published case is replayed through the typed client with the case's own inputs: its
    body, its thread, its idempotency key, and its agent, key, timestamp and nonce. The body it
    sends must be the published JSON document, and the bytes it signs must be the envelope built
    from the bytes it sends under those identifiers. The published signature is verified over the
    published canonical string, with the key each case names, in `_assert_shared_vectors_hold`
    (agntnexus/agentnexus#567, D-262 items 5 and 10).
    """
    vectors = fixture("agentnexus-sig-v1-vectors.json")
    checked = []
    for case in vectors["cases"]:
        path = case["request"]["path"]
        is_translation = "/translations/" in path
        if not is_translation and not path.endswith("/recipe/origin"):
            continue
        request, headers = case["request"], case["headers"]
        body = json.loads(request["body_utf8"])
        billing = BillingDeclaration(
            pricing_version=body["billing"]["pricing_version"],
            max_credit_cost=body["billing"]["max_credit_cost"],
        )
        thread_id = path.split("/")[4]
        signer = _RecordingSigner()
        transport = RecordingTransport()
        client = AgentNexusClient(
            agent_id=headers["X-Agent-ID"],
            key_id=headers["X-Agent-Key-ID"],
            signer=signer,
            options=ClientOptions(base_url="http://127.0.0.1:1"),
            transport=transport,
        )
        instant = parse_timestamp(headers["X-Agent-Timestamp"])
        with (
            mock.patch.object(client_module, "dt", _frozen_datetime(instant)),
            mock.patch.object(client_module, "new_nonce", return_value=headers["X-Agent-Nonce"]),
            client,
        ):
            if is_translation:
                client.put_thread_translation(
                    thread_id=thread_id,
                    language=path.rsplit("/", 1)[1],
                    source_hash=body["source_hash"],
                    title=body["title"],
                    body_markdown=body["body_markdown"],
                    billing=billing,
                    idempotency_key=headers["Idempotency-Key"],
                )
            else:
                client.set_recipe_origin(
                    thread_id=thread_id,
                    origin_countries=body["origin_countries"],
                    is_fusion=body["is_fusion"],
                    billing=billing,
                    idempotency_key=headers["Idempotency-Key"],
                )
        (sent,) = transport.requests
        assert sent.url.path == path, case["name"]
        # The published body is the same JSON document. The client writes its keys in the order it
        # builds them: the server verifies the bytes it receives, so that order is not a rule.
        document = json.dumps(json.loads(sent.content), sort_keys=True, separators=(",", ":"))
        assert document == request["body_utf8"], case["name"]
        # The client signs exactly the bytes it sends, under the identifiers, timestamp and nonce of
        # the published case, so the envelope it signs is the one the server rebuilds from the wire.
        (message,) = signer.messages
        sent_envelope = build_envelope(
            EnvelopeInput(
                method=request["method"],
                path=path,
                query_string=request["query_string"],
                agent_id=headers["X-Agent-ID"],
                key_id=headers["X-Agent-Key-ID"],
                timestamp=instant,
                nonce=headers["X-Agent-Nonce"],
                idempotency_key=headers["Idempotency-Key"],
                body=sent.content,
            )
        )
        assert message == sent_envelope.signing_bytes(), case["name"]
        checked.append(case["name"])
    assert checked == ["thread-translation-put", "recipe-origin-put"]


def _assert_translation_statuses_are_reported() -> None:
    """Check that each answer the contract declares for a translation is reported as its status.

    The API answers 201 for a version it created and 200 for one it replaced or left unchanged,
    and a replay repeats the answer it stored. The bridge reports the first as created, the second
    as updated, and a replay as replayed (agntnexus/agentnexus#567).
    """
    declared = set(
        fixture("api-agent-contract-567.json")["operations"]["thread.translation.put"]["responses"]
    )
    answers = (
        ("201", {}, "created"),
        ("200", {}, "updated"),
        ("201", {"replayed": True}, "replayed"),
        ("200", {"replayed": True}, "replayed"),
    )
    assert {code for code, _, _ in answers} == declared
    config = bridge.BridgeConfig(
        agent_id=AGENT_ID,
        key_id=KEY_ID,
        private_key_file=Path(__file__),  # unused: the client is supplied
        agent_api_url="http://127.0.0.1:1",
        public_api_url="http://127.0.0.1:2",
        observer_url=None,
    )
    for code, extra, status in answers:
        transport = _AnsweringTransport(int(code), extra)
        client = AgentNexusClient(
            agent_id=AGENT_ID,
            key_id=KEY_ID,
            signer=generate_key_pair().signer,
            options=ClientOptions(base_url="http://127.0.0.1:1"),
            transport=transport,
        )
        with client:
            result = bridge.run_command(
                bridge.parse_command(json.dumps(translation()).encode()),
                config=config,
                client=client,
            )
        assert result["operation_status"] == status, (code, extra)
        assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "write",
    [
        "recipe-variants",
        "shared-vectors",
        "shared-vectors-new-operations",
        "contract-agreement",
        "translation-plain",
        "translation-recipe",
        "translation-wine-article",
        "translation-statuses",
        "origin",
        "thread-versions",
    ],
)
def test_the_recipe_writes_are_signed_as_sent_and_match_the_contract(write: str) -> None:
    """Every recipe write signs the bytes it sends, and what it sends is the API's contract.

    The shared signing vectors are rebuilt byte for byte, and their published signatures verify. The
    contract fixture names each new operation's method, path and body, and the Connector's closed
    sets are the pinned ones. A translated version and an origin are signed through the typed client
    and checked against the contract, and the read of a thread's languages is checked against the
    contract's own thread example (agntnexus/agentnexus#567).
    """
    if write == "recipe-variants":
        _assert_recipe_variants_are_signed()
    elif write == "shared-vectors":
        _assert_shared_vectors_hold()
    elif write == "shared-vectors-new-operations":
        _assert_new_operations_sign_the_published_vectors()
    elif write == "contract-agreement":
        _assert_contract_agreement()
    elif write == "translation-statuses":
        _assert_translation_statuses_are_reported()
    elif write == "thread-versions":
        _assert_thread_versions_reads_the_contract_example()
    else:
        _assert_signed_write_matches_contract(write)


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
    [
        "displays-and-bounds",
        "null-amounts",
        "null-food-ref",
        "null-food-ref-with-amounts",
        "origin-and-fusion",
        "origin-unclear",
    ],
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
    elif shape == "origin-and-fusion":
        structured["origin_countries"] = ["IT", "CH"]
        structured["is_fusion"] = True
    elif shape == "origin-unclear":
        structured["origin_countries"] = []
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
        ("recipe-origin-invalid-code", "is not an ISO 3166-1 alpha-2 country code"),
        ("recipe-origin-lowercase", "is not an ISO 3166-1 alpha-2 country code"),
        ("recipe-origin-uk", "is not an ISO 3166-1 alpha-2 country code"),
        ("recipe-origin-too-many", "at most five origin countries"),
        ("recipe-origin-duplicate", "must be unique"),
        ("recipe-origin-fusion-text", "is_fusion must be true or false"),
        ("recipe-origin-null-list", "origin_countries must be a list"),
        ("recipe-origin-unknown-field", "Unknown Recipe field"),
        ("translation-language-nl", "not an active language"),
        ("translation-language-regional", "not an active language"),
        ("translation-language-case", "not an active language"),
        ("translation-language-missing", "Field 'language' is required"),
        ("translation-field-wine", "outside the allowed set"),
        ("translation-field-image", "outside the allowed set"),
        ("translation-field-note", "outside the allowed set"),
        ("translation-field-amount", "outside the allowed set"),
        ("translation-field-food-ref", "outside the allowed set"),
        ("translation-field-times", "outside the allowed set"),
        ("translation-field-sources", "outside the allowed set"),
        ("translation-servings-five", "from 1 to 4"),
        ("translation-markup-step", "without markup"),
        ("translation-empty-step", "must not be empty"),
        ("translation-too-many-steps", "0 to 100 items"),
        ("translation-source-hash-short", "64 lower-case hexadecimal"),
        ("translation-source-hash-upper", "64 lower-case hexadecimal"),
        ("translation-empty-title", "1 to 200 characters"),
        ("translation-title-too-long", "1 to 200 characters"),
        ("translation-body-too-long", "1 to 20000 characters"),
        ("translation-missing-billing", "pricing_version"),
        ("translation-thread-id-upper", "canonical UUID"),
        ("origin-put-invalid-code", "is not an ISO 3166-1 alpha-2 country code"),
        ("origin-put-too-many", "at most five origin countries"),
        ("origin-put-duplicate", "must be unique"),
        ("origin-put-fusion-text", "is_fusion must be true or false"),
        ("origin-put-missing-fusion", "Field 'is_fusion' is required"),
        ("origin-put-missing-countries", "Field 'origin_countries' is required"),
        ("origin-put-null-countries", "Field 'origin_countries' is required"),
        ("origin-put-unknown-field", "Unknown field"),
        ("origin-put-missing-billing", "pricing_version"),
        ("origin-put-thread-id-upper", "canonical UUID"),
    ],
)
def test_malformed_or_fabricated_recipe_data_is_refused_before_sending(
    mutation: str, reason: str
) -> None:
    """Refuse a partial, fabricated or unknown value for the stated reason, before signing.

    The same holds for the origin of a Recipe named in its create command, for a translated version
    and for an origin write. Where the API names a stable code for a refusal, the bridge carries it
    (agntnexus/agentnexus#567).
    """
    if mutation.startswith(WRITE_MUTATION_PREFIXES):
        document = write_refusal_document(mutation)
        with pytest.raises(bridge.BridgeInputError, match=reason) as refused:
            bridge.parse_command(json.dumps(document).encode())
        assert getattr(refused.value, "code", None) == STABLE_CODES.get(mutation)
        return
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
    elif mutation == "recipe-origin-invalid-code":
        structured["origin_countries"] = ["XK"]
    elif mutation == "recipe-origin-lowercase":
        structured["origin_countries"] = ["de"]
    elif mutation == "recipe-origin-uk":
        structured["origin_countries"] = ["UK"]
    elif mutation == "recipe-origin-too-many":
        structured["origin_countries"] = ["DE", "AT", "CH", "IT", "FR", "ES"]
    elif mutation == "recipe-origin-duplicate":
        structured["origin_countries"] = ["DE", "DE"]
    elif mutation == "recipe-origin-fusion-text":
        structured["is_fusion"] = "true"
    elif mutation == "recipe-origin-null-list":
        structured["origin_countries"] = None
    elif mutation == "recipe-origin-unknown-field":
        structured["origin"] = {"countries": ["DE"]}

    with pytest.raises(bridge.BridgeInputError, match=reason) as refused:
        bridge.parse_command(json.dumps(command(structured=structured)).encode())
    assert getattr(refused.value, "code", None) == STABLE_CODES.get(mutation)


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
    # The origin of a Recipe is optional, at most five codes each once, and a JSON boolean (D-262).
    origin = recipe_schema["properties"]["origin_countries"]
    assert origin["type"] == "array"
    assert origin["maxItems"] == 5
    assert origin["uniqueItems"] is True
    assert origin["items"]["type"] == "string"
    assert recipe_schema["properties"]["is_fusion"]["type"] == "boolean"
    assert {"origin_countries", "is_fusion"}.isdisjoint(recipe_schema["required"])


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
