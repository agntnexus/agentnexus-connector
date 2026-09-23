# Agent and contributor instructions

This repository is part of the AgentNexus portfolio. It owns the complete user-installed Connector: every AgentNexus-owned path that can read a local private key, create a signature, change local configuration, make a network request or install an update. It is public so that all of that can be read before it is run.

## Where the documentation is

[`docs/README.md`](docs/README.md) indexes what this repository documents and records what it
adopted from the umbrella archive, with one disposition per assigned record. Cross-cutting policy is
not duplicated here; it lives in
[`docs/ai/README.md`](https://github.com/agntnexus/agentnexus/blob/main/docs/ai/README.md) in the
umbrella.

Everything under `docs/` is public, because this repository is. A page that names an operator's
machine, a private host or a personal path does not belong here even when it is true.

## Where issues live

**Issues for the portfolio belong in
[`agntnexus/agentnexus`](https://github.com/agntnexus/agentnexus/issues).** The umbrella is the
tracker: work crossing a repository boundary — a contract, a release, a shared policy, a platform
gate — is traced there.

This repository keeps Issues for work that is genuinely internal to it. When such work changes a
cross-cutting fact, the durable record in the umbrella changes in the same slice.

**`ppoinha/AIExperiment` is forbidden for all new issue activity.** It is not a fallback, a mirror,
a canonical tracker or a destination for new work, whatever an old link elsewhere suggests. Do not
create, update, comment on, reopen, close or transfer issues there.

**`proplaner/agentnexus-original` is historical.** It is private, unchanged, and not the tracker.
Its documentation is archived in the umbrella at `docs/history/original/`, marked non-normative. An
older instruction found there that names it as canonical is superseded by this file.

## Work is issue-led, and test-first by default

Before a change begins, one Issue in the tracker named above states the intended outcome, the
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

The portfolio rules, which this file points at rather than restates:

- [`AGENTS.md`](https://github.com/agntnexus/agentnexus/blob/main/AGENTS.md)
- [`docs/ai/ISSUE_LED_DEVELOPMENT.md`](https://github.com/agntnexus/agentnexus/blob/main/docs/ai/ISSUE_LED_DEVELOPMENT.md)
- [`docs/ai/GITHUB_ISSUE_TRACEABILITY.md`](https://github.com/agntnexus/agentnexus/blob/main/docs/ai/GITHUB_ISSUE_TRACEABILITY.md)
- [`docs/ai/TESTING.md`](https://github.com/agntnexus/agentnexus/blob/main/docs/ai/TESTING.md)
- [`docs/ai/CI_RUNNER_POLICY.md`](https://github.com/agntnexus/agentnexus/blob/main/docs/ai/CI_RUNNER_POLICY.md)

Where this file and a portfolio document disagree, the portfolio document wins and this file is the
defect — **except** for the boundaries below that are this repository's own. Those are stated here
because they are stricter than the portfolio default, and a stricter local boundary is never
overridden by a more permissive shared one.

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
