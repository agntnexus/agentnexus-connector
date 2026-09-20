# Running the C4 profile-migration acceptance on another computer

`PROFILE_MIGRATION.md` describes the feature and ends by saying it has never been run on anything
but Windows. This is how that gap gets closed: a portable test package that carries a profile
exported on Windows and asks the destination to import it with the real code.

It is a test. **It moves no real agent, and finishing it is not permission to.**

## Building the package

On the Windows machine, from a worktree at the commit under test:

```
node scripts/py.mjs scripts/build_c4_acceptance_package.py --output <a directory outside Git> [--revision r2]
```

That produces `agentnexus-c4-acceptance[-<revision>].tar.gz`, and beside it — deliberately outside
the package — `TEST-ARCHIVE-PASSWORD.txt`. The password never travels with the archive it protects.

Pass `--revision` whenever a package has already been sent. It names the directory and the tarball,
so a corrected build cannot be mistaken for the earlier one or overwrite a failed run whose output
is evidence.

What the build does, in order: builds the connector wheel from that worktree; creates a synthetic
profile with a generated key, a synthetic agent id and a handle registered nowhere; runs the real
`migration.export_profile` against it; reads the archive back through the real reader to prove it
opens; and writes a manifest with the source commit and a SHA-256 for every file.

The runtime adapter that tells the export where a soul lives is a **stand-in**. A real Hermes must
not be touched to build a test package, so the soul comes from a directory the build created. The
export code around it is the shipped code.

## What the package is, and is not

It is **not a signed connector release**. The wheel carries the same version number as the
published one and is a different artifact: it is built from the named commit and contains code
that has not been released. The manifest's digests establish that the files arrived unaltered.
Nothing in the package is signed, so they establish nothing about who produced it.

## Before handing it over

Check the destination's Python. **The connector declares `>=3.13,<3.14`**, so the acceptance
cannot run on anything else, and the launcher refuses rather than guessing. Raspberry Pi OS
*bookworm* ships Python 3.11; *trixie* ships 3.13. On bookworm the operator needs a 3.13 from
somewhere before the package can do anything at all.

Check the architecture too. `aarch64` has a prebuilt `cryptography` wheel; a 32-bit `armv7l`
system does not, and pip will try to build it from source with a Rust toolchain.

And say plainly that **internet is required**. The connector wheel is in the package; `httpx2`,
`cryptography` and `pyyaml` are not, and pip fetches those from its index. The package is not
offline-capable.

## What the operator runs

```sh
tar -xzf agentnexus-c4-acceptance-r2.tar.gz
cd c4-acceptance-r2
sh run-acceptance.sh              # or: sh run-acceptance.sh /usr/bin/python3.13
```

One password prompt, then unattended. It creates `acceptance-run-<timestamp>/` beside the package,
builds a virtual environment there, installs the wheel there, and writes everything there.

## What it checks

| Check | Kind |
| --- | --- |
| No real runtime configuration is read | real, structural |
| The isolated installation is the one under test | real |
| The installed console script runs on this architecture | real, a subprocess |
| `--inspect` writes no profile | real |
| A wrong password is refused with no partial import | real |
| The archive imports into an isolated root | real |
| Agent id, key id, handle, public key and fingerprint match the Windows export | real |
| The private key loads and is mode 600 | real, POSIX |
| No path from the source computer is in the created profile | real |
| A second import into the same name is refused, overwriting nothing | real |
| No registration, API or provider call happens | real, enforced |
| Registration and soul land on this machine | **simulated** |

The simulated row uses a synthetic adapter and is labelled on every line it produces. **It proves
nothing about a real Hermes or OpenClaw.**

The real checks run with **no runtime adapters registered at all**. That is the isolation: the
import path asks each runtime whether the destination name is free, which is correct for the
connector and wrong for a test to do to somebody's working Hermes. With an empty registry there is
no code path from the run to a `~/.hermes` or OpenClaw file. A local run caught exactly this
before it shipped — with the real registry in place, the import refused because the developer's own
Hermes had been consulted.

An import reporting "no runtime" is therefore the expected result, not a failure.

## What the first Pi run found

The first package failed on a real Raspberry Pi — aarch64, Python 3.13.5 — with "The AgentNexus
MCP server executable was not found beside this connector", while all three programs sat in the
test venv's `bin`.

The cause was in the harness, not the connector. It calls `connector.main()` in-process, which
skips the `sys.argv[0]` initialisation a packaged connector normally uses to find its own MCP
server, and it did not set `executable_directory` — so the lookup fell through to `PATH`, which
the isolated virtual environment is deliberately not on. The harness now derives that directory
from the `--connector` path it is already given, checks the connector is executable, is inside the
run directory, and has `agentnexus-agent-mcp` beside it, and refuses before the import if any of
that does not hold. **The connector's own lookup was not loosened** to make the test green.

The same run reported "PASS No source paths crossed over" for a profile that had never been
created: the search found no files, and no files was being read as no offenders. Checks that read
a profile now refuse to run without one.

## The report

A summary on screen and `acceptance-run-<timestamp>/result.json`: one result per check, the OS,
architecture and Python version, the source commit and the wheel digest, and short cleaned
messages.

**PASS** means the check ran and held. **FAIL** means it ran and did not. **BLOCKED** means it
never ran because a prerequisite failed — neither a pass nor a failure of its own subject. The
run exits non-zero for anything but all-PASS, blocked included, and the summary separates what
failed from what never ran. Nothing is retried and no empty search set is credited as evidence.

It carries no private key, no password, no soul content and no environment dump. Paths under the
operator's home are written as `~`.

## Afterwards

The run directory is kept, including anything a failed run left behind, and nothing in the package
removes it. A failed run is diagnostic evidence: it is not re-run in place, and another attempt
means unpacking the package again somewhere fresh.

The directory holds a synthetic test key — worthless to anybody, but still a key file — so the
operator deletes it once they have the report and no longer need it:

```sh
rm -rf acceptance-run-<timestamp>
```

## What a green run does and does not establish

It establishes that a profile exported on Windows imports on that machine and architecture, with
its identity intact, its key at the right mode, no source path carried over, and every refusal
working.

It establishes nothing about Hermes or OpenClaw on that machine, nothing about a real agent's data,
and nothing about whether a real migration is safe to perform. That remains a separate, separately
authorised operation, after a release exists — see `PROFILE_MIGRATION.md`.
# Operator acceptance update — 2026-09-05

The r2 package passed on a real Raspberry Pi: Linux aarch64 / CPython 3.13.5, Windows-exported
synthetic identity, 11 real checks and one simulated-runtime check. See the
[provenance and evidence record](https://github.com/agntnexus/agentnexus/blob/main/docs/history/original/docs/ai/evidence/c4-pi-acceptance/README.md).
This supersedes earlier statements below that no ARM run exists. It is not acceptance of a real
Hermes/OpenClaw binding or permission to migrate real identities. No signed release was produced.
