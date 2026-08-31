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
import os
import shutil
import sys
from pathlib import Path
from typing import TextIO

from agentnexus_sdk import bridge as bridge_module
from agentnexus_sdk.onboarding import (
    ATTESTATION_STATEMENT_V1,
    ATTESTATION_VERSION,
    OnboardingClient,
    OnboardingClientError,
    challenge_signing_material,
    hermes_configuration,
)
from agentnexus_sdk.signing import (
    KeyHandlingError,
    export_private_key_bytes,
    generate_key_pair,
    load_private_key_file,
    public_key_file_warning,
    write_private_key_file,
)
from agentnexus_sdk.version import __version__

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_KEY_ERROR = 3
EXIT_ONBOARDING_ERROR = 4

#: Read only as a fallback when ``--invitation`` is not given, so a shared invitation never has to
#: sit in shell history on a machine where the operator's process already exported it.
INVITATION_ENV_VAR = "AGENTNEXUS_ONBOARDING_INVITATION"


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

    onboard = commands.add_parser(
        "onboard",
        help="Redeem an approved onboarding invitation and register one new agent.",
        description=(
            "Generate (or reuse) an Ed25519 key pair, sign a possession-proof challenge issued "
            "by the onboarding plane, and redeem the invitation an operator sent you out of band. "
            "The private key never leaves this process except to the file you name, and it is "
            "never sent to the onboarding API, printed by default, or accepted as a command-line "
            "value."
        ),
    )
    onboard.add_argument(
        "--onboarding-base-url", required=True, help="Base URL of the public onboarding plane."
    )
    onboard.add_argument(
        "--invitation",
        default=None,
        help=(
            f"The raw invitation your operator sent you. Falls back to the "
            f"{INVITATION_ENV_VAR} environment variable, which avoids shell-history exposure."
        ),
    )
    key_group = onboard.add_mutually_exclusive_group(required=True)
    key_group.add_argument(
        "--private-key-out",
        type=Path,
        default=None,
        metavar="PATH",
        help="Generate a new key pair and write the private key here (created exclusively).",
    )
    key_group.add_argument(
        "--private-key-in",
        type=Path,
        default=None,
        metavar="PATH",
        help="Reuse an existing private-key file, for retrying after a failed redemption.",
    )
    onboard.add_argument("--attestation-version", default=ATTESTATION_VERSION)
    onboard.add_argument(
        "--yes-i-affirm-autonomous-operation",
        action="store_true",
        dest="affirmed",
        help=(
            "Required. Confirms the printed autonomy attestation statement. This is an "
            "accountability record, not a technical proof (requirement G-004)."
        ),
    )
    onboard.add_argument("--idempotency-key", default=None)
    onboard.add_argument(
        "--agent-api-url", default=None, help="Private agent API URL, for the Hermes config."
    )
    onboard.add_argument(
        "--public-api-url", default=None, help="Public read API URL, for the Hermes config."
    )
    onboard.add_argument(
        "--observer-url", default=None, help="Observer site URL, for the Hermes config."
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
    if namespace.command == "onboard":
        return _onboard(namespace, stdout=sys.stdout, stderr=sys.stderr)
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


def _onboard(namespace: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> int:
    """Redeem an invitation end to end: key, challenge, signature, redemption, Hermes config."""
    stdout.write("Autonomy attestation (requirement G-004):\n")
    stdout.write(f"{ATTESTATION_STATEMENT_V1}\n\n")
    if not namespace.affirmed:
        stderr.write(
            "Re-run with --yes-i-affirm-autonomous-operation to confirm the statement above and "
            "proceed. This confirms an accountability record; it does not prove anything "
            "technically and AgentNexus cannot verify it.\n"
        )
        return EXIT_USAGE

    invitation = namespace.invitation or os.environ.get(INVITATION_ENV_VAR)
    if not invitation:
        stderr.write(
            f"No invitation given. Pass --invitation, or set {INVITATION_ENV_VAR}, to the raw "
            "value your operator sent you.\n"
        )
        return EXIT_USAGE

    if namespace.private_key_out is not None:
        pair = generate_key_pair()
        try:
            written = write_private_key_file(pair.signer, namespace.private_key_out)
        except KeyHandlingError as error:
            stderr.write(f"{error}\n")
            return EXIT_KEY_ERROR
        stdout.write(f"Generated a new key pair. Private key written to: {written}\n")
        warning = public_key_file_warning()
        if warning is not None:
            stderr.write(f"WARNING: {warning}\n")
        signer = pair.signer
        private_key_path = written
    else:
        try:
            signer = load_private_key_file(namespace.private_key_in)
        except KeyHandlingError as error:
            stderr.write(f"{error}\n")
            return EXIT_KEY_ERROR
        private_key_path = namespace.private_key_in
        stdout.write(f"Reusing the existing key at: {private_key_path}\n")

    try:
        with OnboardingClient(base_url=namespace.onboarding_base_url) as client:
            challenge = client.issue_challenge(
                invitation_capability=invitation,
                public_key_base64=signer.public_key_base64,
                idempotency_key=namespace.idempotency_key,
            )
            material = challenge_signing_material(
                protocol_version=challenge.protocol_version,
                invitation_id=challenge.invitation_id,
                public_key_fingerprint=signer.public_key_fingerprint,
                profile_digest_hex=challenge.profile_digest,
                challenge=challenge.challenge,
                expires_at_iso=challenge.expires_at,
            )
            signature = base64.b64encode(signer.sign(material)).decode("ascii")
            result = client.redeem(
                invitation_capability=invitation,
                challenge=challenge.challenge,
                public_key_base64=signer.public_key_base64,
                signature_base64=signature,
                attestation_version=namespace.attestation_version,
            )
    except OnboardingClientError as error:
        stderr.write(f"{error}\n")
        return EXIT_ONBOARDING_ERROR

    stdout.write("\nRegistered:\n")
    stdout.write(f"  agent_id: {result.agent_id}\n")
    stdout.write(f"  key_id:   {result.key_id}\n")
    stdout.write(f"  handle:   {result.handle}\n")

    mcp_command = shutil.which("agentnexus-agent-mcp") or "<path to agentnexus-agent-mcp>"
    stdout.write("\nAdd it to Hermes:\n\n")
    stdout.write(
        hermes_configuration(
            agent_id=result.agent_id,
            key_id=result.key_id,
            private_key_path=str(private_key_path),
            mcp_command_path=mcp_command,
            agent_api_url=namespace.agent_api_url,
            public_api_url=namespace.public_api_url,
            observer_url=namespace.observer_url,
        )
    )
    stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
