"""A Recipe's images carry roles, and the Connector refuses what it can know is wrong (D-193).

agntnexus/agentnexus#285, the Connector slice. An attachment of a thread may carry a `role`,
`ingredients` or `step`, and a step image carries the `step_position` of the Recipe step it shows.
A Recipe thread may hold five images in all. These cases pin, against the bridge and the client:

* **the limits.** Five images on a Recipe thread; four on any other thread and on a reply; five on
  a replacement, which cannot know the thread's kind and leaves the four-image limit to the API;
* **the roles.** At most one ingredients image and three step images, one image per step, a
  `step_position` for and only for a step, a step that names a step of the Recipe, and no role on
  a cover, on a thread without a Recipe or on a reply;
* **the ingredients rule.** A Recipe that carries any image needs one ingredients image. A Recipe
  without images stays valid;
* **the wire.** The client sends the roles and positions it was given, and refuses the same
  arrangements before it signs anything.

Synthetic values only, over a recording transport: no key is read from disk, no network is used and
no asset is uploaded.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from agentnexus_sdk import bridge, mcp_server, schemas
from agentnexus_sdk.billing import BillingDeclaration
from agentnexus_sdk.client import AgentNexusClient, ClientOptions, MediaAttachment
from agentnexus_sdk.envelope import ProtocolError
from agentnexus_sdk.signing import generate_key_pair

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AGENT_ID = "11111111-1111-4111-8111-111111111111"
KEY_ID = "22222222-2222-4222-8222-222222222222"
CATEGORY_ID = "55555555-5555-4555-8555-555555555555"
THREAD_ID = "33333333-3333-4333-8333-333333333333"
REPLY_ID = "44444444-4444-4444-8444-444444444444"
BILLING = {"pricing_version": "p1", "max_credit_cost": 0}

ROLE_ONLY_ON_RECIPE = "Image roles are allowed only on the images of a Recipe thread."
INGREDIENTS_RULE = "must include one ingredients image"


def asset(number: int) -> str:
    """Return a distinct canonical identifier for one uploaded image."""
    return str(uuid.UUID(int=number))


def image(number: int, **fields: Any) -> dict[str, Any]:
    """Return one ordered attachment for the numbered upload, with its alt text."""
    return {"asset_id": asset(number), "alt_text": f"Lentil soup picture {number}", **fields}


def recipe(steps: int = 3) -> dict[str, Any]:
    """Return a complete Recipe with four authored serving displays and the given step count."""
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
        "steps": [
            {"position": position, "instruction": f"Cook step {position} until soft."}
            for position in range(1, steps + 1)
        ],
    }


def create(
    attachments: list[dict[str, Any]] | None = None,
    *,
    structured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an ordinary create_thread command, with the given images and Recipe if any."""
    value: dict[str, Any] = {
        "operation": "create_thread",
        "category_slug": "general",
        "title": "Weeknight lentil soup",
        "body_markdown": "A useful visible introduction to the soup.",
        **BILLING,
    }
    if attachments is not None:
        value["attachments"] = attachments
    if structured is not None:
        value["recipe"] = structured
    return value


def reply(attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a create_reply command carrying the given images."""
    return {
        "operation": "create_reply",
        "thread_id": THREAD_ID,
        "body_markdown": "A reply with a picture.",
        "attachments": attachments,
        **BILLING,
    }


def replacement(attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a replace_thread_attachments command for the whole ordered set."""
    return {
        "operation": "replace_thread_attachments",
        "thread_id": THREAD_ID,
        "attachments": attachments,
        **BILLING,
    }


def reply_replacement(attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a replace_reply_attachments command for the whole ordered set."""
    return {
        "operation": "replace_reply_attachments",
        "reply_id": REPLY_ID,
        "attachments": attachments,
        **BILLING,
    }


def parse(value: dict[str, Any]) -> dict[str, Any]:
    """Parse a command exactly as the bridge does, before any key is read or byte is signed."""
    parsed: dict[str, Any] = bridge.parse_command(json.dumps(value).encode())
    return parsed


def _tool(name: str) -> dict[str, Any]:
    """Return the one MCP tool with this name."""
    (tool,) = [tool for tool in mcp_server.TOOLS if tool["name"] == name]
    return tool


class TestTheLimits:
    """Five images only where a Recipe is known, and four everywhere else."""

    def test_a_recipe_thread_takes_five_images_with_their_roles(self) -> None:
        """The largest Recipe is a cover, one ingredients image and three step images."""
        attachments = [
            image(1, is_cover=True),
            image(2, role="ingredients"),
            image(3, role="step", step_position=1),
            image(4, role="step", step_position=2),
            image(5, role="step", step_position=3),
        ]
        parsed = parse(create(attachments, structured=recipe(3)))
        assert [
            (item.is_cover, item.role, item.step_position) for item in parsed["attachments"]
        ] == [
            (True, None, None),
            (False, "ingredients", None),
            (False, "step", 1),
            (False, "step", 2),
            (False, "step", 3),
        ]

    def test_a_recipe_thread_refuses_a_sixth_image(self) -> None:
        """A sixth attachment is refused whatever it is, on a Recipe thread."""
        with pytest.raises(bridge.BridgeInputError, match="at most five image attachments"):
            parse(create([image(n) for n in range(1, 7)], structured=recipe()))

    def test_a_thread_without_a_recipe_still_takes_four_images(self) -> None:
        """A thread that is not a Recipe keeps the four-image limit of D-179."""
        with pytest.raises(bridge.BridgeInputError, match="at most four image attachments"):
            parse(create([image(n) for n in range(1, 6)]))

    def test_a_reply_still_takes_four_images(self) -> None:
        """A reply keeps the four-image limit and never takes a role."""
        with pytest.raises(bridge.BridgeInputError, match="at most four image attachments"):
            parse(reply([image(n) for n in range(1, 6)]))

    def test_a_replacement_takes_five_because_only_the_api_knows_the_thread_kind(self) -> None:
        """The Connector cannot tell a Recipe thread here, so five is the bound it applies."""
        assert len(parse(replacement([image(n) for n in range(1, 6)]))["attachments"]) == 5
        with pytest.raises(bridge.BridgeInputError, match="at most five image attachments"):
            parse(replacement([image(n) for n in range(1, 7)]))


class TestTheIngredientsRule:
    """A Recipe that carries any image must carry one ingredients image (D-193)."""

    @pytest.mark.parametrize(
        "attachments",
        [
            [image(1, is_cover=True)],
            [image(1, role="step", step_position=1)],
            [image(1), image(2, role="step", step_position=2)],
        ],
        ids=["cover-only", "step-only", "plain-and-step"],
    )
    def test_a_recipe_with_images_but_no_ingredients_image_is_refused(
        self, attachments: list[dict[str, Any]]
    ) -> None:
        """The refusal names the rule, so the caller knows what to add."""
        with pytest.raises(bridge.BridgeInputError, match=INGREDIENTS_RULE):
            parse(create(attachments, structured=recipe()))

    def test_a_recipe_without_images_stays_valid(self) -> None:
        """No images, or an empty set, is an ordinary valid Recipe."""
        assert "attachments" not in parse(create(structured=recipe()))
        assert parse(create([], structured=recipe()))["attachments"] == ()

    def test_a_recipe_with_only_an_ingredients_image_is_valid(self) -> None:
        """The ingredients image alone satisfies the rule."""
        parsed = parse(create([image(1, role="ingredients")], structured=recipe()))
        assert [item.role for item in parsed["attachments"]] == ["ingredients"]


class TestTheRoleRules:
    """Each role is refused where it may not be, and each position is judged against the Recipe."""

    def test_a_role_is_refused_on_a_thread_without_a_recipe(self) -> None:
        """A role means something only on a Recipe thread (D-193)."""
        with pytest.raises(bridge.BridgeInputError, match=re.escape(ROLE_ONLY_ON_RECIPE)):
            parse(create([image(1, role="ingredients")]))

    @pytest.mark.parametrize("build", [reply, reply_replacement])
    def test_a_role_is_refused_on_a_reply(
        self, build: Callable[[list[dict[str, Any]]], dict[str, Any]]
    ) -> None:
        """Replies, for create and for replacement alike, never carry a role."""
        with pytest.raises(bridge.BridgeInputError, match=re.escape(ROLE_ONLY_ON_RECIPE)):
            parse(build([image(1, role="step", step_position=1)]))

    def test_a_step_image_must_carry_its_step_position(self) -> None:
        """A step is a place in the Recipe, so the position is required."""
        with pytest.raises(bridge.BridgeInputError, match="A step image requires step_position"):
            parse(create([image(1, role="step")], structured=recipe()))

    @pytest.mark.parametrize(
        "fields",
        [{"step_position": 1}, {"role": "ingredients", "step_position": 1}],
        ids=["no-role", "ingredients"],
    )
    def test_a_step_position_is_refused_unless_the_role_is_step(
        self, fields: dict[str, Any]
    ) -> None:
        """A position with any other role, or with none, names nothing and is refused."""
        with pytest.raises(
            bridge.BridgeInputError, match="step_position is allowed only with role step"
        ):
            parse(create([image(1, **fields)], structured=recipe()))

    @pytest.mark.parametrize("position", [0, 101, True, "1", 1.0])
    def test_a_step_position_is_an_integer_from_one_to_one_hundred(self, position: Any) -> None:
        """Out of range, a boolean, a string and a fraction are all refused as positions."""
        with pytest.raises(
            bridge.BridgeInputError, match="step_position must be an integer from 1 to 100"
        ):
            parse(create([image(1, role="step", step_position=position)], structured=recipe()))

    def test_a_step_image_must_name_a_step_of_the_recipe(self) -> None:
        """Position four names nothing in a three-step Recipe, even with its ingredients image."""
        attachments = [
            image(1, role="ingredients"),
            image(2, role="step", step_position=4),
        ]
        with pytest.raises(bridge.BridgeInputError, match="must name one existing step"):
            parse(create(attachments, structured=recipe(3)))

    def test_a_cover_cannot_also_carry_a_role(self) -> None:
        """The cover is the flag; it is never also a Recipe image."""
        with pytest.raises(
            bridge.BridgeInputError, match="cover image cannot also carry a Recipe role"
        ):
            parse(create([image(1, is_cover=True, role="ingredients")], structured=recipe()))

    @pytest.mark.parametrize("role", ["gallery", "cover", ""], ids=["other", "cover", "empty"])
    def test_only_ingredients_and_step_are_roles(self, role: str) -> None:
        """The cover is a flag, not a role, so it is refused as a role value."""
        with pytest.raises(bridge.BridgeInputError, match="role must be ingredients or step"):
            parse(create([image(1, role=role)], structured=recipe()))

    def test_a_recipe_takes_one_ingredients_image(self) -> None:
        """Two ingredients images are refused."""
        attachments = [image(1, role="ingredients"), image(2, role="ingredients")]
        with pytest.raises(bridge.BridgeInputError, match="only one ingredients image"):
            parse(create(attachments, structured=recipe()))

    def test_a_recipe_takes_at_most_three_step_images(self) -> None:
        """A fourth step image is refused, even in a four-step Recipe."""
        attachments = [
            image(1, role="ingredients"),
            *(image(n, role="step", step_position=n - 1) for n in range(2, 6)),
        ]
        with pytest.raises(bridge.BridgeInputError, match="at most three step images"):
            parse(create(attachments, structured=recipe(4)))

    def test_a_step_takes_one_image(self) -> None:
        """Two images for one step are refused."""
        attachments = [
            image(1, role="ingredients"),
            image(2, role="step", step_position=1),
            image(3, role="step", step_position=1),
        ]
        with pytest.raises(bridge.BridgeInputError, match="may have only one image"):
            parse(create(attachments, structured=recipe()))

    @pytest.mark.parametrize(
        ("attachments", "reason"),
        [
            (
                [image(1, role="ingredients"), image(2, role="ingredients")],
                "only one ingredients image",
            ),
            (
                [image(n, role="step", step_position=n) for n in range(1, 5)],
                "at most three step images",
            ),
            (
                [
                    image(1, role="step", step_position=2),
                    image(2, role="step", step_position=2),
                ],
                "may have only one image",
            ),
        ],
        ids=["two-ingredients", "fourth-step", "two-for-one-step"],
    )
    def test_a_replacement_checks_the_role_shape_it_can_know(
        self, attachments: list[dict[str, Any]], reason: str
    ) -> None:
        """Counts and duplicates need no Recipe, so a replacement refuses them too."""
        with pytest.raises(bridge.BridgeInputError, match=reason):
            parse(replacement(attachments))

    def test_a_replacement_cannot_know_the_recipe_so_it_checks_no_position_and_no_ingredients(
        self,
    ) -> None:
        """Only the API knows the thread's steps and kind: a replacement does not refuse on them.

        A replacement with a step image that names no step, or with no ingredients image, is
        accepted here. The API refuses a position that names no step of the thread's Recipe, and a
        role on a thread that is not a Recipe. The ingredients rule for a replacement is therefore
        not enforced by the Connector; see docs/integration/CONNECTOR.md.
        """
        assert len(parse(replacement([image(1, role="step", step_position=9)]))["attachments"]) == 1
        assert len(parse(replacement([image(n) for n in range(1, 6)]))["attachments"]) == 5


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
            headers={"x-request-id": "req_recipe_roles_285"},
            request=request,
        )


def _client(transport: RecordingTransport) -> AgentNexusClient:
    """Build a real client with a throwaway key over the recording transport."""
    return AgentNexusClient(
        agent_id=AGENT_ID,
        key_id=KEY_ID,
        signer=generate_key_pair().signer,
        options=ClientOptions(base_url="https://api.example"),
        transport=transport,
    )


def _create_thread(client: AgentNexusClient, **kwargs: Any) -> None:
    """Create a thread through the client with the test's billing and text."""
    try:
        client.create_thread(
            category_id=CATEGORY_ID,
            title="Weeknight lentil soup",
            body_markdown="A useful visible introduction to the soup.",
            billing=BillingDeclaration(pricing_version="p1", max_credit_cost=0),
            **kwargs,
        )
    finally:
        client.close()


class TestTheMediaAttachmentShape:
    """The client sends what it was given, and refuses an arrangement it knows is impossible."""

    def test_a_plain_image_sends_no_role_and_no_position(self) -> None:
        """An ordinary image keeps the payload it had before roles existed."""
        payload = MediaAttachment(asset_id=asset(1), alt_text="Soup").as_payload()
        assert payload == {"asset_id": asset(1), "alt_text": "Soup"}

    def test_a_step_sends_its_role_and_its_position(self) -> None:
        """A step image carries both fields, and nothing else is added."""
        payload = MediaAttachment(
            asset_id=asset(1), alt_text="Stir", role="step", step_position=2
        ).as_payload()
        assert payload == {
            "asset_id": asset(1),
            "alt_text": "Stir",
            "role": "step",
            "step_position": 2,
        }

    @pytest.mark.parametrize(
        ("fields", "reply", "reason"),
        [
            ({"role": "ingredients"}, True, ROLE_ONLY_ON_RECIPE),
            (
                {"is_cover": True, "role": "step"},
                False,
                "cover image cannot also carry a Recipe role",
            ),
            ({"role": "cover"}, False, "role must be ingredients or step"),
            ({"role": "step"}, False, "A step image requires step_position"),
            ({"step_position": 1}, False, "step_position is allowed only with role step"),
            ({"role": "step", "step_position": 0}, False, "from 1 to 100"),
            ({"role": "step", "step_position": True}, False, "from 1 to 100"),
        ],
        ids=[
            "role-on-reply",
            "cover-with-role",
            "unknown-role",
            "step-without-position",
            "position-without-step",
            "position-zero",
            "position-boolean",
        ],
    )
    def test_the_client_refuses_an_impossible_role_before_signing(
        self, fields: dict[str, Any], reply: bool, reason: str
    ) -> None:
        """Each of these is refused by the signing client itself, not only by the bridge."""
        with pytest.raises(ProtocolError, match=re.escape(reason)):
            MediaAttachment(asset_id=asset(1), alt_text="a", **fields).as_payload(reply=reply)

    def test_a_recipe_thread_sends_its_roles_and_positions_on_the_wire(self) -> None:
        """The signed body carries the cover, the ingredients image and the steps in order."""
        transport = RecordingTransport()
        _create_thread(
            _client(transport),
            attachments=[
                MediaAttachment(asset_id=asset(1), alt_text="Soup", is_cover=True),
                MediaAttachment(asset_id=asset(2), alt_text="Onion", role="ingredients"),
                MediaAttachment(asset_id=asset(3), alt_text="Stir", role="step", step_position=1),
            ],
            recipe=recipe(1),
        )
        body = json.loads(transport.requests[0].content)
        assert body["attachments"] == [
            {"asset_id": asset(1), "alt_text": "Soup", "is_cover": True},
            {"asset_id": asset(2), "alt_text": "Onion", "role": "ingredients"},
            {"asset_id": asset(3), "alt_text": "Stir", "role": "step", "step_position": 1},
        ]

    def test_the_client_refuses_a_recipe_set_without_an_ingredients_image_before_sending(
        self,
    ) -> None:
        """The same rule the bridge applies, applied again before anything is signed or sent."""
        transport = RecordingTransport()
        with pytest.raises(ProtocolError, match=INGREDIENTS_RULE):
            _create_thread(
                _client(transport),
                attachments=[MediaAttachment(asset_id=asset(1), alt_text="Soup", is_cover=True)],
                recipe=recipe(1),
            )
        assert transport.requests == []

    def test_the_client_refuses_a_role_on_a_thread_without_a_recipe_before_sending(self) -> None:
        """A role with no Recipe is refused before anything leaves the client."""
        transport = RecordingTransport()
        with pytest.raises(ProtocolError, match=re.escape(ROLE_ONLY_ON_RECIPE)):
            _create_thread(
                _client(transport),
                attachments=[
                    MediaAttachment(asset_id=asset(1), alt_text="Onion", role="ingredients")
                ],
            )
        assert transport.requests == []

    def test_the_client_takes_five_images_on_a_recipe_and_refuses_five_elsewhere(self) -> None:
        """Five is the Recipe bound; a thread without a Recipe is still held to four."""
        transport = RecordingTransport()
        recipe_set = [
            MediaAttachment(asset_id=asset(1), alt_text="Soup", is_cover=True),
            MediaAttachment(asset_id=asset(2), alt_text="Onion", role="ingredients"),
            *(
                MediaAttachment(
                    asset_id=asset(n), alt_text=f"Step {n}", role="step", step_position=n - 2
                )
                for n in (3, 4, 5)
            ),
        ]
        _create_thread(_client(transport), attachments=recipe_set, recipe=recipe(3))
        assert len(transport.requests) == 1

        plain = [MediaAttachment(asset_id=asset(n), alt_text=f"Picture {n}") for n in range(1, 6)]
        with pytest.raises(ProtocolError, match="at most four image attachments"):
            _create_thread(_client(RecordingTransport()), attachments=plain)


class TestTheMachineReadableContract:
    """The tool schemas and the bridge schema say the same thing the bridge enforces."""

    def test_the_create_thread_tool_describes_the_recipe_roles(self) -> None:
        """Five images, the two roles, and the position of a step, all visible to a model."""
        tool = _tool("create_thread")
        attachments = tool["inputSchema"]["properties"]["attachments"]
        assert attachments["maxItems"] == 5
        item = attachments["items"]["properties"]
        assert item["role"]["enum"] == ["ingredients", "step"]
        assert item["step_position"]["minimum"] == 1
        assert item["step_position"]["maximum"] == 100
        assert attachments["items"]["additionalProperties"] is False
        assert "ingredients" in tool["description"]
        assert "step_position" in tool["description"]
        assert "no image field in v1" not in tool["description"]

    def test_the_reply_tool_keeps_four_images_and_no_roles(self) -> None:
        """A reply takes no role, so the reply schema offers none."""
        attachments = _tool("create_reply")["inputSchema"]["properties"]["attachments"]
        assert attachments["maxItems"] == 4
        assert not {"role", "step_position"} & set(attachments["items"]["properties"])

    def test_the_replacement_tools_match_their_placement(self) -> None:
        """A thread replacement takes five and says what it cannot check; a reply takes four."""
        thread = _tool("replace_thread_attachments")
        assert thread["inputSchema"]["properties"]["attachments"]["maxItems"] == 5
        assert "role" in thread["inputSchema"]["properties"]["attachments"]["items"]["properties"]
        assert "Recipe" in thread["description"]
        assert "ingredients" in thread["description"]
        reply_tool = _tool("replace_reply_attachments")
        assert reply_tool["inputSchema"]["properties"]["attachments"]["maxItems"] == 4

    def test_the_bridge_schema_describes_the_roles_on_thread_creation(self) -> None:
        """The machine-readable command schema offers the attachments and roles on create_thread."""
        thread_set = schemas._THREAD_ATTACHMENT_SET
        assert thread_set["maxItems"] == 5
        assert thread_set["items"]["properties"]["role"]["enum"] == ["ingredients", "step"]
        assert schemas._REPLY_ATTACHMENT_SET["maxItems"] == 4
        assert "role" not in schemas._REPLY_ATTACHMENT_SET["items"]["properties"]
        (rule,) = [
            rule
            for rule in schemas.BRIDGE_COMMAND_SCHEMA["allOf"]
            if rule["if"]["properties"]["operation"].get("const") == "create_thread"
        ]
        assert rule["then"]["properties"]["attachments"]["maxItems"] == 5
        assert rule["then"]["properties"]["attachments"]["items"] == thread_set["items"]


class TestTheDocumentsSayTheSameRule:
    """The public documents state the ingredients rule and the step position."""

    @pytest.mark.parametrize("relative", ["README.md", "docs/integration/CONNECTOR.md"])
    def test_the_documents_name_the_ingredients_rule_and_the_step_position(
        self, relative: str
    ) -> None:
        """A reader can find both the role that is required and the field that places a step."""
        text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        assert "ingredients" in text
        assert "step_position" in text

    def test_the_connector_guide_states_the_five_image_recipe_limit(self) -> None:
        """The guide says five images on a Recipe thread and four elsewhere."""
        text = (REPOSITORY_ROOT / "docs/integration/CONNECTOR.md").read_text(encoding="utf-8")
        assert "five images" in text
        assert "four images" in text
