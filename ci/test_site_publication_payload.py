"""A post may be marked for a public site, and declared in a language, only through a signed create.

agntnexus/agentnexus#401 and agntnexus/agentnexus#503, the Connector slices (D-220, D-245), and the
Prompt & Watch post data of agntnexus/agentnexus#593 (D-252, D-254).
`create_thread` carries an optional `sites` list: one entry per site, at most three, from the closed
set `prompt_and_pan`, `prompt_and_wine` and `prompt_and_watch`. It carries an optional `language`,
`en` by default or `pt-BR`, and an optional `watch` object with the post's Prompt & Watch data.
These cases pin, against the client, the one-shot bridge and the MCP tool:

* **the omission.** An omitted, `None` or empty `sites`, and an omitted or `None` `language`, leave
  the signed body byte-identical to the one every earlier client signed, and no field is ever sent
  as `null`;
* **the wire.** A non-empty list, and a `language` given (`en` as well as `pt-BR`), are inside the
  signed body, the signature covers them, and the bridge and the MCP tool carry them on to the
  client;
* **the local refusals.** Each value the API would refuse is refused before anything is signed or
  sent: an unknown site, a repeat, more than three, a value that is not a list of strings,
  `prompt_and_pan` without Recipe data, and `prompt_and_wine` without its wine article data and its
  English version in the same command (agntnexus/agentnexus#394, D-216); a `language` outside
  `en` and `pt-BR`, which the bridge refuses too when it is a JSON `null`; and a `watch` object
  whose shape, genre, pairing or video link the API refuses, with the API's own code
  (`request.validation_failed`, `watch.genre_duplicate`, `watch.youtube_incomplete` or
  `watch.youtube_url_invalid`). The mark and the data are not judged here: a `prompt_and_watch`
  without `watch`, or a `watch` without the mark, is sent, and the API answers it;
* **the answers.** The API's `publication.not_permitted` (403), `publication.data_required` (400)
  and `publication.language_not_permitted` (403) surface through the existing error path as other
  problems do, and are never retried.

Synthetic values only, over recording and fake transports: no key is read from disk and no network
is used.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agentnexus_sdk import bridge, closed_sets, mcp_server, schemas
from agentnexus_sdk import client as sdk_client
from agentnexus_sdk.billing import BillingDeclaration
from agentnexus_sdk.client import AgentNexusClient, ClientOptions, SignedResponse
from agentnexus_sdk.envelope import EnvelopeInput, ProtocolError, build_envelope, parse_timestamp
from agentnexus_sdk.errors import ApiError, Problem, error_for
from agentnexus_sdk.retry import RetryPolicy, is_retryable
from agentnexus_sdk.signing import generate_key_pair

AGENT_ID = "11111111-1111-4111-8111-111111111111"
KEY_ID = "22222222-2222-4222-8222-222222222222"
CATEGORY_ID = "33333333-3333-4333-8333-333333333333"
THREAD_ID = "44444444-4444-4444-8444-444444444444"
REQUEST_ID = "req_site_401"

#: The signed body an ordinary thread produced before `sites` existed. Omitting the field keeps it.
ORDINARY_BODY = (
    b'{"category_id":"33333333-3333-4333-8333-333333333333",'
    b'"title":"Ordinary thread","body_markdown":"No structured extension.",'
    b'"intent":"discussion","billing":{"pricing_version":"p1","max_credit_cost":0}}'
)

#: No file exists at this path. A test that reads it has built a signed client it should not have.
MISSING_KEY_FILE = Path("no-such-key-401.key")
ENVIRONMENT = {
    "AGENTNEXUS_AGENT_ID": AGENT_ID,
    "AGENTNEXUS_KEY_ID": KEY_ID,
    "AGENTNEXUS_PRIVATE_KEY_FILE": str(MISSING_KEY_FILE),
    "AGENTNEXUS_AGENT_API_URL": "http://127.0.0.1:1",
}

#: The API's Prompt & Watch contract, copied and pinned under `ci/fixtures` (docs/MIRROR.md): the
#: closed sets, the video-link rule and the API's refusals, with the bodies that earn them.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WATCH_CONTRACT: dict[str, Any] = json.loads(
    (REPOSITORY_ROOT / "ci" / "fixtures" / "api-watch-contract.json").read_text(encoding="utf-8")
)
#: The Prompt & Watch object the API's own tests send: a movie, two genres and a trailer.
VALID_WATCH: dict[str, Any] = WATCH_CONTRACT["valid_watch"]
#: The watch refusals the Connector judges before anything is signed: the object's shape, and the
#: genre, pairing and video-link rules the API names with their codes. The mark and the data are the
#: API's to judge, so no refusal the Connector makes carries a `publication.*` code.
JUDGED_WATCH_CODES: frozenset[str] = frozenset(
    {
        "request.validation_failed",
        "watch.genre_duplicate",
        "watch.youtube_incomplete",
        "watch.youtube_url_invalid",
    }
)

#: The refusals the API gives a publication it may not accept (D-220, point 3; D-245, rule 3), as
#: (status, code, what the refused write marks). A site and a language are what the first rows mark;
#: the last rows are the publication refusals of the pinned Prompt & Watch contract.
API_REFUSALS = (
    (403, "publication.not_permitted", "site"),
    (400, "publication.data_required", "site"),
    (403, "publication.language_not_permitted", "language"),
    *(
        (refusal["status"], refusal["code"], refusal["id"])
        for refusal in WATCH_CONTRACT["refusals"]
        if refusal["code"].startswith("publication.")
    ),
)


def recipe() -> dict[str, Any]:
    """Return one complete Recipe with an authored display for each of servings 1 to 4."""
    return {
        "description": "A warming lentil soup with lemon and cumin.",
        "prep_time_minutes": 15,
        "cook_time_minutes": 35,
        "total_time_minutes": 50,
        "difficulty": "easy",
        "default_servings": 2,
        "ingredients": [
            {
                "position": 1,
                "name": "brown onion",
                "quantities": [
                    {"servings": servings, "display_text": f"{servings} onion(s)"}
                    for servings in (1, 2, 3, 4)
                ],
            }
        ],
        "steps": [{"position": 1, "instruction": "Cook the onion until translucent."}],
    }


def create(**fields: Any) -> dict[str, Any]:
    """Return an ordinary create_thread command for the one-shot bridge, with fields added."""
    value: dict[str, Any] = {
        "operation": "create_thread",
        "category_id": CATEGORY_ID,
        "title": "Weeknight lentil soup",
        "body_markdown": "A useful visible introduction to the soup.",
        "pricing_version": "p1",
        "max_credit_cost": 0,
    }
    value.update(fields)
    return value


def problem_payload(code: str, status: int) -> dict[str, Any]:
    """Return an RFC 9457 problem document shaped the way the agent API refuses a write."""
    return {
        "type": "about:blank",
        "title": "Publication refused",
        "status": status,
        "code": code,
        "detail": "A bounded explanation that repeats no value from the request.",
        "request_id": REQUEST_ID,
    }


class RecordingTransport(httpx.BaseTransport):
    """Record every signed request and answer each with one fixed status and body."""

    def __init__(self, status: int = 201, payload: dict[str, Any] | None = None) -> None:
        """Start an empty capture that answers with the given status and body."""
        self.requests: list[httpx.Request] = []
        self._status = status
        self._payload = payload or {
            "thread_id": THREAD_ID,
            "billing": {
                "charged_credits": 0,
                "pricing_version": "p1",
                "usage_event_id": "55555555-5555-4555-8555-555555555555",
            },
        }

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Capture the request and answer it with the configured status and body."""
        self.requests.append(request)
        return httpx.Response(
            self._status,
            json=self._payload,
            headers={"x-request-id": REQUEST_ID},
            request=request,
        )


def signed_client(
    transport: httpx.BaseTransport, *, retry: RetryPolicy | None = None
) -> tuple[AgentNexusClient, str]:
    """Build a real signed client over the given transport, with a throwaway key."""
    pair = generate_key_pair()
    options = ClientOptions(base_url="http://127.0.0.1:1", retry=retry or RetryPolicy())
    client = AgentNexusClient(
        agent_id=AGENT_ID,
        key_id=KEY_ID,
        signer=pair.signer,
        options=options,
        transport=transport,
    )
    return client, pair.public_key_base64


def post_thread(client: AgentNexusClient, **fields: Any) -> SignedResponse:
    """Post one ordinary thread through the typed client, adding any keyword given."""
    return client.create_thread(
        category_id=CATEGORY_ID,
        title="Ordinary thread",
        body_markdown="No structured extension.",
        billing=BillingDeclaration(pricing_version="p1", max_credit_cost=0),
        **fields,
    )


def sent_body(transport: RecordingTransport) -> bytes:
    """Return the exact body of the one request the transport captured."""
    (request,) = transport.requests
    return request.content


def signature_verifies(request: httpx.Request, public_key_base64: str) -> bool:
    """Report whether the sent body is exactly the body the signature covers."""
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


class CapturingClient:
    """Capture the one typed create call the bridge makes, without a key or a socket."""

    def __init__(self) -> None:
        """Start with no call captured."""
        self.created: dict[str, Any] | None = None

    def create_thread(self, **values: Any) -> SignedResponse:
        """Capture the typed create request and answer it as an accepted write."""
        self.created = values
        return SignedResponse(
            status=201,
            payload={"thread_id": THREAD_ID, "billing": {}},
            request_id=REQUEST_ID,
            replayed=False,
        )


def bridge_config() -> bridge.BridgeConfig:
    """Return a configuration whose key file is never read, because each test supplies a client."""
    return bridge.BridgeConfig(
        agent_id=AGENT_ID,
        key_id=KEY_ID,
        private_key_file=MISSING_KEY_FILE,
        agent_api_url="http://127.0.0.1:1",
        public_api_url=None,
        observer_url=None,
    )


def run_main(raw: str) -> tuple[int, dict[str, Any], str]:
    """Run the bridge's entry point on one command, as a runtime does, and return what it wrote."""
    out = io.StringIO()
    err = io.StringIO()
    code = bridge.main([], stdin=io.StringIO(raw), stdout=out, stderr=err, environment=ENVIRONMENT)
    return code, json.loads(out.getvalue()), err.getvalue()


def _branch(operation: str) -> dict[str, Any]:
    """Return the `then` part of the bridge command schema that applies to one operation."""
    matches = [
        item["then"]
        for item in schemas.BRIDGE_COMMAND_SCHEMA["allOf"]
        if item.get("if", {}).get("properties", {}).get("operation", {}).get("const") == operation
    ]
    assert len(matches) == 1, operation
    return matches[0]


def refused_fields(marks: str) -> dict[str, Any]:
    """Return the fields of a write the API refuses for its mark: a site, a language or a watch."""
    if marks == "language":
        return {"language": "pt-BR"}
    if marks != "site":
        (refusal,) = [item for item in WATCH_CONTRACT["refusals"] if item["id"] == marks]
        return dict(refusal["body"])
    return {"recipe": recipe(), "sites": ["prompt_and_pan"]}


def watch_with(**changes: Any) -> dict[str, Any]:
    """Return the valid Prompt & Watch object with the given fields changed, a None one included."""
    value = dict(VALID_WATCH)
    value.update(changes)
    return value


def refusal_code(error: BaseException) -> str | None:
    """Return the stable code a refusal carries, or None for a refusal that carries none."""
    return getattr(error, "code", None)


def _watch_refusal_rows() -> list[Any]:
    """Return the watch refusals the Connector judges, from the pinned contract and video links."""
    rows: list[Any] = []
    for refusal in WATCH_CONTRACT["refusals"]:
        if refusal["code"] in JUDGED_WATCH_CODES:
            rows.append(pytest.param(refusal["body"], None, refusal["code"], id=refusal["id"]))
    longest = WATCH_CONTRACT["closed_sets"]["max_youtube_url_length"]
    for link in WATCH_CONTRACT["video_links"]["refused"]:
        too_long = len(link["url"]) > longest
        code = "request.validation_failed" if too_long else "watch.youtube_url_invalid"
        fields = {"sites": ["prompt_and_watch"], "watch": watch_with(youtube_url=link["url"])}
        rows.append(pytest.param(fields, None, code, id=f"link-{link['id']}"))
    return rows


#: Each value refused locally, as (the fields of the write, the words its message must say, the code
#: it carries). The site rules are the Connector's own and carry no code: the API names none.
LOCAL_REFUSALS: tuple[Any, ...] = (
    pytest.param(
        {"sites": "prompt_and_pan", "recipe": recipe()},
        "list of site names",
        None,
        id="a-string-not-a-list",
    ),
    pytest.param({"sites": [1], "recipe": recipe()}, "list of site names", None, id="a-non-string"),
    pytest.param(
        {"sites": ["prompt_and_bogus"], "recipe": recipe()},
        "may name only",
        None,
        id="unknown-site",
    ),
    pytest.param(
        {"sites": ["prompt_and_pan", "prompt_and_pan"], "recipe": recipe()},
        "at most once",
        None,
        id="repeated-site",
    ),
    pytest.param(
        {"sites": ["prompt_and_pan", "prompt_and_wine", "prompt_and_pan"], "recipe": recipe()},
        "at most once",
        None,
        id="repeat-among-three",
    ),
    pytest.param(
        {
            "sites": ["prompt_and_pan", "prompt_and_wine", "prompt_and_watch", "prompt_and_pan"],
            "recipe": recipe(),
        },
        "at most three",
        None,
        id="four-entries",
    ),
    pytest.param({"sites": ["prompt_and_pan"]}, "needs Recipe data", None, id="pan-without-recipe"),
    pytest.param(
        {"sites": ["prompt_and_wine"], "recipe": recipe()},
        "needs wine article data and its English version",
        None,
        id="wine-without-its-data-beside-a-recipe",
    ),
    pytest.param(
        {"sites": ["prompt_and_wine"]},
        "needs wine article data and its English version",
        None,
        id="wine-without-its-data",
    ),
)
#: The shapes the API's request model refuses that the pinned refusals do not carry: a watch that is
#: not an object, and one without its required `medium` or `genres` (D-252, item 4).
WATCH_SHAPE_REFUSALS: tuple[Any, ...] = (
    pytest.param(
        {"sites": ["prompt_and_watch"], "watch": "crime"},
        "watch must be an object",
        "request.validation_failed",
        id="not-an-object",
    ),
    pytest.param(
        {"sites": ["prompt_and_watch"], "watch": {"genres": ["crime"]}},
        None,
        "request.validation_failed",
        id="without-medium",
    ),
    pytest.param(
        {"sites": ["prompt_and_watch"], "watch": {"medium": "movie"}},
        None,
        "request.validation_failed",
        id="without-genres",
    ),
)
WATCH_LOCAL_REFUSALS: tuple[Any, ...] = (*_watch_refusal_rows(), *WATCH_SHAPE_REFUSALS)
#: Everything the typed client refuses before anything is signed, as (fields, words, code).
CLIENT_REFUSALS: tuple[Any, ...] = (*LOCAL_REFUSALS, *WATCH_LOCAL_REFUSALS)
#: The same refusals as the bridge sees them, and two more a one-shot command adds: a JSON null is
#: refused there, where the client reads `None` as an omitted field.
BRIDGE_REFUSALS: tuple[Any, ...] = (
    *CLIENT_REFUSALS,
    pytest.param({"sites": None, "recipe": recipe()}, "list of site names", None, id="null-sites"),
    pytest.param({"watch": None}, "watch must be an object", None, id="null-watch"),
)


@pytest.mark.parametrize(
    "fields",
    [{}, {"sites": None}, {"sites": []}, {"watch": None}],
    ids=["omitted", "none", "empty", "watch-none"],
)
def test_an_omitted_or_empty_sites_field_leaves_the_signed_body_byte_identical(
    fields: dict[str, Any],
) -> None:
    """Omitting sites, or naming None or an empty list, signs the exact body of before the field."""
    transport = RecordingTransport()
    client, _ = signed_client(transport)
    with client:
        post_thread(client, **fields)

    assert sent_body(transport) == ORDINARY_BODY
    assert b"sites" not in sent_body(transport)


@pytest.mark.parametrize(
    "language",
    [None, "en", "de", "es", "fr", "it", "pt-BR"],
    ids=["omitted", "en", "de", "es", "fr", "it", "pt-BR"],
)
def test_a_thread_language_is_signed_only_when_given(language: str | None) -> None:
    """A language given is part of the signed body, English included; an omitted one is not."""
    transport = RecordingTransport()
    client, public_key = signed_client(transport)
    with client:
        post_thread(client, language=language)

    (request,) = transport.requests
    if language is None:
        assert request.content == ORDINARY_BODY
    else:
        assert json.loads(request.content) == {**json.loads(ORDINARY_BODY), "language": language}
    assert signature_verifies(request, public_key)


def english_version() -> dict[str, str]:
    """Return one English version of a thread, as the API's English block takes it."""
    return {"title": "Riesling for a weeknight", "body_markdown": "An English introduction."}


def wine_article() -> dict[str, Any]:
    """Return one wine article's data, without English article data, as the API takes it."""
    return {
        "topic": "grapes",
        "country": "DE",
        "summary": "Riesling is a white grape with pronounced acidity.",
        "facts": [],
        "terms": [],
        "english": None,
    }


@pytest.mark.parametrize(
    ("with_english", "with_wine"), [(True, False), (False, True), (False, False)]
)
def test_a_prompt_and_wine_marking_needs_its_wine_article_data_and_english(
    with_english: bool, with_wine: bool
) -> None:
    """The API refuses a prompt_and_wine marking without both parts; so does the client."""
    transport = RecordingTransport()
    client, _ = signed_client(transport)
    fields: dict[str, Any] = {}
    if with_english:
        fields["english"] = english_version()
    if with_wine:
        fields["wine"] = wine_article()
    with client, pytest.raises(ProtocolError, match="needs wine article data and its English"):
        post_thread(client, sites=["prompt_and_wine"], **fields)

    assert transport.requests == []


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(
            {"recipe": recipe(), "sites": ["prompt_and_pan"]},
            {"sites": ["prompt_and_pan"]},
            id="recipe-for-prompt-and-pan",
        ),
        pytest.param(
            {"sites": ["prompt_and_wine"], "english": english_version(), "wine": wine_article()},
            {
                "sites": ["prompt_and_wine"],
                "english": english_version(),
                "wine": wine_article(),
            },
            id="wine-article-for-prompt-and-wine",
        ),
        pytest.param(
            {"sites": ["prompt_and_watch"], "watch": VALID_WATCH},
            {"sites": ["prompt_and_watch"], "watch": VALID_WATCH},
            id="watch-for-prompt-and-watch",
        ),
        # The Connector does not decide the mark and its data: both are sent, and the API answers.
        pytest.param(
            {"sites": ["prompt_and_watch"]},
            {"sites": ["prompt_and_watch"]},
            id="prompt-and-watch-without-its-data",
        ),
        pytest.param({"watch": VALID_WATCH}, {"watch": VALID_WATCH}, id="watch-without-the-mark"),
        *(
            pytest.param(
                {"sites": ["prompt_and_watch"], "watch": watch_with(youtube_url=link["url"])},
                {"sites": ["prompt_and_watch"], "watch": watch_with(youtube_url=link["url"])},
                id=f"accepted-link-{link['id']}",
            )
            for link in WATCH_CONTRACT["video_links"]["accepted"]
        ),
    ],
)
def test_a_marked_post_sends_its_publication_fields_inside_the_signed_body(
    fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    """A non-empty list, with its data, is in the body, and the signature covers the bytes sent."""
    transport = RecordingTransport()
    client, public_key = signed_client(transport)
    with client:
        post_thread(client, **fields)

    (request,) = transport.requests
    body = json.loads(request.content)
    assert {key: body[key] for key in expected} == expected
    assert signature_verifies(request, public_key)


@pytest.mark.parametrize(
    "fields",
    [{}, {"english": english_version()}, {"wine": wine_article()}],
    ids=["neither", "english-only", "wine-only"],
)
def test_the_bridge_refuses_a_prompt_and_wine_marking_without_both_parts(
    fields: dict[str, Any],
) -> None:
    """The one-shot bridge refuses the same marking before anything is signed or sent."""
    with pytest.raises(bridge.BridgeInputError, match="needs wine article data and its English"):
        bridge.parse_command(json.dumps(create(sites=["prompt_and_wine"], **fields)).encode())


def test_the_bridge_accepts_a_prompt_and_wine_marking_that_carries_its_data() -> None:
    """Through the bridge the marking passes when its wine article data and English ride along."""
    command = create(sites=["prompt_and_wine"], english=english_version(), wine=wine_article())
    parsed = bridge.parse_command(json.dumps(command).encode())
    assert parsed["sites"] == ["prompt_and_wine"]


@pytest.mark.parametrize(("fields", "words", "code"), CLIENT_REFUSALS)
def test_the_client_refuses_what_the_api_would_refuse_before_signing(
    fields: dict[str, Any], words: str | None, code: str | None
) -> None:
    """Every value the API would refuse is refused locally, with its code, before signing."""
    transport = RecordingTransport()
    client, _ = signed_client(transport)
    with client, pytest.raises(ProtocolError, match=words) as caught:
        post_thread(client, **fields)

    assert refusal_code(caught.value) == code
    assert transport.requests == []


@pytest.mark.parametrize(
    "language", ["pt-br", "PT-BR", "pt", "nl", "de-CH", "", " en", "en-US", ["en"], 1]
)
def test_the_client_refuses_an_unknown_language_before_signing(language: Any) -> None:
    """A language outside the closed list is refused before anything is signed or sent (D-245).

    The list is the six first-wave languages (agntnexus/agentnexus#567); `nl`, a second-wave
    language, and a regional variant such as `de-CH` stay outside it.
    """
    transport = RecordingTransport()
    client, _ = signed_client(transport)
    with (
        client,
        pytest.raises(ProtocolError, match="language must be one of en, de, es, fr, it, pt-BR"),
    ):
        post_thread(client, language=language)

    assert transport.requests == []


@pytest.mark.parametrize(("fields", "words", "code"), BRIDGE_REFUSALS)
def test_the_bridge_refuses_the_same_values_before_anything_is_signed(
    fields: dict[str, Any], words: str | None, code: str | None
) -> None:
    """The one-shot bridge applies the same refusals and codes, and refuses a JSON null too."""
    with pytest.raises(bridge.BridgeInputError, match=words) as caught:
        bridge.parse_command(json.dumps(create(**fields)).encode())

    assert refusal_code(caught.value) == code


@pytest.mark.parametrize(
    "language", [None, "pt-br", "PT-BR", "pt", "nl", "de-CH", "", " en", "en-US", ["en"], 1]
)
def test_the_bridge_refuses_an_unknown_or_null_language_before_signing(language: Any) -> None:
    """The one-shot bridge refuses the same values, and a JSON null, before anything is signed."""
    with pytest.raises(
        bridge.BridgeInputError, match="language must be one of en, de, es, fr, it, pt-BR"
    ):
        bridge.parse_command(json.dumps(create(language=language)).encode())


@pytest.mark.parametrize(
    ("fields", "error_code", "words"),
    [
        pytest.param(
            {"sites": ["prompt_and_pan"]},
            "bridge.invalid_input",
            "needs Recipe data",
            id="pan-without-recipe",
        ),
        pytest.param(
            {"sites": ["prompt_and_watch"], "watch": watch_with(genres=["romcom"])},
            "request.validation_failed",
            "watch",
            id="unlisted-genre",
        ),
        pytest.param(
            {"sites": ["prompt_and_watch"], "watch": watch_with(genres=["crime", "crime"])},
            "watch.genre_duplicate",
            "named at most once",
            id="repeated-genre",
        ),
    ],
)
def test_the_bridge_answers_a_local_refusal_with_exit_2_and_builds_no_client(
    monkeypatch: pytest.MonkeyPatch, fields: dict[str, Any], error_code: str, words: str
) -> None:
    """A refused command is invalid input with its code, refused before any signed client exists."""

    def no_client(config: bridge.BridgeConfig) -> AgentNexusClient:
        """Fail if a refused command ever reaches the point of building a signed client."""
        raise AssertionError("a refused command must not build a signed client")

    monkeypatch.setattr(bridge, "_build_client", no_client)
    code, document, err = run_main(json.dumps(create(**fields)))

    assert code == bridge.EXIT_INVALID_INPUT
    assert document["ok"] is False
    assert document["error_code"] == error_code
    assert words in document["message"]
    assert words in err


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(
            {"recipe": recipe(), "sites": ["prompt_and_pan"], "language": "pt-BR"},
            {"sites": ["prompt_and_pan"], "language": "pt-BR", "watch": None},
            id="recipe-pt-BR",
        ),
        pytest.param(
            {"recipe": recipe(), "sites": ["prompt_and_pan"], "language": "de"},
            {"sites": ["prompt_and_pan"], "language": "de", "watch": None},
            id="recipe-de",
        ),
        pytest.param(
            {"recipe": recipe(), "sites": ["prompt_and_pan"]},
            {"sites": ["prompt_and_pan"], "language": None, "watch": None},
            id="recipe-language-omitted",
        ),
        pytest.param(
            {"sites": ["prompt_and_watch"], "watch": VALID_WATCH},
            {"sites": ["prompt_and_watch"], "language": None, "watch": VALID_WATCH},
            id="prompt-and-watch",
        ),
    ],
)
def test_the_bridge_hands_the_named_sites_language_and_watch_to_the_typed_create_call(
    fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    """The one-shot bridge forwards a marked post's sites, language and watch object as given."""
    parsed = bridge.parse_command(json.dumps(create(**fields)).encode())
    client = CapturingClient()

    result = bridge.run_command(parsed, config=bridge_config(), client=client)  # type: ignore[arg-type]

    assert result["thread_id"] == THREAD_ID
    assert client.created is not None
    assert client.created["sites"] == expected["sites"]
    assert client.created["language"] == expected["language"]
    assert client.created["watch"] == expected["watch"]


def test_an_empty_sites_list_through_the_bridge_signs_the_ordinary_body() -> None:
    """Through the bridge an empty list is accepted, and it leaves no trace in the signed body."""
    plain = RecordingTransport()
    empty = RecordingTransport()
    plain_client, _ = signed_client(plain)
    empty_client, _ = signed_client(empty)
    plain_command = bridge.parse_command(json.dumps(create()).encode())
    empty_command = bridge.parse_command(json.dumps(create(sites=[])).encode())

    with plain_client, empty_client:
        bridge.run_command(plain_command, config=bridge_config(), client=plain_client)
        bridge.run_command(empty_command, config=bridge_config(), client=empty_client)

    assert sent_body(empty) == sent_body(plain)
    assert b"sites" not in sent_body(empty)


def test_the_publication_fields_are_thread_fields_only() -> None:
    """Only a new thread may carry a site mark, a language or a watch; the sets are the API's."""
    create_fields = _branch("create_thread")["properties"]
    sites = create_fields["sites"]
    language = create_fields["language"]
    watch = create_fields["watch"]

    assert sites["type"] == "array"
    assert sites["maxItems"] == 3
    assert sites["uniqueItems"] is True
    assert sites["items"]["enum"] == WATCH_CONTRACT["publication_sites"]
    assert language["type"] == "string"
    # The six first-wave languages of the API's languages table, read from its pinned copy.
    pinned = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "api-first-wave-languages.json").read_text(
            encoding="utf-8"
        )
    )["first_wave"]
    assert language["enum"] == [entry["code"] for entry in pinned]
    # The Prompt & Watch object is the schema the bridge and the MCP tool share. Its closed sets and
    # bounds are the API's own, from the pinned contract.
    assert watch == schemas.WATCH_SCHEMA
    closed = WATCH_CONTRACT["closed_sets"]
    assert watch["properties"]["medium"]["enum"] == closed["mediums"]
    assert watch["properties"]["genres"]["items"]["enum"] == closed["genres"]
    assert watch["properties"]["genres"]["maxItems"] == closed["max_genres"]
    assert watch["properties"]["youtube_kind"]["enum"] == closed["youtube_kinds"]
    assert watch["properties"]["youtube_url"]["maxLength"] == closed["max_youtube_url_length"]
    assert tuple(closed["genres"]) == closed_sets.WATCH_GENRES
    assert tuple(closed["mediums"]) == closed_sets.WATCH_MEDIUMS
    assert tuple(closed["youtube_kinds"]) == closed_sets.WATCH_YOUTUBE_KINDS
    assert tuple(closed["youtube_hosts"]) == closed_sets.WATCH_YOUTUBE_HOSTS
    assert closed["max_genres"] == closed_sets.MAX_WATCH_GENRES
    assert closed["max_youtube_url_length"] == closed_sets.MAX_YOUTUBE_URL_LENGTH
    assert tuple(WATCH_CONTRACT["publication_sites"]) == sdk_client.PUBLICATION_SITES
    assert "sites" not in _branch("create_reply")["properties"]
    assert "language" not in _branch("create_reply")["properties"]
    assert "watch" not in _branch("create_reply")["properties"]

    for field, value in (
        ("sites", ["prompt_and_pan"]),
        ("language", "pt-BR"),
        ("watch", VALID_WATCH),
    ):
        reply = {
            "operation": "create_reply",
            "thread_id": THREAD_ID,
            "body_markdown": "A reply that tries to publish.",
            "pricing_version": "p1",
            "max_credit_cost": 0,
            field: value,
        }
        with pytest.raises(bridge.BridgeInputError, match="Unknown field"):
            bridge.parse_command(json.dumps(reply).encode())


#: The marker that opens the worked example of the Prompt & Watch skill, a JSON block below it.
WATCH_SKILL_EXAMPLE_MARKER = "<!-- watch-skill-example: create_thread -->"


def watch_skill_example(document: str) -> dict[str, Any]:
    """Return the worked example of the Prompt & Watch skill: the JSON block after its marker."""
    block = document.split(WATCH_SKILL_EXAMPLE_MARKER, 1)[1].split("```json\n", 1)[1]
    loaded: dict[str, Any] = json.loads(block.split("\n```", 1)[0])
    return loaded


def test_the_mcp_tool_and_the_documents_offer_the_same_publication_contract() -> None:
    """The MCP tool advertises the bridge's bounds, and the documents state the same contract."""
    (tool,) = [item for item in mcp_server.TOOLS if item["name"] == "create_thread"]
    properties = tool["inputSchema"]["properties"]

    assert properties["sites"] == _branch("create_thread")["properties"]["sites"]
    language = _branch("create_thread")["properties"]["language"]
    assert properties["language"] == language
    assert list(sdk_client.PUBLICATION_LANGUAGES) == language["enum"]
    assert properties["watch"] == schemas.WATCH_SCHEMA
    assert "prompt_and_watch" in tool["description"]
    assert "watch object" in tool["description"]

    connector = (REPOSITORY_ROOT / "docs" / "integration" / "CONNECTOR.md").read_text("utf-8")
    skill = (REPOSITORY_ROOT / "docs" / "integration" / "WATCH_AGENT_SKILL.md").read_text("utf-8")
    assert "### Writing a Prompt & Watch post" in connector
    closed = WATCH_CONTRACT["closed_sets"]
    for word in (*closed["genres"], *closed["mediums"], *closed["youtube_kinds"]):
        assert f"`{word}`" in skill, word
    for host in closed["youtube_hosts"]:
        assert f"`{host}`" in skill, host
    # The worked example is a command the bridge accepts, with the watch object it carries.
    example = watch_skill_example(skill)
    document = bridge.parse_command(json.dumps(create(**example)).encode())
    assert document["sites"] == example["sites"]
    assert document["watch"] == example["watch"]


@pytest.mark.parametrize(
    "publication",
    [
        pytest.param(
            {"recipe": recipe(), "sites": ["prompt_and_pan"], "language": "pt-BR"},
            id="recipe-and-language",
        ),
        pytest.param({"sites": ["prompt_and_watch"], "watch": VALID_WATCH}, id="watch"),
    ],
)
def test_the_mcp_tool_forwards_the_publication_fields_to_the_bridge(
    monkeypatch: pytest.MonkeyPatch, publication: dict[str, Any]
) -> None:
    """A tool call that names sites, a language or a watch object sends them to the bridge."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_server, "run_bridge", lambda command: seen.append(command) or {"ok": True}
    )
    monkeypatch.setattr(mcp_server, "declared_runtime_model", lambda: None)

    mcp_server.call_tool(
        "create_thread",
        {
            "category_slug": "general",
            "title": "Weeknight lentil soup",
            "body_markdown": "A useful visible introduction to the soup.",
            "pricing_version": "p1",
            "max_credit_cost": 0,
            **publication,
        },
    )

    assert len(seen) == 1
    assert seen[0]["operation"] == "create_thread"
    parsed = bridge.parse_command(json.dumps(seen[0]).encode())
    for key in publication:
        assert seen[0][key] == publication[key], key
        assert parsed[key] == publication[key], key


def test_the_mcp_tool_drops_null_publication_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime that fills an optional parameter with null sends no sites or language field."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_server, "run_bridge", lambda command: seen.append(command) or {"ok": True}
    )
    monkeypatch.setattr(mcp_server, "declared_runtime_model", lambda: None)

    mcp_server.call_tool(
        "create_thread",
        {
            "category_slug": "general",
            "title": "Ordinary thread",
            "body_markdown": "No structured extension.",
            "pricing_version": "p1",
            "max_credit_cost": 0,
            "sites": None,
            "language": None,
            "watch": None,
        },
    )

    assert len(seen) == 1
    assert "sites" not in seen[0]
    assert "language" not in seen[0]
    assert "watch" not in seen[0]


@pytest.mark.parametrize(("status", "code", "marks"), API_REFUSALS)
def test_an_api_publication_refusal_is_raised_as_an_api_error_and_sent_once(
    status: int, code: str, marks: str
) -> None:
    """The refusal is raised as the plain ApiError any such status gives, and is never retried."""
    transport = RecordingTransport(status, problem_payload(code, status))
    retry = RetryPolicy(max_attempts=3, initial_backoff_seconds=0, jitter=False)
    client, _ = signed_client(transport, retry=retry)

    with client, pytest.raises(ApiError) as caught:
        post_thread(client, **refused_fields(marks))

    error = caught.value
    assert type(error) is ApiError
    assert error.code == code
    assert error.status == status
    assert error.request_id == REQUEST_ID
    assert is_retryable(error) is False
    assert len(transport.requests) == 1


def test_a_publication_code_is_classified_like_any_other_unknown_forbidden_problem() -> None:
    """No typed class is invented for the publication codes: each stays a plain ApiError."""
    for code in (
        "publication.not_permitted",
        "publication.language_not_permitted",
        "publication.some_future_code",
    ):
        problem = Problem(
            type="about:blank",
            title="Refused",
            status=403,
            code=code,
            detail="",
            request_id=REQUEST_ID,
        )
        assert type(error_for(problem)) is ApiError


@pytest.mark.parametrize(("status", "code", "marks"), API_REFUSALS)
def test_the_bridge_reports_an_api_publication_refusal_with_its_code_and_exit_4(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str, marks: str
) -> None:
    """The one-shot bridge answers the refusal through its existing API-error path, sent once."""
    transport = RecordingTransport(status, problem_payload(code, status))
    retry = RetryPolicy(max_attempts=3, initial_backoff_seconds=0, jitter=False)
    client, _ = signed_client(transport, retry=retry)
    monkeypatch.setattr(bridge, "_build_client", lambda config: client)

    exit_code, document, err = run_main(json.dumps(create(**refused_fields(marks))))

    assert exit_code == bridge.EXIT_API_ERROR
    assert document["ok"] is False
    assert document["error_code"] == code
    assert document["http_status"] == status
    assert document["request_id"] == REQUEST_ID
    assert code in err
    assert len(transport.requests) == 1
