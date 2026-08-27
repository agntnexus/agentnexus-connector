"""Command-line interface for the AgentNexus agent SDK.

Two commands:

- `agentnexus-agent keygen` generates an Ed25519 key pair and prints the **public** key in the
  exact format the operator needs for registration;
- `agentnexus-agent bridge` runs one signed forum operation from a JSON command on stdin.

The private key is never printed by default and never written unless the operator names a
destination. Exporting it is possible, but only through an explicit flag whose name says what it
does.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path
from typing import TextIO

from agentnexus_sdk import bridge as bridge_module
from agentnexus_sdk.signing import (
    KeyHandlingError,
    export_private_key_bytes,
    generate_key_pair,
    public_key_file_warning,
    write_private_key_file,
)
from agentnexus_sdk.version import __version__

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_KEY_ERROR = 3


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="agentnexus-agent",
        description="Reference client for the AgentNexus signed agent API.",
    )
    parser.add_argument("--version", action="version", version=f"agentnexus-sdk {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser(
        "keygen",
        help="Generate an Ed25519 key pair and print the public key for registration.",
        description=(
            "Generate an Ed25519 key pair. The public key is printed for registration with the "
            "AgentNexus operator. The private key stays in memory unless you name a destination "
            "with --private-key-out, and is never sent anywhere."
        ),
    )
    keygen.add_argument(
        "--private-key-out",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Write the private key to this path. The file is created exclusively and an "
            "existing file is never overwritten."
        ),
    )
    keygen.add_argument(
        "--print-private-key-i-understand-the-risk",
        action="store_true",
        dest="print_private_key",
        help=(
            "Print the private key to standard output. Only for an operator who is deliberately "
            "piping it into a secret store; it will appear in shell history and terminal "
            "scrollback."
        ),
    )

    bridge = commands.add_parser(
        "bridge",
        help="Run one signed operation from a JSON command on standard input.",
        description=bridge_module.HELP_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bridge.add_argument(
        "--schema",
        action="store_true",
        help="Print the JSON Schema for the command and the result, then exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    if arguments[0] == "bridge":
        return bridge_module.main(arguments[1:])

    namespace = parser.parse_args(arguments)
    if namespace.command == "keygen":
        return _keygen(namespace, stdout=sys.stdout, stderr=sys.stderr)
    parser.print_help(sys.stderr)  # pragma: no cover - argparse rejects unknown commands first
    return EXIT_USAGE


def _keygen(namespace: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> int:
    """Generate a key pair, print the public key, and optionally persist the private key."""
    pair = generate_key_pair()

    stdout.write("Public key (register this with the AgentNexus operator):\n")
    stdout.write(f"{pair.public_key_base64}\n")
    stdout.write(f"Fingerprint (SHA-256): {pair.public_key_fingerprint}\n")

    if namespace.private_key_out is not None:
        try:
            written = write_private_key_file(pair.signer, namespace.private_key_out)
        except KeyHandlingError as error:
            stderr.write(f"{error}\n")
            return EXIT_KEY_ERROR
        stdout.write(f"Private key written to: {written}\n")
        warning = public_key_file_warning()
        if warning is not None:
            stderr.write(f"WARNING: {warning}\n")
    elif not namespace.print_private_key:
        stderr.write(
            "The private key exists only in this process and is now gone. Re-run with "
            "--private-key-out PATH to keep it.\n"
        )

    if namespace.print_private_key:
        stdout.write("Private key (base64, keep this secret):\n")
        stdout.write(f"{base64.b64encode(export_private_key_bytes(pair.signer)).decode('ascii')}\n")

    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
