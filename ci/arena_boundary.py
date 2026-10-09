"""What the Arena's runtime-neutral and runtime-driver code may name (agntnexus/agentnexus#228).

AgentNexus does not know models or providers. The names below are the ones no Connector code may
carry as logic or as a comment: not in the supervisor, not in the match process, not in the driver
registry, and not in a driver either. A runtime may know its own provider internally; the Connector
that drives it does not. They belong in a driver's test fixtures and nowhere else.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

#: Names distinctive enough to be refused anywhere, even inside an identifier or a longer word.
DISTINCTIVE_NAMES = (
    r"openai|anthropic|openrouter|codex|claude|ollama|gemini|mistral|llama|deepseek|qwen|"
    r"huggingface|perplexity|bedrock|moonshot|fireworks|zhipu|minimax|inkling"
)
#: Short names that are also words or parts of words: refused as whole words, where an underscore,
#: a digit or a hyphen ends a word as a letter does not.
SHORT_NAMES = r"gpt[-_ ]?\d|vertex|azure|groq|xai|grok|cohere|kimi|nvidia|luna|haiku|sonnet|opus"
MODEL_AND_PROVIDER_NAMES = re.compile(
    rf"{DISTINCTIVE_NAMES}|(?<![a-z])(?:{SHORT_NAMES})(?![a-z])", re.IGNORECASE
)
RUNTIME_NAMES = re.compile(r"hermes|openclaw", re.IGNORECASE)


def names_in(path: Path, pattern: re.Pattern[str] = MODEL_AND_PROVIDER_NAMES) -> list[str]:
    """Return the distinct matches of a pattern in a whole source file, comments included."""
    text = path.read_text(encoding="utf-8")
    return sorted({match.group(0).lower() for match in pattern.finditer(text)})


def names_a_secrets_file(text: str) -> bool:
    """Return whether a source reads, or names, a secrets file or the module that reads one."""
    named = "\n".join(
        line for line in text.splitlines() if "# metadata only, never opened" not in line
    )
    return "dotenv" in named or bool(re.search(r"\.env(?![A-Za-z_])", named))


def imported_text(text: str) -> set[str]:
    """Return the modules a source text imports statically."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.update(f"{base}.{alias.name}".strip(".") for alias in node.names)
            found.add(base)
    return found
