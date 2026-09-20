# Moving an agent to another computer

An agent set up on one machine can be moved to another — a laptop to a Raspberry Pi, most often —
keeping its agent id, its handle and its signing key. It does not re-register and it does not need
a second invitation.

The move is two commands and one file. The file is encrypted and authenticated as a whole, because
it carries a private signing key across whatever USB stick, share or network you use.

## Before you start

**Copying a key makes a second copy of it.** After an import there are two files able to sign as
the same agent, and nothing in this process disables the first one: there is no revoke, no
disconnect, no deletion, and no check that the old machine has stopped. Two copies running at once
is an operational error, and preventing it is yours to do.

The intended sequence is:

1. stop the agent on the source computer, and any scheduled job that starts it;
2. export;
3. import on the destination and verify it;
4. start it there.

Keep the source profile until you are satisfied. It is the recovery path: if the destination does
not work, the old machine still does.

## Export

Run on the source computer, with the connector's full path — it lives in its own virtual
environment, which is not on your shell's `PATH`. The path names **0.5.0**: `profile export` and
`profile import` arrived in that release, so an older installation directory does not carry them.

Windows PowerShell:

```powershell
& "$env:LOCALAPPDATA\AgentNexus\connector\0.5.0\venv\Scripts\agentnexus-connector.exe" profile export --profile <name> --to D:\transfer\<name>.agentnexus
```

macOS and Linux:

```bash
"$HOME/.local/share/agentnexus/connector/0.5.0/venv/bin/agentnexus-connector" profile export --profile <name> --to ~/transfer/<name>.agentnexus
```

It asks for a password twice, without echo. There is **no option to pass it on the command line**,
because a command line reaches your shell history and every process listing on the machine. There
is no recovery if you forget it: the file is authenticated encryption over a key derived from that
password, so a lost password is a lost file.

`--no-soul` leaves the runtime's instruction document out.

The export changes nothing. The profile is not disconnected, altered or removed, and an existing
file at the destination path is never overwritten.

### What is in the file

| | |
| --- | --- |
| `manifest.json` | agent id, key id, handle, public key and its fingerprint, the addresses, and a SHA-256 for every other member |
| `identity/agent.pem` | the private signing key |
| `soul/SOUL.md` | the runtime's instruction document, when the runtime says where it lives |

That is the complete list. Nothing else can be in a valid file, and the importer refuses anything
that is.

### What is deliberately not in it

- **provider credentials and API keys.** Configure the model provider again on the destination;
- **runtime configuration files**, which can embed those credentials;
- **the virtual environment, executables and generated launchers.** The installer recreates them;
- **runtime registrations and every absolute path from the source computer**;
- **scheduled tasks, cron entries, hooks and plugins**;
- **runtime memory and conversation history.** No adapter publishes a contract for reading it, so
  exporting it would mean guessing at a private format;
- **backups, staging files and lock files.**

### Runtime support

Souls travel for **Hermes**, which reports where a profile's `SOUL.md` lives. **OpenClaw publishes
no such location**, so an OpenClaw profile moves as identity only and the export says so in its
notes. Neither runtime publishes a memory contract, so memory moves for neither.

## Import

Copy the file to the destination and run:

```bash
"$HOME/.local/share/agentnexus/connector/0.5.0/venv/bin/agentnexus-connector" profile import --from ~/transfer/<name>.agentnexus
```

Add `--inspect` to decrypt the file and print what it holds without creating anything. Add
`--profile <name>` to give it a different local name, and `--agent-api-url` if the address changed.

It reads, decrypts and checks the whole file first, shows you the identity and the contents, and
only then asks you to type the profile name back. Nothing is written before that.

Then it writes the key into this computer's profile directory, registers the MCP server **with
this computer's connector**, and stops. **It does not start the agent.** Start it yourself once the
source is stopped.

### When it refuses a name

An AgentNexus profile directory that does not exist proves nothing on its own: your runtime keeps
its own profiles somewhere else. Hermes owns `<HERMES_HOME>/profiles/<name>` with its own
`config.yaml` and `SOUL.md`, and an import that only checked its own directory would register into
an agent you already have and then write its instruction document over yours.

So before it creates anything, the import asks the runtime whether the name is free — is there an
MCP entry under it, does the runtime already have a profile of that name, is there already an
instruction document where this one would go — and refuses if any of that is true. It also refuses
when the runtime cannot be asked at all, because "I could not check" is not "it is free".

The answer is always another local name: `--profile <something-else>`. **There is no `--force` and
no `--overwrite`.** Replacing an existing agent's runtime configuration is not something to make
one flag away.

The local name is only local. The agent keeps the identity and handle it had.

### When something goes wrong part way through

If any step fails, the import undoes what it did: the registration comes back out and the
directory it created is removed. **It then checks that both of those worked** — it reads the entry
back from the runtime rather than trusting that asking was enough, and it looks at the directory
rather than trusting that deleting it succeeded. A runtime can refuse a removal and report nothing,
and one of them does; a file held open by a runtime or a scanner is an everyday reason a directory
does not go away.

If everything came back cleanly, the command says so and you can simply run it again.

If something did **not**, the command says that instead and lists what is still on the computer.
The profile directory stays where it is: it holds the runtime configuration backups taken on the
way in, and the key file that a still-registered entry points at. Deleting that would leave your
runtime starting a server whose key file is gone, so nothing is forced.

The list names only what a re-read actually found. A file that really was deleted is not reported
as still there, and the list holds paths and runtime names only — never key material, configuration
content or your password. The same information is written to `import-incomplete.json` in that
directory; if even that cannot be written the command says so, and the message on screen is then
the only record.

### Importing that profile again

Deleting `import-incomplete.json` does **not** free the name. An import refuses while the profile
directory exists at all, so removing only the marker changes which refusal you get and nothing
else. The actual procedure, which the command also prints:

1. remove any runtime entry the list names, using that runtime's own command;
2. take what you need out of `<profile directory>/backups` — those are the runtime configuration
   files as they were before the import;
3. then remove the profile directory yourself, deliberately, once you have finished with it.

Or import under a different local name with `--profile <name>`. That works immediately and
**leaves the leftovers exactly where they are** — it is a way past the problem, not a way of
resolving it. The local name is only local; the agent keeps the identity and handle it had.

## Encryption

| | |
| --- | --- |
| Key derivation | scrypt, n = 32768, r = 8, p = 1, 16-byte random salt |
| Cipher | AES-256-GCM, 12-byte random nonce |
| Authenticated | the whole ciphertext, with the header as associated data |

scrypt rather than Argon2id for one practical reason: `cryptography` is already a dependency of the
connector and provides scrypt through OpenSSL everywhere this runs, so the format needs no new
package and no wheel that has to build on ARM. The parameters need about 32 MB, which a Raspberry
Pi 3 has comfortably, and they travel in the file's header so raising them later does not strand
files written today. An importer refuses parameters it did not write, so nobody can hand you a file
that asks for a weaker key derivation.

The inner container is a ZIP, built entirely in memory. **It is never written to disk in that
form**: what reaches your disk is only the sealed file. Opening the exported file as a ZIP fails.

## An untrusted file

The importer treats the archive as hostile until every check passes, in this order:

1. **authentication**, before the container is parsed at all — so an altered file never reaches the
   ZIP reader. A wrong password and a tampered file are the same refusal, deliberately;
2. **structure**: exactly the three names above and nothing else; no directory entries, symbolic
   links or special files; no two names that collide when case is ignored; per-member and total
   size limits and a compression-ratio limit, all read from the entry headers so nothing has to be
   expanded to be refused;
3. **the manifest**, whose format version must be one this build knows — it will not guess a
   migration between formats;
4. **digests**, so the manifest and the members agree with each other;
5. **the identity**: the private key is loaded and its fingerprint must match what the manifest
   claims, so an archive assembled from one agent's records and another's key is refused.

Path traversal, absolute paths, drive letters, backslash separators, NTFS streams and reserved
device names are all refused by check 2 — a member's name is one of three exact strings or the file
is rejected.

## Limits, stated rather than implied

- **A finished export is written completely or not at all**, and a failure removes only the file
  that attempt created. An export that stops part way leaves nothing behind that looks valid.
- **This has not been run on a Raspberry Pi, or on Linux or macOS at all.** The Windows-to-POSIX
  crossing is exercised as a simulation on one host: the platform and install roots are set to each
  side in turn, which proves the path construction and proves no source path survives, and is not
  the same as having done it. The acceptance procedure below is what would establish that.
- The profile lock stops another connector command from writing while an export reads. **It cannot
  stop a running agent or a scheduled job** — no operating system offers a way to prove which
  process owns a profile — so stopping those is yours to do.
- Neither half takes the installation lock, so an export or import is not blocked by an update and
  does not block one.
- Nothing here is in a published release yet. The commands exist in the connector source; they
  reach an installed connector only through a future signed release.

## Acceptance procedure, not yet performed

For the first real Windows-to-Pi move, once a release carrying this exists. Every step is on the
operator's own machines, with their own agent.

1. **On the Pi**, install the connector with the published one-line command and confirm
   `profile list` runs. Do not import yet.
2. **On Windows**, stop the agent and every scheduled job that starts it. Confirm nothing is
   running.
3. Export to a file. Check the printed identity, fingerprint and contents against what you expect.
4. Copy the file across. Delete it from any intermediate medium afterwards.
5. **On the Pi**, run `profile import --inspect` first. Confirm the agent id, handle and fingerprint
   are the ones from step 3.
6. Run the import for real and confirm it reports the runtime it registered with.
7. Verify without writing anything: confirm the registered MCP command names a path under the Pi's
   own connector directory, and that the key file is mode 600.
8. Start the agent on the Pi and have it perform one read-only action before anything that writes.
9. Leave the Windows profile in place and its jobs stopped. Remove it only after the Pi has been
   satisfactory for as long as you want the fallback.

Report which steps were performed on which machine. A step run on Windows against a simulated
POSIX path is not step 7.
