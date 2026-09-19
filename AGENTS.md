# Agent and contributor instructions

This repository is part of the AgentNexus portfolio. It owns the complete user-installed Connector: every AgentNexus-owned path that can read a local private key, create a signature, change local configuration, make a network request or install an update. It is public so that all of that can be read before it is run.

## Where issues live

**All issues belong in [`proplaner/agentnexus-original`](https://github.com/proplaner/agentnexus-original/issues).** That repository
remains canonical until an evidenced ownership cutover, and it is the single tracker for the whole
portfolio — source lives in seven repositories, work is traced through one.

**`ppoinha/AIExperiment` is forbidden for all new issue activity.** It is not a fallback, a mirror,
a canonical tracker or a destination for new work, whatever an old link elsewhere suggests. Do not
create, update, comment on, reopen, close or transfer issues there.

Do not open an issue tracker here. This file exists partly to say so.

## Work is issue-led, and test-first by default

Before a change begins, one issue in the canonical repository states the intended outcome, the
in-scope surfaces and explicit non-goals, observable acceptance criteria, the evidence that
establishes each one, and the applicable security, operational and release boundaries.

For a behaviour change: derive a small test from one acceptance criterion, **run it and watch it
fail for the intended reason**, implement the smallest change that makes it pass, then run the
focused and affected suites. For security, authority, data-integrity, release and operations
guards, add a negative or mutation proof: violate the protected condition deliberately, require the
guard to refuse, and restore the implementation before the final verification.

Narrow documented exceptions exist — a documentation-only correction, a measured inventory, an
audit, a mechanical move with byte-identity proof, generated output. An exception is not permission
to skip tests because a test is inconvenient, and the issue must say which exception applies and
why.

The canonical rules, which this file points at rather than restates:

- [`AGENTS.md`](https://github.com/proplaner/agentnexus-original/blob/main/AGENTS.md)
- [`docs/ai/ISSUE_LED_DEVELOPMENT.md`](https://github.com/proplaner/agentnexus-original/blob/main/docs/ai/ISSUE_LED_DEVELOPMENT.md)
- [`docs/ai/GITHUB_ISSUE_TRACEABILITY.md`](https://github.com/proplaner/agentnexus-original/blob/main/docs/ai/GITHUB_ISSUE_TRACEABILITY.md)
- [`docs/ai/CI_RUNNER_POLICY.md`](https://github.com/proplaner/agentnexus-original/blob/main/docs/ai/CI_RUNNER_POLICY.md)

Where this file and a canonical document disagree, the canonical document wins and this file is the
defect.

## Where CI may run

This repository is **public**, and that changes the rule rather than softening it.

```
runs-on: ubuntu-latest
```

GitHub-hosted runners only. A workflow here must never select `private-ci`, `self-hosted`,
`agntnexus-linux`, `agntnexus-windows`, `wsl-14900k` or `windows-14900k`, and must hold no
organisation secret. A public pull request can contain code nobody has reviewed; letting it execute
on an operator-maintained machine would hand that machine to whoever opened the pull request.

## What needs an owner decision, not a pull request

None of the following is authorised by an issue, a green pipeline or a review:

- a tag, a release, a package, wheel, container or registry publication, or any signing action;
- a secret, private key, GitHub App permission, personal access token or deploy key;
- a cloud resource, production access, or a DNS, TLS, firewall or cluster change;
- a repository transfer, archival, deletion, visibility change or ownership cutover.

An issue tracks work. It grants no merge, push, release, deployment or closure authority.
