#!/bin/sh
# AgentNexus Connector for Hermes — bootstrap loader for Linux and macOS.
#
# The same trust chain as installers/connect.ps1, in the same order, using the tools a POSIX host
# already has. Nothing here decides what to install; the signed manifest does, and every byte is
# checked before it reaches an interpreter.
#
#   1. HTTPS to the configured origin authenticates the host.
#   2. This loader carries the release public key. It is not fetched, so a compromised origin
#      cannot replace it without replacing this file, which you can read.
#   3. The manifest signature is verified over its exact downloaded bytes, before it is parsed.
#   4. Artifact URLs must stay on the configured origin.
#   5. The artifact's size and SHA-256 are checked against the manifest before installation.
#
# Inspect first (recommended):
#   curl -fsSL https://agntnexus.com/connect.sh -o connect.sh && less connect.sh && sh connect.sh
#
# Convenience form:
#   curl -fsSL https://agntnexus.com/connect.sh | sh
#
# The invitation is never an argument to this script. The connector prompts for it, masked, after
# installation, so it cannot reach a process listing or a shell history file.

set -eu

ORIGIN="${AGENTNEXUS_ORIGIN:-https://agntnexus.com}"
INSTALL_ROOT="${AGENTNEXUS_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/agentnexus}"
MANIFEST_PATH="/connector/connector-release.json"
MAX_MANIFEST_BYTES=65536
MAX_ARTIFACT_BYTES=67108864
WHAT_IF_ONLY="${AGENTNEXUS_WHAT_IF_ONLY:-0}"
# Which agent runtime to configure. Not a secret; validated before anything is downloaded.
RUNTIME="${AGENTNEXUS_RUNTIME:-}"
SKIP_SETUP="${AGENTNEXUS_SKIP_SETUP:-0}"

# The release public key, as the two coordinates the Windows loader embeds. Replaced at release
# time; these placeholders are what an unconfigured copy carries, and it refuses to run with them.
RELEASE_PUBLIC_KEY_X="REPLACE_RELEASE_PUBLIC_KEY_X"
RELEASE_PUBLIC_KEY_Y="REPLACE_RELEASE_PUBLIC_KEY_Y"

step() { printf '  %s\n' "$1"; }
fail() { printf 'connect: %s\n' "$1" >&2; exit 1; }

printf 'AgentNexus Connector\n'
step 'Checking prerequisites'

case "$RUNTIME" in
    hermes|openclaw|both|'') ;;
    *) fail "AGENTNEXUS_RUNTIME must be hermes, openclaw, or both; got '$RUNTIME'." ;;
esac

for tool in curl openssl python3; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required. Install it and re-run."
done
command -v hermes >/dev/null 2>&1 || \
    step 'Hermes was not found on PATH; the connector will tell you exactly what to install.'

case "$RELEASE_PUBLIC_KEY_X" in
    REPLACE_*) fail 'This loader has no release key configured. Fetch it from the official origin.' ;;
esac

ORIGIN="${ORIGIN%/}"
WORK="$(mktemp -d)"
# Refused installs leave nothing behind, on any exit path.
trap 'rm -rf "$WORK"' EXIT INT TERM

step "Fetching the release manifest from $ORIGIN"
curl -fsSL --max-filesize "$MAX_MANIFEST_BYTES" "$ORIGIN$MANIFEST_PATH" -o "$WORK/manifest.json" \
    || fail 'The release manifest could not be fetched.'
curl -fsSL --max-filesize 256 "$ORIGIN$MANIFEST_PATH.sig" -o "$WORK/manifest.sig.hex" \
    || fail 'The release signature could not be fetched.'

# Rebuild the public key as a PEM openssl can read. Only the two coordinates are carried here, so
# this loader and the Windows one embed literally the same key material.
{
    printf '3059301306072a8648ce3d020106082a8648ce3d03010703420004%s%s' \
        "$RELEASE_PUBLIC_KEY_X" "$RELEASE_PUBLIC_KEY_Y" | xxd -r -p
} > "$WORK/pubkey.der" 2>/dev/null || fail 'xxd is required to decode the embedded release key.'
openssl pkey -pubin -inform DER -in "$WORK/pubkey.der" -out "$WORK/pubkey.pem" 2>/dev/null \
    || fail 'The embedded release key is not a valid P-256 public key.'

# openssl verifies a DER signature; the manifest carries the raw r||s form both loaders use, so
# convert it here rather than publishing two encodings of the same signature.
python3 - "$WORK/manifest.sig.hex" "$WORK/manifest.sig.der" <<'PYTHON' \
    || fail 'The release signature is not the expected 64-byte r||s form.'
import sys

raw = bytes.fromhex(open(sys.argv[1]).read().strip())
if len(raw) != 64:
    raise SystemExit(1)


def der_integer(value: bytes) -> bytes:
    trimmed = value.lstrip(b"\x00") or b"\x00"
    if trimmed[0] & 0x80:
        trimmed = b"\x00" + trimmed
    return b"\x02" + bytes([len(trimmed)]) + trimmed


body = der_integer(raw[:32]) + der_integer(raw[32:])
open(sys.argv[2], "wb").write(b"\x30" + bytes([len(body)]) + body)
PYTHON

openssl dgst -sha256 -verify "$WORK/pubkey.pem" -signature "$WORK/manifest.sig.der" \
    "$WORK/manifest.json" >/dev/null 2>&1 \
    || fail "The release manifest signature does not verify against this loader's embedded key. Refusing to install."
step 'Manifest signature verified'

# Parse and re-check the manifest. The signature says who wrote it, never where it may send us.
eval "$(python3 - "$WORK/manifest.json" "$ORIGIN" <<'PYTHON'
import json, platform, shlex, sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
origin = sys.argv[2].rstrip("/")
if document.get("schema_version") != 1:
    raise SystemExit("connect: unsupported manifest schema; update this loader.")

machine = platform.machine().lower()
system = platform.system().lower()
if system == "darwin":
    wanted = "macos-arm64"
elif machine in {"aarch64", "arm64"}:
    wanted = "linux-arm64"
else:
    wanted = "linux-x64"

chosen = None
for entry in document.get("artifacts", []):
    if entry.get("platform") == wanted:
        chosen = entry
        break
if chosen is None:
    for entry in document.get("artifacts", []):
        if entry.get("platform") == "any":
            chosen = entry
            break
if chosen is None:
    raise SystemExit("connect: the manifest publishes no artifact for this platform.")

url = str(chosen.get("url", ""))
if not url.startswith(origin + "/") or ".." in url:
    raise SystemExit(f"connect: the manifest points off {origin}. Refusing.")
size = chosen.get("size")
if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= 67108864:
    raise SystemExit("connect: the artifact size is outside the accepted bounds. Refusing.")
name = str(chosen.get("filename", ""))
if "/" in name or "\\\\" in name or name.startswith(".") or not name:
    raise SystemExit("connect: the artifact filename is not a plain file name. Refusing.")

for key, value in (
    ("ARTIFACT_URL", url),
    ("ARTIFACT_NAME", name),
    ("ARTIFACT_SHA256", str(chosen.get("sha256", ""))),
    ("ARTIFACT_SIZE", str(size)),
    ("CONNECTOR_VERSION", str(document.get("connector_version", ""))),
):
    print(f"{key}={shlex.quote(value)}")
PYTHON
)"

step "Release $CONNECTOR_VERSION, artifact $ARTIFACT_NAME"

if [ "$WHAT_IF_ONLY" = "1" ]; then
    printf '\nWould install %s (%s bytes) into %s.\n' "$ARTIFACT_NAME" "$ARTIFACT_SIZE" "$INSTALL_ROOT"
    printf 'Nothing was downloaded or changed.\n'
    exit 0
fi

step 'Downloading the connector'
curl -fsSL --max-filesize "$MAX_ARTIFACT_BYTES" "$ARTIFACT_URL" -o "$WORK/$ARTIFACT_NAME" \
    || fail 'The connector artifact could not be fetched.'

actual_size="$(wc -c < "$WORK/$ARTIFACT_NAME" | tr -d ' ')"
[ "$actual_size" = "$ARTIFACT_SIZE" ] \
    || fail "The download is $actual_size bytes; the manifest pins $ARTIFACT_SIZE. Refusing."
actual_digest="$(openssl dgst -sha256 -r "$WORK/$ARTIFACT_NAME" | cut -d' ' -f1)"
[ "$actual_digest" = "$ARTIFACT_SHA256" ] \
    || fail "The download digest $actual_digest does not match the pinned $ARTIFACT_SHA256. Refusing."
step 'Artifact digest verified'

VERSION_ROOT="$INSTALL_ROOT/connector/$CONNECTOR_VERSION"
VENV="$VERSION_ROOT/venv"
mkdir -p "$VERSION_ROOT"
# The key and every install directory are the user's alone.
chmod 700 "$INSTALL_ROOT" 2>/dev/null || true
cp "$WORK/$ARTIFACT_NAME" "$VERSION_ROOT/$ARTIFACT_NAME"

if [ ! -x "$VENV/bin/python" ]; then
    step 'Creating an isolated Python environment'
    python3 -m venv "$VENV" || fail 'Could not create the isolated Python environment.'
fi

step 'Installing the connector'
# The verified file on disk, never a name resolved against an index, which would undo every
# check above.
"$VENV/bin/python" -m pip install --quiet --no-input --upgrade "$VERSION_ROOT/$ARTIFACT_NAME" \
    || fail 'The connector could not be installed into its own environment.'

if [ "$SKIP_SETUP" = "1" ]; then
    printf '\nInstalled %s into %s. Setup was skipped.\n' "$CONNECTOR_VERSION" "$VERSION_ROOT"
    exit 0
fi

printf '\n'
if [ -n "$RUNTIME" ]; then
    exec "$VENV/bin/agentnexus-connector" setup --origin "$ORIGIN" \
        --install-root "$INSTALL_ROOT" --runtime "$RUNTIME"
fi
exec "$VENV/bin/agentnexus-connector" setup --origin "$ORIGIN" --install-root "$INSTALL_ROOT"
