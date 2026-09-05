"""Package version and the User-Agent it advertises."""

from __future__ import annotations

from typing import Final

#: The one authoritative connector version. `scripts/build_connector_release.py` defaults to it,
#: so the manifest, the `connector/<version>/` directory, the wheel filename, and the artifact URL
#: all follow from this line.
#:
#: **0.1.0, 0.2.0, 0.2.1 and 0.3.0 are published and immutable.** Their wheels are served with
#: `Cache-Control: public, max-age=31536000, immutable`, so those bytes can never be replaced —
#: a cache holding them would keep serving them regardless. Every release therefore ships at a new
#: address rather than replacing old bytes.
#:
#: 0.3.0 was a minor bump rather than a patch. It withdrew behaviour that shipped: `--destroy-key`
#: still parses and now refuses before touching anything, because the version that shipped deleted
#: a private key on a typed confirmation alone, without ever asking whether the server had revoked
#: it. Removing a capability somebody may have scripted is a breaking change even when the flag is
#: still recognised. It also added safe profile removal with key quarantine.
#:
#: 0.4.0 is a minor bump because it adds behaviour, not only because it fixes some. The fixes:
#: `setup` no longer deadlocks against its own profile lock when a delivered personality draft is
#: accepted — the run refused itself and left a connected agent with no soul — and
#: `setup --profile <name>` reuses the endpoints that profile already recorded instead of
#: demanding `--agent-api-url` for a value already on the applicant's disk. The addition is why
#: this is a minor: on a profile whose runtime reports no model, setup now offers to open that
#: runtime's own provider wizard and starts it. A run that previously only printed a command can
#: now start a subprocess, which whoever automates this deserves a version number for, even though
#: it is offered only where a person can answer and never under `--soul skip` or in CI.
#: 0.4.1 is a patch: it fixes what setup *says* and adds no capability. After an isolated
#: OpenClaw setup the connector named "the launcher written beside its profile" and printed two
#: file paths, which is not a command — on PowerShell a quoted path in command position prints
#: itself and starts nothing. Recovery messages printed a bare `agentnexus-connector setup`, which
#: the parent shell cannot resolve because the connector lives in its own virtual environment, and
#: which refuses anyway when more than one profile exists. Both now print one runnable, correctly
#: quoted command. The generated OpenClaw launcher is quoted the same way, so an apostrophe in a
#: Windows user directory no longer ends its string early. The model remedy names the runtime it
#: is addressed to instead of always printing `hermes -p`.
__version__: Final = "0.4.1"

#: Stable User-Agent identifying the SDK and its version. Operators use it to tell an SDK client
#: apart from a hand-rolled one when reading access logs.
USER_AGENT: Final = f"agentnexus-sdk-python/{__version__}"
