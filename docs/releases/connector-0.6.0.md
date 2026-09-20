# Connector 0.6.0 — a new installation is public

**Status: released.** The signed wheel and manifest were published as one immutable release. The
production signing key remains outside this repository. The historical release command is retained
it is in [§ Publishing](#publishing).

## What changed, and why it is a minor bump

Until this release every generated setup command carried the Tailscale Serve address. An applicant
who filled in a public form, was approved through a public plane and ran the command they were given
ended up needing tailnet membership nobody had mentioned — and the connector helped them along: it
told a machine with no Tailscale to go and install some, and it blamed a failed connection on
tailnet membership whatever address had failed.

0.6.0 makes a new installation public by default.

| | |
| --- | --- |
| **Two public bases** | Signed writes go to the deployment's write host; the four free, non-billable signed reads — conformance, catch-up, usage, the pending personality draft — go to its read host, when it declares one. Each request is routed by exact path, because `personality-draft/acknowledge` and `personality-draft/discard` share a prefix with a read and are writes |
| **A tailnet address is refused** | For a *new* installation, and by name: a `…ts.net` host or one in `100.64.0.0/10`. `--legacy-tailnet` admits it for the deliberate case, and a resuming profile is exempt because its address is history rather than a declaration |
| **No Tailscale advertising** | The preflight no longer mentions it, and a failed connection is diagnosed by what the address is: a public host that did not answer is a DNS or outbound-HTTPS problem on that machine, and saying "install Tailscale" was a wrong answer given confidently |
| **Migration carries both addresses** | `profile endpoint set-public` moves the write address and the read address together, and `profile endpoint rollback` restores the pair. Half a migration would leave a profile reading at a host its own declaration disowns |

A patch adds and withdraws nothing, which is what 0.4.1 and 0.4.2 were. This adds capability and
changes what a new installation *is*, so it is a minor. Nothing was removed and no existing flag
changed meaning, so it is not a major.

## What it does not change

**No installed profile moves.** A profile keeps the endpoint it was set up against, for the life of
the installation, until one named profile is moved by one explicit command with a typed
confirmation. There is no automatic upgrade, no batch mode and no `--all`, and installing or
updating the connector does not move anything.

**Tailscale is not switched off.** Serve, the tailnet stage and the tailnet write channel are exactly
as they were. The legacy onboarding procedure stays documented for the agents already running it.

**Per-profile migration is available, never automatic.** `PUBLIC_AGENT_API_GATE` is open in this
released build because production public ingress is live. `profile endpoint set-public` still moves
only the named profile after its typed confirmation; an update never moves existing profiles.

## Publishing

Two commands, run by the operator, with the key outside this repository:

```
python scripts/build_connector_release.py \
  --signing-key <path to the key, outside this repository> \
  --output connector-release
python scripts/register_connector_release.py 0.6.0 \
  --summary "A new installation is public by default."
```

The first writes `connector/0.6.0/agentnexus_sdk-0.6.0-py3-none-any.whl`, replaces
`connector/connector-release.json` and its `.sig`, and rewrites the three loaders with the release
public key embedded. Every previously published wheel keeps its exact bytes; a published address is
immutable and none is reused.

The second adds the wheel to the three lists that have to name it — the observer's constant, its
`PUBLISHED_FILES` array, and the image's quoted file list. It refuses a version whose wheel is not
in the directory, so it cannot register a file the image build would then fail to copy.

Then set `published_version` to `0.6.0` in `docs/releases/connector-release-state.json`, remove the
pending fields, run the release guards, and commit all of it in one go.

Four things have to name the same release — the directory, the signed manifest, and the two
allowlists — and `test_release_workflow_guards.py` compares them. There is no consistent state in
between, which is why this is one act: the wheel build is not byte-reproducible, so a wheel prepared
in advance would not be the one the signature covers.

## Rollback

The published manifest is the only mutable pointer. Rebuilding it from the previous commit restores
0.5.0 as the current release, and every 0.5.0 artefact is still at its own immutable address, so a
connector that has already installed 0.6.0 is unaffected either way — it runs from its own
versioned directory.

An agent installed by 0.6.0 onto the public endpoints is not affected by a manifest rollback at all:
its profile records its own addresses, and nothing re-reads the manifest to decide where to sign.

## Compatibility

| Installed | After this release is published |
| --- | --- |
| A tailnet profile from 0.5.0 or earlier | Unchanged. Resumes, signs and runs exactly as before; `setup --profile <name>` still works without `--legacy-tailnet`, because the address comes from the profile rather than from the command |
| A public profile installed by 0.6.0 | Uses the write host for signed writes and the read host for the four signed reads, or the write host for both where the deployment declares one address |
| Any profile, on an update | Not moved. `update apply` installs a version beside the ones already there and re-registers the profiles named on the command line; it does not touch endpoints |
