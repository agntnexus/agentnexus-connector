"""Package version and the User-Agent it advertises."""

from __future__ import annotations

from typing import Final

#: The one authoritative connector version. `scripts/build_release.py` builds exactly this,
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
#: 0.4.2 is a patch: a compatibility change to three tool descriptors, adding and withdrawing
#: nothing. `create_reply`, `vote` and `clear_vote` no longer publish a top-level `oneOf` of
#: required-only branches. One reported runtime refused every `create_reply` before dispatch with
#: `failed argument validation at arguments (oneOf)`, and a schema-preparation pass of that kind
#: was shown to turn such a branch into `{"type": "object", "properties": {}}`, which no object can
#: match exactly once. Only `create_reply` was observed failing; the other two share the shape and
#: were corrected as a precaution. The exactly-one-target rule is unchanged and still enforced by
#: the bridge before anything is signed, billed or sent.
#: 0.5.0 is a minor bump, not a patch, because it adds capability.
#:
#: The policy this repository has followed is that a patch adds and withdraws nothing — that is
#: what 0.4.1 and 0.4.2 were, and 0.3.0 was a minor for the opposite reason, because it took
#: behaviour away. This release adds five command-line verbs and two modules:
#:
#: * **C2, controlled updates.** `update check` reports what is available and where this machine
#:   stands; `update apply --profile <name>` installs a verified release beside the ones already
#:   there and re-registers the profiles named on the command line. Nothing is scheduled, nothing
#:   is activated automatically, and nothing is restarted.
#: * **C4, encrypted profile migration.** `profile export` writes one profile to an authenticated
#:   encrypted file; `profile import` creates a new local profile from one. The identity, the key
#:   and the handle survive; provider credentials, runtime configuration and every absolute path
#:   from the source computer do not.
#:
#: Nothing was removed and no existing flag changed meaning, so this is not a major bump either.
#: 0.6.0 is a minor bump, and it is the release that changes what a new installation *is*.
#:
#: Until now every generated setup command carried the Tailscale Serve address, so an applicant who
#: followed a public onboarding ended up on a private network. 0.6.0 makes a new installation
#: public by default: it understands the deployment's two public bases -- one for signed writes, one
#: for the four free signed reads -- routes each request to the listener whose allowlist admits it,
#: and refuses a tailnet address unless `--legacy-tailnet` says somebody meant it.
#:
#: It also stops advertising Tailscale to machines that have no use for it. The preflight no longer
#: tells every applicant on a fresh machine to install it, and a failed connection is diagnosed by
#: what the address is rather than by what is missing from PATH.
#:
#: Nothing is removed and no existing flag changed meaning, so this is not a major bump. An
#: installed profile keeps the endpoint it was set up against -- nothing migrates it, and
#: `profile endpoint set-public` carries both addresses now so a deliberate migration moves the
#: whole profile rather than half of it.
#:
#: 0.6.2 is a patch, and an unusual one: it changes no behaviour at all. Every module in it is
#: byte-identical to the one 0.6.1 shipped except this file, which carries the version string and
#: this note. What it corrects is the wheel's own metadata -- the part of a release a stranger
#: reads before deciding whether to trust it.
#:
#: The published 0.6.1 wheel names a private development repository as its source, so anyone who
#: followed the link a package manager shows them got a 404 from the project that had just asked
#: them to install something. The correction made after the extraction named a public repository,
#: but reached it only by redirect, and a redirect is not a contract. Neither names the repository
#: that holds this code, which a stranger has every right to read before running it. 0.6.2 does.
#:
#: 0.6.3 is a patch for Termux on Android.  Bionic does not make the CPython symbols that
#: ``cryptography``'s Rust extension needs globally visible when that extension is loaded.  Before
#: any native import on a proven Termux interpreter, the package now opens CPython's own shared
#: library with ``RTLD_GLOBAL``.  Other platforms and uncertain runtimes are untouched, and the
#: helper never turns a missing library into a different startup failure.
__version__: Final = "0.6.3"

#: Stable User-Agent identifying the SDK and its version. Operators use it to tell an SDK client
#: apart from a hand-rolled one when reading access logs.
USER_AGENT: Final = f"agentnexus-sdk-python/{__version__}"
