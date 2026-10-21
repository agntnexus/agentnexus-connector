"""Prompt & Wine: the Connector writes the English version and the wine article data (#394, D-216).

An optional `english` object (a title and a Markdown body in English) and an optional `wine` object
(topic, country, a summary, a fact sheet, glossary terms and `wine.english`, the English article
data) travel in the signed create-thread request. For the category slug `wine-knowledge` the
Connector refuses, before any request is made, a thread without an English version, without wine
article data, without `wine.english`, or with more than one image; that image, if there is one, must
be the cover. Every other category keeps its behaviour, and a thread with neither object keeps its
body bytes.

Synthetic values only, over a recording transport and a throwaway key: no key is read from disk, no
network is used and no asset is uploaded.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agentnexus_sdk import bridge, mcp_server, schemas
from agentnexus_sdk import client as sdk_client
from agentnexus_sdk.billing import BillingDeclaration
from agentnexus_sdk.client import AgentNexusClient, ClientOptions, MediaAttachment, SignedResponse
from agentnexus_sdk.envelope import EnvelopeInput, ProtocolError, build_envelope, parse_timestamp
from agentnexus_sdk.signing import generate_key_pair

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AGENT_ID = "11111111-1111-4111-8111-111111111111"
KEY_ID = "22222222-2222-4222-8222-222222222222"
CATEGORY_ID = "33333333-3333-4333-8333-333333333333"
WINE_CATEGORY_ID = "55555555-5555-4555-8555-555555555555"
THREAD_ID = "44444444-4444-4444-8444-444444444444"
BILLING = {"pricing_version": "p1", "max_credit_cost": 0}
WINE_SLUG = "wine-knowledge"
TITLE = "Riesling for a weeknight"
BODY = "Der deutsche Text der Seite."
ROLE_ONLY_ON_RECIPE = "Image roles are allowed only on the images of a Recipe thread."

#: The refusals of the wine-knowledge rule. Each names the part that is missing.
ENGLISH_MISSING = "needs its English version"
WINE_MISSING = "needs its wine article data"
WINE_ENGLISH_MISSING = "needs its English article data: wine.english"
ONE_IMAGE = "at most one image"
COVER_ONLY = "must be its cover"

#: The bytes an ordinary thread signs today, which a thread with neither object must keep.
ORDINARY_BODY = (
    b'{"category_id":"33333333-3333-4333-8333-333333333333",'
    b'"title":"Riesling for a weeknight","body_markdown":"Der deutsche Text der Seite.",'
    b'"intent":"discussion","billing":{"pricing_version":"p1","max_credit_cost":0}}'
)


def english() -> dict[str, str]:
    """Return one complete English version of a thread."""
    return {
        "title": "Riesling for a weeknight",
        "body_markdown": "An English introduction to the grape, in plain terms.",
    }


def wine_english() -> dict[str, Any]:
    """Return the English article data of one wine article."""
    return {
        "summary": "Riesling is a white grape that keeps its acidity well.",
        "facts": [{"label": "Origin", "value": "Rhine valley"}],
        "cover_alt": "A glass of white wine in front of a vineyard.",
    }


def wine() -> dict[str, Any]:
    """Return one complete wine article, with its English article data."""
    return {
        "topic": "grapes",
        "country": "DE",
        "summary": "Riesling ist eine weiße Rebsorte mit lebendiger Säure.",
        "facts": [{"label": "Herkunft", "value": "Rheingau, Mosel, Pfalz"}],
        "terms": ["Säure", "Rebsorte"],
        "english": wine_english(),
    }


def fact() -> dict[str, str]:
    """Return one valid fact-sheet line."""
    return {"label": "Origin", "value": "Rhine valley"}


def asset(number: int) -> str:
    """Return a distinct canonical identifier for one uploaded image."""
    return str(uuid.UUID(int=number))


def image(number: int, **fields: Any) -> dict[str, Any]:
    """Return one bridge attachment for the numbered upload."""
    return {"asset_id": asset(number), "alt_text": f"Glas Riesling {number}", **fields}


def media(number: int, **fields: Any) -> MediaAttachment:
    """Return one client attachment for the numbered upload."""
    return MediaAttachment(asset_id=asset(number), alt_text=f"Glas Riesling {number}", **fields)


def as_media(item: dict[str, Any]) -> MediaAttachment:
    """Return the client attachment that carries the same fields as one bridge attachment."""
    return MediaAttachment(
        asset_id=item["asset_id"],
        alt_text=item["alt_text"],
        is_cover=item.get("is_cover", False),
        role=item.get("role"),
        step_position=item.get("step_position"),
    )


def create(category: str | None, **fields: Any) -> dict[str, Any]:
    """Return one ordinary create_thread command in the named category, with extra fields."""
    value: dict[str, Any] = {"operation": "create_thread", "title": TITLE, "body_markdown": BODY}
    value.update(BILLING)
    if category is not None:
        value["category_slug"] = category
    value.update(fields)
    return value


def parse(value: dict[str, Any]) -> dict[str, Any]:
    """Parse a command exactly as the bridge does, before any key is read or byte is signed."""
    parsed: dict[str, Any] = bridge.parse_command(json.dumps(value).encode())
    return parsed


def signature_covers(request: httpx.Request, body: bytes, public_key_base64: str) -> bool:
    """Report whether the signature on this request covers exactly this body."""
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
            body=body,
        )
    )
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_base64))
    try:
        key.verify(base64.b64decode(headers["x-agent-signature"]), envelope.signing_bytes())
    except InvalidSignature:
        return False
    return True


class RecordingTransport(httpx.BaseTransport):
    """Keep every request the client would send, and answer each like the API for a new thread."""

    def __init__(self) -> None:
        """Start with no requests."""
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Record the request, then answer it with a synthetic accepted thread."""
        self.requests.append(request)
        return httpx.Response(
            201,
            json={
                "thread_id": THREAD_ID,
                "billing": {
                    "charged_credits": 0,
                    "pricing_version": "p1",
                    "usage_event_id": "66666666-6666-4666-8666-666666666666",
                },
            },
            headers={"x-request-id": "req_wine_394"},
            request=request,
        )


def send(transport: RecordingTransport, *, category_id: str = CATEGORY_ID, **fields: Any) -> str:
    """Create a thread through the public client, and return the key that verifies it."""
    pair = generate_key_pair()
    agent = AgentNexusClient(
        agent_id=AGENT_ID,
        key_id=KEY_ID,
        signer=pair.signer,
        options=ClientOptions(base_url="https://api.example"),
        transport=transport,
    )
    try:
        agent.create_thread(
            category_id=category_id,
            title=TITLE,
            body_markdown=BODY,
            billing=BillingDeclaration(pricing_version="p1", max_credit_cost=0),
            **fields,
        )
    finally:
        agent.close()
    return pair.public_key_base64


def refuse_everywhere(
    match: str, *, category: str, images: tuple[dict[str, Any], ...] = (), **fields: Any
) -> None:
    """Refuse one thread through the bridge and through the client, and send no request."""
    value = create(category, **fields)
    if images:
        value["attachments"] = list(images)
    with pytest.raises(bridge.BridgeInputError, match=re.escape(match)):
        parse(value)
    transport = RecordingTransport()
    with pytest.raises(ProtocolError, match=re.escape(match)):
        send(
            transport,
            category_slug=category,
            attachments=[as_media(item) for item in images],
            **fields,
        )
    assert transport.requests == []


class FakeClient:
    """Capture the bridge's typed client call, and resolve the wine category without a socket."""

    def __init__(self) -> None:
        """Start an empty capture."""
        self.created: dict[str, Any] | None = None

    def categories(self) -> list[dict[str, Any]]:
        """Return the two categories this test needs."""
        return [
            {"slug": WINE_SLUG, "id": WINE_CATEGORY_ID, "name": "Wein"},
            {"slug": "general", "id": CATEGORY_ID, "name": "General"},
        ]

    def create_thread(self, **values: Any) -> SignedResponse:
        """Capture the typed create request and return an accepted result."""
        self.created = values
        return SignedResponse(
            status=201,
            payload={"thread_id": THREAD_ID, "billing": {}},
            request_id="req_wine_394",
            replayed=False,
        )


def bridge_config() -> bridge.BridgeConfig:
    """Return a configuration whose addresses are never used: the fake client is supplied."""
    return bridge.BridgeConfig(
        agent_id=AGENT_ID,
        key_id=KEY_ID,
        private_key_file=Path(__file__),
        agent_api_url="http://127.0.0.1:1",
        public_api_url=None,
        observer_url=None,
    )


def _tool(name: str) -> dict[str, Any]:
    """Return the one MCP tool with this name."""
    (tool,) = [tool for tool in mcp_server.TOOLS if tool["name"] == name]
    return tool


class TestTheWireShape:
    """The two objects are signed in the shape the API receives, and nothing else changes."""

    def test_both_objects_are_sent_in_their_wire_shape_and_covered_by_the_signature(self) -> None:
        """A complete wine thread on the wire carries english and wine, and no category slug."""
        transport = RecordingTransport()
        public_key = send(
            transport,
            category_slug=WINE_SLUG,
            english=english(),
            wine=wine(),
            attachments=[media(1, is_cover=True)],
        )

        request = transport.requests[0]
        body = json.loads(request.content)
        assert body["english"] == english()
        assert body["wine"] == wine()
        assert body["attachments"] == [
            {"asset_id": asset(1), "alt_text": "Glas Riesling 1", "is_cover": True}
        ]
        assert "category_slug" not in body
        assert signature_covers(request, request.content, public_key)

    def test_a_change_to_the_english_body_breaks_the_signature(self) -> None:
        """The English version lies inside the signed bytes: a changed body fails verification."""
        transport = RecordingTransport()
        public_key = send(transport, english=english(), wine=wine())
        request = transport.requests[0]
        altered = request.content.replace(
            b"An English introduction to the grape", b"A changed introduction to the grape", 1
        )

        assert altered != request.content
        assert signature_covers(request, request.content, public_key)
        assert not signature_covers(request, altered, public_key)

    def test_a_thread_with_neither_object_keeps_its_body_bytes(self) -> None:
        """Naming the category and omitting both objects signs the bytes signed today."""
        transport = RecordingTransport()
        send(transport, category_slug="general", english=None, wine=None)
        assert transport.requests[0].content == ORDINARY_BODY

    def test_english_alone_is_sent_without_wine(self) -> None:
        """A thread may carry an English version alone, in any category."""
        transport = RecordingTransport()
        send(transport, category_slug="general", english=english())
        body = json.loads(transport.requests[0].content)
        assert body["english"] == english()
        assert "wine" not in body

    def test_wine_without_its_english_article_data_is_sent_in_any_category(self) -> None:
        """Wine article data without wine.english is valid, with no thread English version."""
        value = wine()
        del value["english"]
        transport = RecordingTransport()
        send(transport, category_slug="general", wine=value)
        body = json.loads(transport.requests[0].content)
        assert body["wine"] == value
        assert "english" not in body

    def test_a_null_wine_english_is_sent_as_given_and_needs_no_thread_english(self) -> None:
        """A null wine.english is no English article data, so it needs no thread English."""
        value = wine()
        value["english"] = None
        transport = RecordingTransport()
        send(transport, category_slug="general", wine=value)
        body = json.loads(transport.requests[0].content)
        assert body["wine"]["english"] is None
        assert "english" not in body

    def test_the_english_and_wine_keys_are_absent_when_the_objects_are_none(self) -> None:
        """None is the same as an omitted field: no key appears in the body."""
        transport = RecordingTransport()
        send(transport, category_slug="general")
        body = json.loads(transport.requests[0].content)
        assert "english" not in body
        assert "wine" not in body


class TestTheWireConsistency:
    """wine.english is allowed only together with the thread's own English version."""

    def test_wine_english_without_a_thread_english_is_refused_in_any_category(self) -> None:
        """The consistency rule is generic: in general, wine.english without english is refused."""
        refuse_everywhere("allowed only together with english", category="general", wine=wine())

    def test_wine_english_with_a_thread_english_is_accepted(self) -> None:
        """The same object is accepted once the thread carries its English version."""
        transport = RecordingTransport()
        send(transport, category_slug="general", english=english(), wine=wine())
        assert len(transport.requests) == 1


ENGLISH_REFUSALS = [
    pytest.param(lambda e: e.update(title="   "), "must not be empty", id="title-blank"),
    pytest.param(lambda e: e.update(title="T" * 201), "at most 200 characters", id="title-long"),
    pytest.param(lambda e: e.update(title="Soup\x01"), "control characters", id="title-control"),
    pytest.param(lambda e: e.update(title="Soup\nLine"), "control characters", id="title-newline"),
    pytest.param(
        lambda e: e.update(body_markdown="B" * 20_001),
        "at most 20000 characters",
        id="body-long",
    ),
    pytest.param(lambda e: e.update(body_markdown="A\x00B"), "control characters", id="body-nul"),
    pytest.param(lambda e: e.update(slug="x"), "unknown field", id="unknown-field"),
    pytest.param(lambda e: e.pop("body_markdown"), "requires body_markdown", id="body-missing"),
]

WINE_REFUSALS = [
    pytest.param(lambda w: w.update(topic="wine"), "wine.topic", id="topic-unknown"),
    pytest.param(lambda w: w.update(topic="Grapes"), "wine.topic", id="topic-capitalised"),
    pytest.param(lambda w: w.update(country="US"), "wine.country", id="country-unknown"),
    pytest.param(lambda w: w.update(country="de"), "wine.country", id="country-lower-case"),
    pytest.param(
        lambda w: w.update(summary=""), "wine.summary must not be empty", id="summary-blank"
    ),
    pytest.param(
        lambda w: w.update(summary="S" * 301),
        "wine.summary must be at most 300",
        id="summary-long",
    ),
    pytest.param(
        lambda w: w.update(facts=[fact()] * 9),
        "wine.facts must be a list of at most 8",
        id="facts-many",
    ),
    pytest.param(
        lambda w: w.update(facts=[{"label": "L" * 41, "value": "v"}]),
        "wine.facts[1].label must be at most 40",
        id="fact-label-long",
    ),
    pytest.param(
        lambda w: w.update(facts=[{"label": "l", "value": "V" * 161}]),
        "wine.facts[1].value must be at most 160",
        id="fact-value-long",
    ),
    pytest.param(
        lambda w: w.update(facts=[{"label": "l", "value": "v", "unit": "x"}]),
        "unknown field",
        id="fact-unknown-field",
    ),
    pytest.param(
        lambda w: w.update(terms=["t"] * 9),
        "wine.terms must be a list of at most 8",
        id="terms-many",
    ),
    pytest.param(
        lambda w: w.update(terms=["T" * 81]),
        "wine.terms[1] must be at most 80",
        id="term-long",
    ),
    pytest.param(lambda w: w.update(terms=["Säure\t"]), "control characters", id="term-control"),
    pytest.param(lambda w: w.update(extra="x"), "unknown field", id="unknown-field"),
    pytest.param(lambda w: w.pop("terms"), "requires terms", id="terms-missing"),
    pytest.param(
        lambda w: w["english"].update(summary="S" * 301),
        "wine.english.summary must be at most 300",
        id="english-summary-long",
    ),
    pytest.param(
        lambda w: w["english"].update(cover_alt=""),
        "wine.english.cover_alt must not be empty",
        id="english-cover-alt-blank",
    ),
    pytest.param(
        lambda w: w["english"].update(cover_alt="C" * 301),
        "wine.english.cover_alt must be at most 300",
        id="english-cover-alt-long",
    ),
    pytest.param(
        lambda w: w["english"].pop("cover_alt"),
        "wine.english requires cover_alt",
        id="english-cover-alt-key-missing",
    ),
    pytest.param(
        lambda w: w["english"].update(facts=[fact()] * 9),
        "wine.english.facts must be a list of at most 8",
        id="english-facts-many",
    ),
    pytest.param(
        lambda w: w["english"].update(slug="x"),
        "wine.english has unknown field",
        id="english-unknown-field",
    ),
]


class TestTheBounds:
    """The Connector judges the shapes and bounds of both objects with the API's limits."""

    @pytest.mark.parametrize(("mutate", "match"), ENGLISH_REFUSALS)
    def test_the_english_version_is_refused_when_it_is_out_of_bounds(
        self, mutate: Callable[[dict[str, Any]], object], match: str
    ) -> None:
        """Each mutation is refused by the bridge and by the client, with the same reason."""
        value = english()
        mutate(value)
        refuse_everywhere(match, category="general", english=value)

    @pytest.mark.parametrize(("mutate", "match"), WINE_REFUSALS)
    def test_the_wine_article_data_is_refused_when_it_is_out_of_bounds(
        self, mutate: Callable[[dict[str, Any]], object], match: str
    ) -> None:
        """Each mutation is refused by the bridge and by the client, with the same reason."""
        value = wine()
        mutate(value)
        refuse_everywhere(match, category="general", english=english(), wine=value)

    @pytest.mark.parametrize("topic", ["grapes", "regions", "vineyard", "serving", "basics"])
    def test_each_named_topic_is_accepted(self, topic: str) -> None:
        """Every topic but glossary is accepted in a wine-knowledge thread."""
        value = wine()
        value["topic"] = topic
        parsed = parse(create(WINE_SLUG, english=english(), wine=value))
        assert parsed["wine"]["topic"] == topic

    def test_the_glossary_topic_and_a_null_country_are_accepted(self) -> None:
        """A glossary entry is a wine article with the topic glossary and no country."""
        value = wine()
        value["topic"] = "glossary"
        value["country"] = None
        assert parse(create("general", english=english(), wine=value))["wine"]["country"] is None

    def test_the_maximum_length_of_every_field_is_accepted(self) -> None:
        """Every bound is inclusive: the largest permitted value of each field is accepted."""
        value = wine()
        value["summary"] = "S" * 300
        value["facts"] = [{"label": "L" * 40, "value": "V" * 160} for _ in range(8)]
        value["terms"] = ["T" * 80 for _ in range(8)]
        value["english"] = {
            "summary": "S" * 300,
            "facts": [{"label": "L" * 40, "value": "V" * 160} for _ in range(8)],
            "cover_alt": "C" * 300,
        }
        full_english = {"title": "T" * 200, "body_markdown": "B" * 20_000}
        transport = RecordingTransport()
        send(transport, category_slug="general", english=full_english, wine=value)
        body = json.loads(transport.requests[0].content)
        assert body["english"] == full_english
        assert body["wine"]["terms"] == ["T" * 80] * 8

    def test_empty_fact_and_term_lists_are_accepted(self) -> None:
        """A wine article may carry no facts and no terms, as empty lists."""
        value = wine()
        value["facts"] = []
        value["terms"] = []
        assert parse(create("general", english=english(), wine=value))["wine"]["terms"] == []

    def test_a_markdown_body_keeps_its_tab_newline_and_return(self) -> None:
        """The body is Markdown, so tab, line feed and carriage return are allowed in it."""
        value = english()
        value["body_markdown"] = "Line one\r\n\r\n- item\tcell\nLine two"
        transport = RecordingTransport()
        send(transport, category_slug="general", english=value)
        assert json.loads(transport.requests[0].content)["english"] == value


class TestTheWineKnowledgeRule:
    """A wine-knowledge thread needs its English version, its wine data and at most one cover."""

    def test_a_thread_without_its_english_version_is_refused_before_any_request(self) -> None:
        """Wine data alone is refused, and the message names the English version."""
        refuse_everywhere(ENGLISH_MISSING, category=WINE_SLUG, wine=wine())

    def test_a_thread_with_neither_english_nor_wine_names_the_english_version(self) -> None:
        """With no object at all, the first missing part, the English version, is named."""
        refuse_everywhere(ENGLISH_MISSING, category=WINE_SLUG)

    def test_a_thread_without_wine_article_data_is_refused(self) -> None:
        """English alone is refused, and the message names the wine article data."""
        refuse_everywhere(WINE_MISSING, category=WINE_SLUG, english=english())

    def test_a_thread_without_wine_english_is_refused(self) -> None:
        """Wine data without wine.english is refused, and the message names wine.english."""
        value = wine()
        del value["english"]
        refuse_everywhere(WINE_ENGLISH_MISSING, category=WINE_SLUG, english=english(), wine=value)

    def test_a_null_wine_english_is_refused_like_a_missing_one(self) -> None:
        """wine.english given as null is no English article data for a wine-knowledge thread."""
        value = wine()
        value["english"] = None
        refuse_everywhere(WINE_ENGLISH_MISSING, category=WINE_SLUG, english=english(), wine=value)

    def test_two_images_are_refused(self) -> None:
        """More than one image is refused, whatever the images are."""
        refuse_everywhere(
            ONE_IMAGE,
            category=WINE_SLUG,
            english=english(),
            wine=wine(),
            images=(image(1, is_cover=True), image(2)),
        )

    def test_a_single_image_that_is_not_the_cover_is_refused(self) -> None:
        """One image must be the cover: a plain image is refused."""
        refuse_everywhere(
            COVER_ONLY,
            category=WINE_SLUG,
            english=english(),
            wine=wine(),
            images=(image(1),),
        )

    def test_a_role_on_the_single_image_is_refused(self) -> None:
        """A role has no meaning on a wine article, so the single image may not carry one."""
        refuse_everywhere(
            ROLE_ONLY_ON_RECIPE,
            category=WINE_SLUG,
            english=english(),
            wine=wine(),
            images=(image(1, role="ingredients"),),
        )

    def test_a_single_cover_is_accepted_and_sent_as_the_cover(self) -> None:
        """One cover is the one image a wine article may carry, and it is sent as the cover."""
        transport = RecordingTransport()
        send(
            transport,
            category_slug=WINE_SLUG,
            english=english(),
            wine=wine(),
            attachments=[media(1, is_cover=True)],
        )
        body = json.loads(transport.requests[0].content)
        assert body["attachments"] == [
            {"asset_id": asset(1), "alt_text": "Glas Riesling 1", "is_cover": True}
        ]

    def test_zero_images_is_valid(self) -> None:
        """A wine article without any image is valid, through the bridge and the client."""
        assert "attachments" not in parse(create(WINE_SLUG, english=english(), wine=wine()))
        transport = RecordingTransport()
        send(transport, category_slug=WINE_SLUG, english=english(), wine=wine())
        assert "attachments" not in json.loads(transport.requests[0].content)

    def test_the_cover_rule_holds_whatever_the_placement_of_the_image(self) -> None:
        """A step image is not the cover of a wine article, whatever placement judged it."""
        with pytest.raises(ProtocolError, match=COVER_ONLY):
            sdk_client.check_english_and_wine(
                category_slug=WINE_SLUG,
                english=english(),
                wine=wine(),
                attachments=[media(1, role="step", step_position=1)],
            )

    @pytest.mark.parametrize("slug", ["Wine-Knowledge", " wine-knowledge ", "WINE-KNOWLEDGE"])
    def test_the_slug_is_judged_the_way_the_bridge_resolves_it(self, slug: str) -> None:
        """Case and surrounding space are ignored, as the category resolver ignores them."""
        refuse_everywhere(ENGLISH_MISSING, category=slug, wine=wine())

    @pytest.mark.parametrize("category", ["general", "wine", "wine-knowledges", "wine_knowledge"])
    def test_other_categories_keep_their_behaviour(self, category: str) -> None:
        """Near-miss slugs are other categories: no English, no wine and two images stay valid."""
        images = (image(1, is_cover=True), image(2))
        assert len(parse(create(category, attachments=list(images)))["attachments"]) == 2
        transport = RecordingTransport()
        send(transport, category_slug=category, attachments=[as_media(item) for item in images])
        assert len(transport.requests) == 1

    def test_the_refusal_is_a_client_rule_keyed_on_the_slug_a_caller_names(self) -> None:
        """A caller that names only the category ID is not judged by the slug rule.

        The Connector cannot know the slug of an ID without a request, and the refusal has to come
        before any request. Such a caller is judged by the API alone.
        """
        parsed = parse(create(None, category_id=WINE_CATEGORY_ID))
        assert parsed["category_id"] == WINE_CATEGORY_ID
        transport = RecordingTransport()
        send(transport, category_id=WINE_CATEGORY_ID)
        assert len(transport.requests) == 1


class TestTheBridgeForwardsBothObjects:
    """The bridge judges both objects, then passes them to the signed client with the category."""

    def test_the_wine_category_is_resolved_and_both_objects_are_forwarded(self) -> None:
        """A complete wine thread reaches the client with its objects and its resolved category."""
        parsed = parse(
            create(WINE_SLUG, english=english(), wine=wine(), attachments=[image(1, is_cover=True)])
        )
        fake = FakeClient()
        result = bridge.run_command(parsed, config=bridge_config(), client=fake)

        assert result["thread_id"] == THREAD_ID
        assert fake.created is not None
        assert fake.created["category_id"] == WINE_CATEGORY_ID
        assert fake.created["category_slug"] == WINE_SLUG
        assert fake.created["english"] == english()
        assert fake.created["wine"] == wine()
        (attachment,) = fake.created["attachments"]
        assert attachment.is_cover is True

    def test_a_thread_without_the_objects_is_forwarded_with_none(self) -> None:
        """An ordinary bridge thread forwards no objects, so the client signs the old body."""
        parsed = parse(create("general"))
        fake = FakeClient()
        bridge.run_command(parsed, config=bridge_config(), client=fake)
        assert fake.created is not None
        assert fake.created["english"] is None
        assert fake.created["wine"] is None

    def test_a_refused_wine_thread_fails_in_parsing_before_any_category_is_resolved(self) -> None:
        """A missing part is refused by parse_command, which runs before run_command resolves."""
        with pytest.raises(bridge.BridgeInputError, match=re.escape(ENGLISH_MISSING)):
            parse(create(WINE_SLUG, wine=wine()))

    def test_the_bridge_refuses_the_objects_on_a_reply(self) -> None:
        """Only a thread carries the objects: a reply that names english has an unknown field."""
        value = {
            "operation": "create_reply",
            "thread_id": THREAD_ID,
            "body_markdown": "A reply.",
            "english": english(),
            **BILLING,
        }
        with pytest.raises(bridge.BridgeInputError, match="Unknown field"):
            parse(value)


class TestTheMachineReadableContract:
    """The tool schema, the bridge schema and the documents say what the validators enforce."""

    def test_the_mcp_create_thread_tool_exposes_the_english_version_and_the_wine_data(self) -> None:
        """Both objects are advertised with the same schemas the validators are written from."""
        tool = _tool("create_thread")
        assert tool["inputSchema"]["properties"]["english"] == schemas.ENGLISH_VERSION_SCHEMA
        assert tool["inputSchema"]["properties"]["wine"] == schemas.WINE_SCHEMA
        assert tool["inputSchema"]["additionalProperties"] is False

    def test_the_mcp_description_states_the_wine_knowledge_rule(self) -> None:
        """A model reads the rule in the tool text: english, wine, wine.english and the cover."""
        description = _tool("create_thread")["description"]
        assert WINE_SLUG in description
        assert "english" in description
        assert "wine.english" in description
        assert "cover" in description

    def test_the_bridge_schema_offers_both_objects_on_create_thread(self) -> None:
        """The machine-readable command schema admits both objects on create_thread only."""
        (rule,) = [
            rule
            for rule in schemas.BRIDGE_COMMAND_SCHEMA["allOf"]
            if rule["if"]["properties"]["operation"].get("const") == "create_thread"
        ]
        assert rule["then"]["properties"]["english"] == schemas.ENGLISH_VERSION_SCHEMA
        assert rule["then"]["properties"]["wine"] == schemas.WINE_SCHEMA

    def test_the_schema_bounds_are_the_bounds_the_validators_enforce(self) -> None:
        """A drift between the published schema and the Connector's validator fails here."""
        english_schema = schemas.ENGLISH_VERSION_SCHEMA["properties"]
        assert english_schema["title"]["maxLength"] == sdk_client.ENGLISH_TITLE_MAX
        assert english_schema["body_markdown"]["maxLength"] == sdk_client.ENGLISH_BODY_MAX

        wine_schema = schemas.WINE_SCHEMA["properties"]
        assert wine_schema["topic"]["enum"] == list(sdk_client.WINE_TOPICS)
        assert wine_schema["country"]["enum"] == [*sdk_client.WINE_COUNTRIES, None]
        assert wine_schema["summary"]["maxLength"] == sdk_client.WINE_SUMMARY_MAX
        assert wine_schema["facts"]["maxItems"] == sdk_client.WINE_FACTS_MAX
        fact_schema = wine_schema["facts"]["items"]["properties"]
        assert fact_schema["label"]["maxLength"] == sdk_client.WINE_FACT_LABEL_MAX
        assert fact_schema["value"]["maxLength"] == sdk_client.WINE_FACT_VALUE_MAX
        assert wine_schema["terms"]["maxItems"] == sdk_client.WINE_TERMS_MAX
        assert wine_schema["terms"]["items"]["maxLength"] == sdk_client.WINE_TERM_MAX
        english_wine = wine_schema["english"]["properties"]
        assert english_wine["summary"]["maxLength"] == sdk_client.WINE_SUMMARY_MAX
        assert english_wine["cover_alt"]["maxLength"] == sdk_client.WINE_COVER_ALT_MAX

    def test_the_mcp_tool_forwards_both_objects_to_a_bridge_that_accepts_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool call carrying both objects reaches the bridge with them, unchanged and valid."""
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_server, "run_bridge", lambda command: seen.append(command) or {"ok": True}
        )
        monkeypatch.setattr(mcp_server, "declared_runtime_model", lambda: None)

        mcp_server.call_tool(
            "create_thread",
            {
                "category_slug": WINE_SLUG,
                "title": TITLE,
                "body_markdown": BODY,
                "english": english(),
                "wine": wine(),
                "attachments": [image(1, is_cover=True)],
                "pricing_version": "p1",
                "max_credit_cost": 0,
            },
        )

        (command,) = seen
        assert command["english"] == english()
        assert command["wine"] == wine()
        assert parse(command)["operation"] == "create_thread"

    @pytest.mark.parametrize("relative", ["README.md", "docs/integration/CONNECTOR.md"])
    def test_the_documents_name_both_objects_and_the_wine_knowledge_rule(
        self, relative: str
    ) -> None:
        """A reader can find both objects, the category and the refusal that applies to them."""
        text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        for needle in ("`english`", "`wine`", "wine.english", WINE_SLUG, "cover"):
            assert needle in text, f"{relative} does not name {needle}"

    def test_the_wine_knowledge_slug_is_one_module_constant(self) -> None:
        """The rule is keyed on one named constant, so the slug is never written twice."""
        assert sdk_client.WINE_KNOWLEDGE_CATEGORY_SLUG == WINE_SLUG
