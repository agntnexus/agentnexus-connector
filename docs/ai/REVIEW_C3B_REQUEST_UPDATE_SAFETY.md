# Review — C3-B: request-triggered update checks, security and operations

Independent review of the **merged** C3-B code, not a second design. The brief was to verify what
is on `main`, change product code only against a reproducible defect with a failing-first
regression test, and otherwise report.

- **Branch**: `review/c3b-request-update-safety`
- **Base**: local `main` `2811f16`
- **Commits**: `d8d5e51`, `0ddab3d` — two fixes, seven new regression cases
- **Local only. Not pushed, not CI-verified, not live, not merged into `main`.** No release, no
  deployment, no wheel, no signed manifest, and no change to the Pi, cron, Task Scheduler, Hermes,
  Tailscale, DNS, a provider or production.

Reviewed: `CONNECTOR_AUTOMATIC_UPDATE_SAFETY_PLAN.md`, `HANDOFF_C3B_REQUEST_TRIGGERED_UPDATE_CHECKS.md`,
`CONNECTOR.md` §"Being told when a release exists", `autocheck.py`, `updater.py`, `mcp_server.py`,
`connector.py`, `profiles.py`, `release.py`, `test_connector_autocheck.py` and the existing update,
profile, locking, MCP-server and bridge tests.

## 1. Findings

Four defects, all reproducible, each with a test that fails against the code before its fix. Three
are in one place — what the status document says about a check that stopped — and one is in the
fetcher.

### F-1 — `update apply` silently cleared a halt (trust boundary) · fixed in `d8d5e51`

**Risk: moderate.** An untrusted answer is supposed to stop automatic checking until the owner
looks at it. `autocheck.record_installed_version`, which `update apply` calls after a successful
install, recomputed `state` from the installed version **unconditionally**. A halted status
therefore moved to `up-to-date`; `next_check_after` was already `None` because halting clears it,
so `due()` said yes and the very next request resumed checking — with no `update auto --resume`,
and with `update status` no longer printing "Owner action required" while the reason stood
unresolved.

This contradicts three accepted statements: the C3-A matrix ("halt automatic operation, record the
reason, **require an explicit owner re-enable**", and "re-enabling after a `halted` state" listed
under *owner action required*), `CONNECTOR.md` ("stops automatic checking entirely and waits for
you"), and C3-B's own handoff ("no retry until `update auto --resume`").

**Reproduction** — enable, one successful check, one answer that does not verify, then:

```
after a successful check       state=available   next_check_after=2026-09-07T12:00:00Z  due=True
after an untrusted answer      state=halted      next_check_after=None                  due=False
after record_installed_version state=up-to-date  next_check_after=None                  due=True
  last_error_code still=untrusted
  status says 'Owner action required': False
```

**Fix**: leave the state alone when it is `halted`. The **floor still rises** there — a release
whose signature and digest `update apply` has just verified is at least as trustworthy as one a
check merely read about — because the floor and the state are different claims.

**Tests**: `test_installing_a_release_does_not_clear_a_halt`,
`test_the_owner_is_still_told_the_check_halted`. Both fail before the fix
(`assert 'up-to-date' == 'halted'`).

### F-2 — `update auto --enable` promised a cadence it could not keep · fixed in `d8d5e51`

**Risk: low, but it is a false statement about a security-relevant state.** A halt keeps `due`
False whatever `enabled` says. On a halted installation the command still printed:

```
  Automatic update checking is on.
  At most one check every 86400 seconds, triggered by a normal request.
```

No request would ever check again, and nothing named `--resume`. Verified with a refusing fetcher:
after `--enable`, a request 30 days later still made **zero** network calls.

**Fix**: report the halt and the one command that clears it, instead of an interval that cannot
happen. `--enable` deliberately does **not** clear the halt: keeping one and only one way out of
that state is what makes F-1's fix coherent.

**Test**: `test_enable_on_a_halted_check_does_not_promise_a_cadence_it_will_not_keep`.

### F-3 — the fetch budget was not a deadline · fixed in `0ddab3d`

**Risk: moderate; availability of the agent session.** `serve` handles one message at a time, so
whatever a check spends between two messages, the next tool call waits. The module documented
`FETCH_BUDGET_SECONDS` as "enforced per request … so an unresponsive origin cannot hold the loop
for a read timeout", but the clock was only consulted *before* each of the two requests. Nothing
looked at it once a download had started.

A read timeout does not close that: it bounds the gap between two chunks, not their number. An
origin that trickles bytes satisfies every individual read and never finishes, and the 65,536-byte
manifest cap is never reached.

**Reproduction** (injected clock, stubbed transport, no network): an origin sending one byte every
30 seconds held the fetcher for **1200 simulated seconds against an 8 second budget — 150× over**,
with no timeout raised anywhere.

**Fix**: check the deadline between chunks as well. Giving up is classified **transient**, not
untrusted: a slow origin is a transport problem, and halting on one would switch checking off
permanently for something a retry fixes.

**Tests**: `test_a_trickling_origin_is_given_up_on_inside_the_budget`,
`test_giving_up_on_a_slow_origin_is_transient_rather_than_untrusted`, plus the control
`test_an_origin_that_answers_promptly_is_not_cut_off`.

### F-4 — an unreadable status file was reported as an absent one · fixed in `d8d5e51`

**Risk: low.** `load` returns `None` for a file that is absent and for one it cannot parse, and
`update status` printed "Not configured. No check has ever run and no status file exists." while
the file sat there — hiding both that checking had silently stopped and that the replay floor had
gone with it. Reproduced by writing `{ truncated` to `update-check.json`.

**Fix**: distinguish the two, and say that re-enabling starts the floor again from the versions
installed here. The file is left untouched.

**Test**: `test_a_status_file_that_cannot_be_read_is_not_reported_as_absent`.

## 2. Invariants verified and left unchanged

### JSON-RPC and request safety

- **stdout carries protocol only.** `autocheck` contains no `print` and no stdout write of any
  kind; the single notice goes to `sys.stderr` in `mcp_server._after_response`, and the CLI notice
  goes to `environment.stderr`.
- **The check begins only after the answer is gone.** `serve` calls `_write`, which writes and
  flushes, and only then `_after_response()`. The triggering response cannot be delayed, altered
  or failed by it.
- **Two layers of containment.** `maybe_check` catches `Exception` around everything and returns an
  `Outcome`; `_after_response` catches again around that. `BaseException` — `KeyboardInterrupt`,
  `SystemExit` — is correctly *not* swallowed.
- **An invalid installation path ends the matter.** `installation_from_executable` answers only for
  a real `connector/<version>/venv/` tree; a development checkout, a `pipx` layout or a bare
  `argv[0]` yields `None` and nothing is read, created or written.
- **Several messages**: `serve` is single-threaded, so ordering is trivially serial. The in-process
  gate `poll_is_allowed` will not even read the status file more than once every 60 s.
- Observed and accepted: `_after_response` runs only after a **successful** result. Parse errors,
  invalid requests, `_RpcError` and internal errors all `continue` past it. Harmless, arguably
  right; recorded because it is not stated anywhere.

### Trust and replay

- Artifact URLs are pinned to the configured origin in `release.parse_manifest`; the signature is
  verified over the exact bytes and the document is re-canonicalised, so a reordered document
  cannot inherit a signature.
- `trusted_release_key()` is called **before** any fetch, so an unstamped build halts and never
  reaches the origin at all.
- A non-HTTPS origin raises a plain `UpdateError`, which classifies as untrusted and halts —
  correctly, since retrying it would only repeat the refusal.
- Digest and size are **not** reached, and should not be: C3-B downloads no artifact. Asserted by a
  test that pins the two requested URLs.
- `release.py` validates `released_at` for shape only and never compares it — confirmed at
  `parse_manifest`. The monotonic floor in `autocheck` is what closes that, it is persisted in the
  status document, and it survives a restart.
- `--resume` keeps both floor fields. Confirmed by reading `_run_update_auto` and by the existing
  test; F-1's fix removes the other route out of a halt, so the floor is now the only thing that
  survives a halt and the halt is the only thing that needs an owner.
- An untrusted finding sets no `next_check_after` and `due()` refuses on `state == halted`, so
  there is no retry loop. With F-1 fixed, that now holds across `update apply` as well.

### Local files and privacy

- Atomic writes through `write_json_atomically`: a pid-suffixed temporary plus one `os.replace`.
  No temporary file is left behind.
- The log is capped at 64 KB with exactly one rotation — a bounded amount of disk forever, not a
  date-stamped series.
- The status document's key set is pinned by a test; no field can hold a secret. Messages are
  stripped of control characters and capped at 200 characters, which matters because the untrusted
  branch records `str(error)` from the trust chain and that text is partly attacker-influenced.
- Transport failures record the URL and the exception **type name** only, never the exception text
  — so a proxy URL carrying credentials cannot reach the file.
- Corrupt, foreign-schema, unreadable and concurrently written status files all end at `load`
  returning `None`, which is inert and fail-closed. F-4 fixed only how that is *reported*.

### Concurrency and operational limits

- `update-check` is a **leaf lock**: nothing is acquired while it is held, and it is never taken
  while holding another. Its name contains a hyphen, which `PROFILE_NAME_PATTERN` forbids, so it
  cannot collide with a profile.
- Confirmed by reading the call chain that `install_release` releases the installation lock before
  `record_installed_version` runs, and that `prepare_installation` takes and releases the migration
  lock before dispatch — so no `installation → update-check` edge exists anywhere.
- The check never installs, stages, activates, registers or starts anything. `perform_check`
  fetches two documents and calls `installed_versions`, which only reads directories.
- A busy lock is `blocked`: no backoff consumed, no schedule moved.

### Usability

- `update status` is dispatched **before** `prepare_installation`, takes no lock and makes no
  network call, so it answers offline, during an update, and while a check holds the lock.
- The notice reads "connector X is available. Nothing was installed. Run … when it suits you." It
  never suggests that anything was activated or that a runtime restarted.

## 3. Limits left open, deliberately

1. **The bounded next-message delay is real, and no thread is warranted.** With F-3 fixed, a check
   can add at most the 8 s budget plus the trailing chunk's read timeout and connect time — on the
   order of twelve seconds — to the *next* message, at most once per interval and never more than
   once per 60 s of process time. Before F-3 it was unbounded. That is small enough that putting
   concurrency into a process that has none is still the wrong trade; the evidence for a worker
   would be a measurement, and there is none.
2. **`bounded_fetcher` is still not exercised against a real network**, exactly as
   `updater.https_fetcher` is not. F-3's tests stub the transport. What a real TLS stack does with
   a genuinely hostile origin is untested here.
3. **The MCP trigger is not exercised end to end against a real runtime.** No test starts Hermes
   and watches a check happen.
4. **On POSIX the status file is created with the process umask** (typically `0644`). The install
   root is `0700` once any profile exists (`ensure_profile_directory` → `_harden`), and the
   document holds no secrets by construction, so this was left alone. It is pre-existing and not
   specific to C3-B.
5. **A stale "available" notice can still appear once after `--disable`.** The not-due path still
   delivers a pending notice. The statement is true — a release *is* available — so it was not
   changed.
6. **`--resume` leaves `last_error_code` and `last_error_message` in place.** With
   `state=never-checked` and `consecutive_failures=0` beside them that reads as history rather than
   as a current fault, so it was left as informative.
7. **`update auto --resume --interval-seconds 60`** fails with a usage error, because the interval
   is validated before the resume branch. Harmless; not changed.
8. **`updater.version_key` strips non-digits per component**, so a pre-release suffix orders oddly
   (`0.5.0-rc1` sorts above `0.5.0`). Pre-existing C2 behaviour that the replay floor inherits; no
   published version has such a suffix, and changing the ordering was out of scope for a review.

## 4. Gates

Run on this worktree's own `.venv`, with `npm run preflight:python` reporting the interpreter and
all three local packages resolving **inside this checkout**.

| Gate | Result |
| --- | --- |
| `npm run preflight:python` | OK — interpreter and `agentnexus_api` / `agentnexus_worker` / `agentnexus_sdk` all in this worktree |
| `pytest packages/agent-sdk-python/tests/test_connector_autocheck.py` | **66 passed** (59 before this review) |
| `pytest packages/agent-sdk-python/tests` | **1229 passed, 7 skipped** |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 382 files already formatted |
| `npm run typecheck:py` (mypy) | no issues in **286 source files** |
| `npm run contracts:check` | Contract artifacts are current |
| `git diff --check` | clean |

The 7 skips are the suite's ordinary platform skips (symlinks, POSIX permissions, POSIX command
form), present before this review.

Each of the two commits was left green on its own: the first was tested with the second's changes
parked (63 passed, ruff clean) before it was committed.

**Not run, and why**: the TypeScript typecheck, Vitest and the Playwright suites — this review
touches Python only, and no contract artifact changed. `pytest apps/forum-api/tests` was not run
either; nothing here is reachable from the API. No test performed a forum write, built a client,
invoked the bridge or touched a network: the only transport shapes involved are an in-memory
fetcher and a stubbed `httpx2` module.

## 5. Assessment

C3-B is a careful slice, and most of what it claims holds: the trigger point is correctly placed
after the flush, the trust chain is reused rather than re-implemented, the transient/untrusted
split is structural rather than read out of error prose, the lock is a genuine leaf, and the status
document cannot hold a secret. The four defects share one shape — the **state machine around
`halted` had more exits than the design has**, and the **fetch budget was documented as a bound it
did not enforce**. Both are now closed with failing-first tests. Nothing else found warranted a
product change.
