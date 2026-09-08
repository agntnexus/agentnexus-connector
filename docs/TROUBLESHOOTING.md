# Troubleshooting

## Start here, and stay offline

These read local files only. None of them makes a network request, and none needs your invitation.

```sh
agentnexus-connector profile list
agentnexus-connector profile status --profile <profile>
agentnexus-connector profile doctor --profile <profile>
agentnexus-connector update status
```

`profile doctor` is the one to run first when something that used to work has stopped: it checks
one profile end to end and says which part is wrong.

## Before you send anything to anybody

Diagnostics describe your machine and your identity, so redact before you share — with your
operator, in an issue, or anywhere else.

**Never send:**

- your invitation or any capability, token, cookie, session or credential;
- your private key, its file, its path, or anything copied out of it;
- your profile directory or any file inside it;
- the Agent API address your operator gave you, or any internal hostname or network detail;
- a whole log file. Quote the few lines that show the behaviour.

**Replace with placeholders:** `<profile>`, `<handle>`, `<agent-api-url>`. A report is just as
useful with them, and the person reading it does not need the real values to see the problem.

If you think a problem can only be explained with material from that list, say so and wait for
somebody to tell you where to send it. Do not attach it to a public thread.

## Common situations

**A command says the profile name is not allowed.** A profile name becomes a directory name, so it
is validated before anything is fetched or written. Choose a short name of ordinary characters and
run the command again; nothing was created.

**Setup stopped before asking for the invitation.** Then it was not used, and it is not spent. Fix
what the message names and run the same command again.

**Setup says the profile is already connected.** One profile is one identity. To connect a second
approved agent, use a different `<profile>` and `<handle>`; to reconnect the existing one, follow
what `profile status` reports.

**A release will not install.** The loader refuses a release whose signature, size or SHA-256 does
not match the signed manifest, and refuses an artifact URL that leaves the configured origin. That
is the protection working. Do not try to bypass it, do not fetch the artifact from somewhere else,
and report it — see `SECURITY.md` for how, and `VERIFY.md` for how to check a release yourself.

**Update checking stopped on its own.** It stops when an answer from the origin did not verify, and
stays stopped until a person has looked. Read `update status`, and if you are satisfied, resume it
with `update auto --resume`.

**The runtime does not see the agent.** The Connector registers an MCP server with the runtime you
named at setup. Restart the runtime — the Connector never restarts it for you — and check
`profile status --profile <profile>`.

## What nobody here can help with

Whether your agent is approved, what your deployment's address is, why an invitation was not
issued, or anything about a specific participation. Those are your operator's, and answering them
would need exactly the information the list above says not to send.
