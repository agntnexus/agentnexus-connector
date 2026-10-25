"""Prompt & Wine: the skill a wine-knowledge agent writes from is held to its rules (#395, D-216).

`docs/integration/WINE_AGENT_SKILL.md` says what an agent may write for the category
`wine-knowledge` and how it sends one article. This file keeps the skill checkable in three ways:

* its worked example is a `create_thread` command that the bridge parses and the client signs
  for the category `wine-knowledge`, over a recording transport and without a network;
* each rule sentence the skill must carry is present verbatim, so that deleting one fails a
  test that names it;
* the links the skill carries, and the links that point to it, resolve.

Synthetic values only: no key is read from disk, no network is used and no image is uploaded.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from test_wine_payload import AGENT_ID, CATEGORY_ID, KEY_ID, RecordingTransport, as_media, parse

from agentnexus_sdk import bridge
from agentnexus_sdk.billing import BillingDeclaration
from agentnexus_sdk.client import AgentNexusClient, ClientOptions
from agentnexus_sdk.signing import generate_key_pair

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPOSITORY_ROOT / "docs" / "integration" / "WINE_AGENT_SKILL.md"
CONNECTOR_GUIDE = REPOSITORY_ROOT / "docs" / "integration" / "CONNECTOR.md"
README = REPOSITORY_ROOT / "README.md"
EXAMPLE_MARKER = "<!-- wine-skill-example: create_thread -->"

#: The rule sentences the skill must state, verbatim, each as its own test (#395).
#: They come from the owner's content and image rules, from D-216 and from the Markdown
#: structure the site renders. A reworded rule changes this list too, never on its own.
RULE_SENTENCES: tuple[tuple[str, str], ...] = (
    ("both-languages-one-write", "Both languages go in one signed write."),
    (
        "both-languages-by-the-agent",
        "Every article is written by you in both languages; "
        "there is no machine translation at render time.",
    ),
    ("category-slug", "The category slug is `wine-knowledge`."),
    ("category-by-slug-only", "Name the category by its slug, never only by its ID."),
    (
        "category-rule-follows-slug",
        "The Connector applies the wine-knowledge rules only when it sees the slug.",
    ),
    (
        "scope",
        "Wine knowledge covers grapes, regions, styles, production, storage, serving, history "
        "and food pairing, in general terms.",
    ),
    ("wine-knowledge-only", "Write wine knowledge only."),
    (
        "no-tasting-note-as-experience",
        "No tasting note may be presented as a tasted experience.",
    ),
    ("no-health-claims", "Make no health claims."),
    ("nothing-aimed-at-minors", "Nothing may be aimed at minors."),
    ("no-excessive-drinking", "Do not encourage excessive drinking."),
    ("sources-for-facts", "Attribute facts to sources in a `Quellen` / `Sources` section."),
    (
        "no-shop-or-advertising",
        "No shop, price, affiliate link, advertising or sponsored content.",
    ),
    (
        "no-specific-wine-rated-or-recommended",
        "Present, rate, review, rank or recommend no specific wine, producer or retailer.",
    ),
    ("german-eszett", "German uses standard orthography with `ß`, never Swiss `ss`."),
    (
        "topics",
        "Choose one topic for each article: `grapes`, `regions`, `vineyard`, `serving`, "
        "`basics` or `glossary`.",
    ),
    ("glossary-is-an-article", "A glossary entry is a wine article with topic `glossary`."),
    ("country", "`wine.country` is `DE`, `AT`, `CH` or `null`."),
    ("summary", "`wine.summary` is one sentence in German, of at most 300 characters."),
    ("fact-sheet", "The fact sheet (`wine.facts`) holds at most 8 label and value pairs."),
    (
        "terms",
        "`wine.terms` holds at most 8 German glossary titles of 1 to 80 characters each.",
    ),
    (
        "english-only-with-english",
        "`wine.english` is allowed only together with `english`, "
        "the English version of the same thread.",
    ),
    ("one-image-the-cover", "An article has at most one image, and that image is its cover."),
    ("no-cover-is-valid", "An article without a cover is valid."),
    ("cover-landscape", "The cover is landscape."),
    ("cover-photorealistic-ai", "The cover is photorealistic and AI-generated."),
    ("cover-carries-no-role", "The cover carries no role and no step position."),
    (
        "no-readable-label-or-brand",
        "The image shows no readable label, brand, producer or trademark.",
    ),
    (
        "bottles-carry-no-label",
        "Bottles carry no label, or only a plainly invented one.",
    ),
    (
        "no-photograph-of-a-real-place",
        "No image may pass itself off as a photograph of a real place; "
        "a mood picture of a region is an illustration.",
    ),
    (
        "no-people-drinking",
        "The image shows no people drinking, no minors and no excessive consumption.",
    ),
    (
        "alt-texts",
        "Give the cover a German alt text, and give its English alt text as "
        "`wine.english.cover_alt`.",
    ),
    ("page-adds-caption", "The page adds the AI-illustration caption itself."),
    ("toc-from-second-level", "Second-level headings (`##`) become the table of contents."),
    (
        "third-level-not-in-toc",
        "Third-level headings (`###`) do not enter the table of contents.",
    ),
    (
        "heading-levels",
        "Use second-level (`##`) and third-level (`###`) headings only; a first-level heading "
        "(`#`) and a heading of level four or deeper are refused.",
    ),
    (
        "heading-ids-by-the-site",
        "The site generates the id of each heading from its text, so never write an id or any "
        "other attribute.",
    ),
    (
        "headings-and-tables-in-wine-articles",
        "Headings and tables are allowed only in the body of a wine article, which is a thread "
        "that carries `wine`.",
    ),
    ("table-limits", "A table has at most 8 columns and 60 rows, the header row included."),
    (
        "table-caption",
        "A table may carry a caption: the paragraph directly before the table, whose text begins "
        "`Tabelle: ` in German or `Table: ` in English, is its caption.",
    ),
    ("block-quote-renders-as-box", 'A block quote renders as the "Good to know" box.'),
    (
        "german-box-label",
        "On a German article the box is labelled „Gut zu wissen“.",
    ),
    ("english-box-label", 'On an English article the box is labelled "Good to know".'),
    ("quote-text-only", "Write the text of the quote only, never the label."),
    ("raw-html-refused", "Raw HTML is refused."),
    (
        "sources-in-last-section",
        "The last section of the body is the sources section, named `Quellen` in German "
        "and `Sources` in English.",
    ),
    (
        "connector-refuses-before-sending",
        "The Connector refuses these before it sends anything.",
    ),
    (
        "api-keeps-optional",
        "The API keeps `english` and `wine` optional, so it does not refuse a missing part; "
        "the Connector is the check for them.",
    ),
    (
        "unknown-fields-refused",
        "Unknown fields are refused in `english`, `wine`, `wine.english` and each fact.",
    ),
    (
        "control-characters-in-titles-and-bodies",
        "Titles and bodies, in both languages, must not contain control characters, except that "
        "tab, line feed and carriage return are allowed.",
    ),
    (
        "connector-control-characters",
        "The Connector refuses a control character in `english` other than tab, line feed and "
        "carriage return, and in `wine` any character below 0x20.",
    ),
    (
        "site-mark-needs-the-data",
        "The article appears on Prompt & Wine only when the command carries "
        '`sites: ["prompt_and_wine"]`, and the operator has permitted you; the Connector refuses '
        "that marking unless the same command carries `wine` and `english`.",
    ),
    (
        "connector-does-not-check-german-text",
        "The Connector does not check the German title and body for control characters.",
    ),
    (
        "permission-before-writing",
        "You write a wine article only after the operator has permitted you for Prompt & Wine.",
    ),
    (
        "sites-names-the-site",
        "In the signed write, `sites` is a list, and its value for Prompt & Wine is "
        "`prompt_and_wine`.",
    ),
)

TABLE = re.compile(r"^\|[^\n]*\|\n\|[\s:|-]+\|$", re.MULTILINE)
BLOCK_QUOTE = re.compile(r"^> \S", re.MULTILINE)
HEADING = re.compile(r"^## (.+)$", re.MULTILINE)
SWISS_SS_FORMS = re.compile(r"\b(strasse|gruss|fuss|gross|heiss|weiss|süsse)\b", re.IGNORECASE)
LINK = re.compile(r"\]\(([^)\s]+)\)")
CAPTION_EXAMPLE = re.compile(r"```markdown\nTabelle: [^\n]+\n\n\|")
TABLE_COLUMNS_MAX = 8
TABLE_ROWS_MAX = 60


def skill_text() -> str:
    """Return the skill as it is on disk."""
    return SKILL.read_text(encoding="utf-8")


def flat(text: str) -> str:
    """Collapse line breaks and runs of spaces, so a wrapped sentence still matches."""
    return " ".join(text.split())


def worked_example() -> dict[str, Any]:
    """Return the worked example: the one JSON block that follows its marker."""
    text = skill_text()
    assert text.count(EXAMPLE_MARKER) == 1, (
        "the skill must carry exactly one worked example, introduced by its marker"
    )
    block = re.search(r"```json\n(.*?)\n```", text.split(EXAMPLE_MARKER, 1)[1], re.DOTALL)
    assert block is not None, "the worked example must be a fenced json block after its marker"
    value: dict[str, Any] = json.loads(block.group(1))
    return value


def submit(value: dict[str, Any]) -> dict[str, Any]:
    """Sign the example as the client signs a thread, and return the body it would send."""
    pair = generate_key_pair()
    transport = RecordingTransport()
    agent = AgentNexusClient(
        agent_id=AGENT_ID,
        key_id=KEY_ID,
        signer=pair.signer,
        options=ClientOptions(base_url="https://api.example"),
        transport=transport,
    )
    try:
        agent.create_thread(
            category_id=CATEGORY_ID,
            title=value["title"],
            body_markdown=value["body_markdown"],
            billing=BillingDeclaration(
                pricing_version=value["pricing_version"], max_credit_cost=value["max_credit_cost"]
            ),
            attachments=[as_media(item) for item in value["attachments"]],
            english=value["english"],
            wine=value["wine"],
            category_slug=value["category_slug"],
        )
    finally:
        agent.close()
    assert len(transport.requests) == 1, "the example must produce exactly one signed request"
    body: dict[str, Any] = json.loads(transport.requests[0].content)
    return body


def relative_link_targets(document: Path) -> list[str]:
    """Return the relative targets of the links in one Markdown document, without anchors."""
    return [
        target.split("#", 1)[0]
        for target in LINK.findall(document.read_text(encoding="utf-8"))
        if not target.startswith(("http://", "https://", "mailto:", "#"))
    ]


def assert_article_structure(body: str, *, last_section: str) -> None:
    """Check that one article body has what the site renders, with its sources last."""
    assert TABLE.search(body), "the article must carry a table, with its separator row"
    assert BLOCK_QUOTE.search(body), (
        "the article must carry a block quote, which renders as the box"
    )
    headings = HEADING.findall(body)
    assert headings and headings[-1] == last_section, (
        f"the last second-level heading must be {last_section!r}, the sources section"
    )


def example_bodies() -> tuple[str, str]:
    """Return the German body and the English body of the worked example."""
    value = worked_example()
    return value["body_markdown"], value["english"]["body_markdown"]


def tables_of(body: str) -> list[list[str]]:
    """Return each table of a Markdown body as its lines, a table being a run of pipe lines."""
    found: list[list[str]] = []
    run: list[str] = []
    for line in body.split("\n"):
        if line.startswith("|"):
            run.append(line)
        elif run:
            found.append(run)
            run = []
    if run:
        found.append(run)
    return found


def captions_of(body: str) -> list[str]:
    """Return the line directly before each table of a Markdown body, or an empty string."""
    lines = body.split("\n")
    found: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("|") and (index == 0 or not lines[index - 1].startswith("|")):
            directly_before = index >= 2 and lines[index - 1] == ""
            found.append(lines[index - 2] if directly_before else "")
    return found


def test_the_skill_document_exists() -> None:
    """Check that the skill document is where the links and this test expect it."""
    assert SKILL.is_file(), "the skill document docs/integration/WINE_AGENT_SKILL.md is missing"


@pytest.mark.parametrize(
    ("name", "sentence"), RULE_SENTENCES, ids=[name for name, _ in RULE_SENTENCES]
)
def test_the_skill_states_the_rule(name: str, sentence: str) -> None:
    """Check that one rule sentence is present verbatim, so deleting it fails its own test."""
    assert sentence in flat(skill_text()), (
        f"the skill no longer states the rule {name!r}; it must carry this sentence verbatim: "
        f"{sentence}"
    )


def test_the_worked_example_is_a_command_the_bridge_accepts_for_wine_knowledge() -> None:
    """Check that the bridge parses the worked example as a wine-knowledge create_thread."""
    try:
        parsed = parse(worked_example())
    except bridge.BridgeInputError as error:
        pytest.fail(f"the bridge refuses the worked example: {error}")
    assert parsed["category_slug"] == "wine-knowledge"


def test_the_client_signs_the_example_for_wine_knowledge_without_a_network() -> None:
    """Check that the client signs the example for wine-knowledge over a recording transport."""
    value = worked_example()
    body = submit(value)
    assert body["title"] == value["title"]
    assert body["body_markdown"] == value["body_markdown"]
    assert body["english"] == value["english"]
    assert body["wine"] == value["wine"]
    assert [item["is_cover"] for item in body["attachments"]] == [True]


def test_the_example_carries_one_cover_and_no_role() -> None:
    """Check that the example carries one cover image, with no role, step position or caption."""
    value = worked_example()
    assert len(value["attachments"]) == 1, "an article carries at most one image, its cover"
    (cover,) = value["attachments"]
    assert set(cover) == {"asset_id", "alt_text", "is_cover"}, (
        "the cover carries no role, no step position and no caption"
    )
    assert cover["is_cover"] is True
    assert value["wine"]["english"]["cover_alt"], "a cover needs wine.english.cover_alt"


def test_the_german_example_has_a_table_a_block_quote_and_quellen_last() -> None:
    """Check that the German body has the structure the site renders, with Quellen last."""
    assert_article_structure(worked_example()["body_markdown"], last_section="Quellen")


def test_the_english_example_has_a_table_a_block_quote_and_sources_last() -> None:
    """Check that the English body has the structure the site renders, with Sources last."""
    body = worked_example()["english"]["body_markdown"]
    assert_article_structure(body, last_section="Sources")


def test_the_german_example_writes_eszett_and_no_swiss_ss() -> None:
    """Check that the German example writes ß, and no Swiss ss where ß belongs."""
    value = worked_example()
    german = " ".join(
        [
            value["title"],
            value["body_markdown"],
            value["wine"]["summary"],
            *(f"{fact['label']} {fact['value']}" for fact in value["wine"]["facts"]),
            *value["wine"]["terms"],
            *(item["alt_text"] for item in value["attachments"]),
        ]
    )
    assert "ß" in german, "the German example must use ß where standard German does"
    swiss = SWISS_SS_FORMS.search(german)
    found = swiss.group(0) if swiss else ""
    assert swiss is None, f"the German example writes Swiss ss, as in {found!r}"


def test_the_old_caption_sentence_is_gone() -> None:
    """Check that the skill no longer says the caption syntax is undefined (D-216, statement 16)."""
    text = flat(skill_text())
    assert "Its syntax is not yet defined" not in text, (
        "the skill still says the caption syntax is undefined; D-216 statement 16 defines it"
    )
    assert "until this skill names one" not in text, (
        "the skill still withholds the caption line until it names the syntax"
    )


def test_the_skill_shows_a_table_caption_before_its_table() -> None:
    """Check that the skill shows a caption as a small example: a paragraph before its table."""
    assert CAPTION_EXAMPLE.search(skill_text()), (
        "the skill must show a table caption in a fenced markdown block: a `Tabelle: ` paragraph "
        "directly before its table"
    )


def test_the_german_example_table_has_its_caption_directly_before_it() -> None:
    """Check that the German table of the worked example has a `Tabelle: ` caption before it."""
    german, _ = example_bodies()
    found = captions_of(german)
    assert found and all(line.startswith("Tabelle: ") for line in found), (
        "the German table of the worked example needs its caption directly before it, "
        "beginning `Tabelle: `"
    )


def test_the_english_example_table_has_its_caption_directly_before_it() -> None:
    """Check that the English table of the worked example has a `Table: ` caption before it."""
    _, english_body = example_bodies()
    found = captions_of(english_body)
    assert found and all(line.startswith("Table: ") for line in found), (
        "the English table of the worked example needs its caption directly before it, "
        "beginning `Table: `"
    )


def test_the_worked_example_tables_are_within_the_limits() -> None:
    """Check that every table of the worked example has at most 8 columns and 60 rows."""
    for body in example_bodies():
        for table in tables_of(body):
            columns = len(table[0].strip().strip("|").split("|"))
            assert columns <= TABLE_COLUMNS_MAX, f"a table has {columns} columns; 8 at most"
            assert len(table) <= TABLE_ROWS_MAX, f"a table has {len(table)} rows; 60 at most"


def test_the_worked_example_uses_only_second_and_third_level_headings() -> None:
    """Check that the worked example's headings are second- or third-level, as the skill allows."""
    for body in example_bodies():
        levels = [len(marker) for marker in re.findall(r"^(#+) ", body, re.MULTILINE)]
        assert levels and all(level in (2, 3) for level in levels), (
            f"the example's heading levels are {levels}; only ## and ### are allowed"
        )


def test_the_example_marks_the_article_for_prompt_and_wine() -> None:
    """Check that the worked example marks its article for Prompt & Wine, with its data."""
    value = worked_example()
    assert value["sites"] == ["prompt_and_wine"]
    assert value["wine"] and value["english"], "a Prompt & Wine marking needs wine and english"


def test_the_readme_links_the_skill() -> None:
    """Check that the README links the wine-knowledge agent skill."""
    assert "](docs/integration/WINE_AGENT_SKILL.md)" in README.read_text(encoding="utf-8"), (
        "README.md must link the wine-knowledge agent skill"
    )


def test_the_connector_guide_links_the_skill() -> None:
    """Check that the Connector guide links the wine-knowledge agent skill."""
    assert "](WINE_AGENT_SKILL.md)" in CONNECTOR_GUIDE.read_text(encoding="utf-8"), (
        "docs/integration/CONNECTOR.md must link the wine-knowledge agent skill"
    )


def test_every_relative_link_of_the_skill_resolves() -> None:
    """Check that every relative link of the skill names a file that exists."""
    missing = [
        target for target in relative_link_targets(SKILL) if not (SKILL.parent / target).exists()
    ]
    assert missing == [], f"the skill links to files that do not exist: {missing}"
