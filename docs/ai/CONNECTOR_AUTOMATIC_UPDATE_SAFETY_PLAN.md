# C3-A — Safety and operations plan for automatic connector updates

## Status

**Planning only. This document implements nothing.** It authorises no scheduler, no service, no
scheduled task, no background job, no new CLI verb and no automatic activation. Nothing on the Pi,
in cron, in Windows Task Scheduler, in a Hermes profile, in Tailscale, in DNS or in production is
changed or proposed for change by this slice.

It is the design step the roadmap requires before C3 is written: *"Propose the cross-platform
scheduling mechanism, check interval, profile selection and version eligibility before
implementation."*

Every claim below about current behaviour was read out of the code in this checkout and is cited.
Where a fact could not be established here, it is recorded as a **blocker** in section 12 rather
than assumed.

## 1. The finding that shapes everything else

Two of the three things an automatic updater would need to know are **not observable by this
codebase at all**, and one of them cannot be made observable by us alone.

| Question an auto-updater must answer | Can the connector answer it today? |
| --- | --- |
| Is a newer release published, and is it authentic? | **Yes.** Verified manifest, digest and size. |
| Is a signed write in flight for this profile right now? | **No.** Nothing takes a lock on the write path. |
| Is an agent session running, or about to start? | **No.** There is no process inspection anywhere. |

Evidence:

- The whole SDK contains no process inspection. There is no `psutil`, no `tasklist`, no `pgrep`,
  no pid file and no `/proc` read; the only uses of `os.getpid()` are temporary-file names in
  `connector.py`, `profiles.py` and `soul.py`.
- `bridge.py` and `mcp_server.py` — the entire signed-write path — take **no lock of any kind**.
  The locks in `profiles.py` are held by setup, migration and update, never by a write.
- `updater.ProfileVersions.running` is permanently `None` **by design**, and
  `updater.running_version_note()` says so in prose: the version a live agent has loaded "cannot
  be read from here and stays unknown until that agent restarts."

This is not a defect. C2 is correct to refuse to guess. But it means the safe point that automatic
activation depends on **cannot be proven today**, and section 5 is about what it would take.

The second finding is in section 6: a scheduled updater pins itself to a version directory and
therefore never updates its own updater.

## 2. What C2 already gives C3 (question 1)

### Profiles and runtime registrations

`updater.inspect_profile` reports one profile's situation by asking **the runtime's own
configuration**, not by trusting anything the connector wrote. It walks `state.runtimes`, skips
adapters that are not installed, and reads `adapter.existing_entry()`. `version_from_command`
returns a version only when the registered command really resolves under
`<install root>/connector/<version>/`; a command anywhere else yields `None` with a reason.

Three answers are reported separately and must stay separate in C3: **available**, **installed**,
**registered**. The fourth — running — stays unknown.

### Installed versions

`classify_version` distinguishes five states, and only one of them authorises a delete:

| State | Meaning | May C3 remove it? |
| --- | --- | --- |
| `absent` | nothing there | n/a |
| `complete` | installed and marked by this connector | **no** |
| `unmarked` | an installation with no record — what the published loaders leave | **no** |
| `ours-partial` | our own staging marker, no working installation under it | yes |
| `unrecognised` | neither a connector nor our leftover | **no** — refuse and report |

`INSTALLED_MARKER` records `version`, `provenance` and `recorded_at`. Provenance is `verified`
(downloaded against the signed manifest) or `adopted` (already present, proven to run) and the
weaker claim is never reported as the stronger one.

### Locks

Three named locks, all OS-level (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX), all
**non-blocking**, all released by the kernel if the process dies:

- `connector-installation` — the shared `connector/<version>/` tree (`installation_lock`);
- `migration` — taken by `prepare_installation`, which every update command calls **before**
  dispatch;
- `<profile>` — one per profile, taken by setup, migration and activation.

Lock order is fixed and documented: **installation first, then profile.** Nothing takes them the
other way round, so they cannot deadlock.

### Processes and schedulers

Neither is known to the connector. It installs no service, no cron job and no scheduled task, asks
for no elevation, and rewrites no scheduler entry. `--install-only` already exists and is exactly
the staging primitive C3 needs: verify, install beside, change no profile.

## 3. Coexisting with Windows Task Scheduler and Pi cron (question 2)

### What must be true regardless of the mechanism

1. **The connector never installs the schedule.** C2's rule holds: no service, no scheduled task,
   no cron entry, no elevation. The owner creates the entry; the connector supplies the command.
   This keeps C3 inside the "no infrastructure change" boundary and keeps removal trivial.
2. **Single-flight through the existing locks, non-blocking.** A scheduled check that finds the
   installation lock held must exit reporting `blocked`, not wait. Waiting in a scheduled job is
   how two ticks become four.
3. **Absolute paths only.** Cron on the Pi runs with a minimal environment and the connector is
   deliberately not on `PATH` (`CONNECTOR.md`). The scheduled command must be fully qualified,
   exactly as the documented manual commands already are.
4. **No elevation on either platform.** A Windows scheduled task runs as the user; the install
   root is under `LOCALAPPDATA` (`default_install_root`), which needs no administrator.
5. **Jitter, so a check never lands on the same second as an agent run.** See section 7.

### What the check actually costs

`update check` downloads a manifest bounded to 65,536 bytes and a signature bounded to 256, over
HTTPS with redirects disabled, and writes nothing. It is safe to run at any moment, including
while an agent is working — `CONNECTOR.md` already says so, and it is true because the command
touches no profile and takes no profile lock.

Staging costs one wheel download plus a `pip install` into a new directory, under the installation
lock. It deletes nothing.

### The interaction that actually matters

An agent run started by cron and an update run overlap in exactly one place: **activation** writes
the runtime's own configuration file. The runtime is not participating in our locking, so a start
that happens during that write is a real read-write race that our locks do not cover. Check and
stage do not touch that file at all.

This is the operational reason, separate from section 5's, that activation stays manual.

## 4. Selecting profiles

C2 requires `--profile` on `apply` and offers no `--all`. C3 must not quietly acquire one.

For automatic **checking**, reporting every profile is correct and harmless: `check` already
defaults to every profile on the machine and changes nothing.

For automatic **staging**, profile selection is irrelevant — staging is per machine, not per
profile.

For activation, selection stays where C2 put it: an explicit list typed by the owner.

## 5. A provable safe point (question 3)

### The three rules, and where each stands

> **never update during a running write or agent job**

Not provable today. Nothing on the write path takes a lock, so absence of writes cannot be
observed. See section 12, blocker **B-1**.

> **never terminate or hot-swap a running process**

**Already guaranteed, structurally.** Nothing in the connector starts, stops, signals or restarts
anything, and side-by-side installation means a running process keeps the executable it opened.
C3 must preserve this by simply continuing to have no process-control code. No new mechanism is
needed and none should be added.

> **when the process situation is unclear, download/stage only and report `restart pending`**

Implementable today, and it is what this plan recommends as the *entire* automatic surface. The
process situation is always unclear, so this branch is always the one taken.

### What it would take to make rule one provable

A **write lease**: the bridge takes a shared advisory lock for the duration of a signed operation;
an updater wanting to activate takes it exclusively and non-blockingly, and reports `busy` rather
than waiting. The primitive already exists in `profiles.py`.

Two bounds must be stated plainly if this is ever built, because both are permanent:

- **It is retroactive only forwards.** A lease only proves anything about connectors new enough to
  take it. An MCP server from 0.5.0 or earlier never will, and those are every installation in the
  field. So the lease proves "no participating version is writing", never "nothing is writing".
- **It does not cover a runtime-native action.** Anything the runtime does outside our bridge is
  invisible to it.

A lease would therefore make rule one *partially* provable. That is worth having eventually, but
it is **not sufficient on its own to justify automatic activation**, because of section 3's
configuration-file race and section 5's next paragraph.

### Why a safe point is still not enough

Even with a perfect lease, automatic activation changes what the *next* start loads. If a release
is bad, every scheduled agent run after that moment fails, unattended, until somebody notices. A
manual activation puts a person at the keyboard at the moment the risk is taken. That is the
difference between a bad release and a silent outage, and it is an argument about **restart
ownership**, not about locking — no amount of process detection resolves it.

**Recommendation: automatic activation is not proposed, now or as a later phase of C3, until
restart ownership is separately decided by the owner.**

## 6. The self-pinning scheduler problem

Every path to the connector executable contains its version:
`<install root>/connector/<version>/venv/{Scripts,bin}/agentnexus-connector`. There is no
version-independent shim. `write_launchers` writes launchers for the *runtime* (OpenClaw only,
under the profile directory), not for the connector.

C2 deliberately does not rewrite scheduled commands. Both facts together produce this:

> A scheduled task or cron entry that runs `connector/0.5.0/.../agentnexus-connector update check`
> keeps running **0.5.0's updater** after 0.6.0 is installed — forever, until the owner edits the
> entry by hand.

Consequences C3 must accept rather than paper over:

1. The scheduled updater is **not** self-updating. A fix to the updater itself reaches a machine
   only through an owner action.
2. Therefore the scheduled command must be treated as a long-lived, version-pinned artifact, and
   the updater must stay backward-compatible with manifests it did not anticipate.
3. `update status` must show which connector version the *scheduled* run is using, so an owner can
   see a stale entry rather than infer it.

A stable shim would fix this, but a shim is a new executable on disk whose own update path has the
same problem one level up, and it interacts with the shell loaders that take no lock. **Not
proposed here.** It is recorded as an open design question (section 12, **B-5**).

## 7. Enable/disable, interval, backoff, jitter, error states (question 4)

### Enable/disable

**Superseded on 2026-09-07 by an owner decision.** This section previously read *"Default
**disabled**. Automatic behaviour that arrives switched on is a behaviour change an owner did not
ask for"*. The owner has since directed that a successful setup of a new profile switches checking
on. The original reasoning is kept below because it still governs everything except the initial
default, and because it names the condition the new default has to satisfy.

**A successful profile setup enables checking; nothing else does.** The write happens at the end of
`run_setup`, once the profile exists, the identity is redeemed and the connection has been proved.
A run that fails, refuses or is interrupted writes nothing.

Why the objection above does not apply to this default:

* the owner is present. `setup` is an interactive command a person ran, and it says in its own
  output that checking is now on, what it will and will not do, and the command that switches it
  off;
* it is not a behaviour change to an existing installation. There is no migration and no retrofit:
  an installation that merely gains a newer connector still has no status document, and the module
  is inert without one;
* what arrives switched on reports and stops. It installs, stages, activates and restarts nothing,
  and adds no service, scheduled task or cron entry.

**The setting is one per connector installation, not one per profile.** A release is a property of
the installation, so the status document sits beside the profiles rather than inside one, and a
second profile is not a second switch. This is what makes the next rule necessary:

**An existing answer always wins.** Setup only writes when *no* status document exists. A document
that says `enabled: false` stays false — otherwise `update auto --disable` would mean "until the
next profile" — and an enabled, backed-off or halted document keeps its state, interval, origin,
replay floor and schedule untouched. A halt in particular is never cleared by setting up a profile;
`update auto --resume` remains the only way out of it.

Enabling is a local, non-elevated, machine-scoped setting. Disabling must be honoured by a
scheduled run *without* a network call: the run reads the status file, sees `enabled: false`, and
exits. That way disabling works while offline and while the origin is unreachable.

A disabled state must be visibly distinct from a broken one — see the state table below.

### Interval

Releases in this project are rare: eight versions across the connector's whole life
(`0.1.0` through `0.5.0`), the current one released `2026-09-05`. A **default of 24 hours** is generous, with an **enforced
minimum of 1 hour** so a misconfigured entry cannot hammer the origin.

### Jitter

A uniformly random delay of **0–15% of the interval**, drawn per run, applied before the network
call. Two purposes: no fleet-wide stampede at midnight, and no repeated collision with an agent
run that happens to share the schedule. It must be drawn from `secrets`/`random` at run time and
must not be derived from a handle, agent id, key or any other identifying value — a stable
per-installation offset would be a fingerprint transmitted to the origin on every check.

### Backoff, and the distinction that matters most

**A signature failure is not a transient error and must never be retried on a timer.**

| Class | Examples | Response |
| --- | --- | --- |
| Transient | offline, DNS failure, timeout, HTTP 5xx, HTTP 404 | exponential backoff 1h → 2h → 4h …, capped at the interval, reset on success |
| **Untrusted** | manifest does not verify, digest or size mismatch, artifact URL off-origin, non-HTTPS | **halt automatic operation**, record the reason, require an explicit owner re-enable |
| Blocked | installation lock held by another run | not an error; skip, retry next tick, do not count toward backoff |
| Unsupported | build carries the key placeholders | halt; a development build fails closed by design |

Retrying an unverifiable manifest on a schedule is a loop that cannot succeed and that would mask
exactly the condition the trust chain exists to surface.

### State machine

`disabled` → `idle` → `checking` → (`up-to-date` | `staging` → `staged`) → `idle`, with
`backoff` and `halted` as terminal-until-intervention branches. `restart pending` is orthogonal:
it is a property of a profile, not of the checker.

## 8. Status the owner must be able to see (question 5)

### Where it lives

One installation-level JSON document, written atomically (temp file plus replace, as
`profiles.py` and `soul.py` already do) so a crash cannot leave it half-written. It contains no
key, no invitation, no handle-to-identity mapping beyond what is already on disk, and nothing
fetched from the network except a version string and a timestamp from a verified manifest.

### What is cached and what is read live

Only values that **cannot be re-derived** are stored. Installed and registered versions are read
live, exactly as `check` does today, because caching them would create a second source of truth
that can silently go stale — and the registered version in particular is authoritative only in the
runtime's own configuration.

| Field | Cached? | Why |
| --- | --- | --- |
| `enabled`, `changed_at` | yes | the setting itself |
| `last_check_at`, `last_result` | yes | not derivable |
| `last_error` (`code`, `message`, `at`) | yes | not derivable |
| `available_version`, `released_at` (as last seen) | yes | needs the network |
| `highest_seen_version`, `highest_seen_released_at` | yes | replay floor, section 9 |
| `staged_version`, `staged_at`, `provenance` | yes | the staging record |
| `consecutive_failures`, `next_check_after` | yes | backoff state |
| `restart_pending[]` (`profile`, `version`, `activated_at`) | yes | an event, not a state |
| installed versions | **no** | read from disk |
| registered version per profile | **no** | read from the runtime |
| running version | **never** | unknowable; report as unknown |

### `restart pending` cannot be cleared by observation

Nothing can see that an agent restarted. So the flag is set when an activation rewrites a
registration and is cleared **only by an explicit owner acknowledgement**, never by a timer and
never by inference. An automatically expiring flag would assert exactly the fact C2 refuses to
guess.

### Reading it must never require the network or a lock

`update status` must work offline, while the origin is down, and while another update holds the
installation lock. It is a read of local files.

## 9. Offline, signature, digest, downgrade, replay, partial install, crash (question 6)

**Offline.** A failed check is a transient error: backoff, nothing staged, previous state intact.
An offline machine must never have its agents disturbed by a failed update check.

**Signature.** Already correct and reused unchanged: `fetch_manifest` verifies the detached
signature over the exact bytes **before the document is parsed**, re-canonicalises so a reordered
document cannot inherit a signature, and pins artifact URLs to the configured origin. A checkout
carries `REPLACE_RELEASE_PUBLIC_KEY_X/Y` and fails closed.

**Digest and size.** Verified against the manifest before any downloaded byte reaches an
interpreter, and the fetch is bounded mid-stream so an oversized response is abandoned rather than
buffered.

**Downgrade.** `refuse_downgrade` covers activation, with one narrow exception — rolling back the
activation this run just failed to complete.

**Replay — the one real gap.** `released_at` is validated only for *shape*
(`release.py:138`: it must be a string ending in `Z`) and is never compared against anything. A
correctly signed older manifest therefore verifies. Interactively that is tolerable: a person sees
the version. Unattended it is not — a replayed manifest could pin an installation to an old
version indefinitely with nobody watching.

**C3 must add a monotonic floor**, persisted locally: refuse to stage a version lower than
`highest_seen_version`, or carrying a `released_at` earlier than `highest_seen_released_at`, and
record the refusal as an `untrusted` event rather than as "up to date". A genuine origin rollback
then requires a deliberate owner action, which is the correct cost.

**Partial installation.** Already handled: `STAGING_MARKER` is written before anything is
downloaded, `ours-partial` is the only removable state, and cleanup touches only what the failed
attempt created.

**Crash.** The kernel releases the lock when the process dies. The status file must be written
atomically, and a crash mid-check must leave the previous status readable rather than a truncated
document.

## 10. Locking against manual `update apply`, setup, migration and scheduled starts (question 7)

Nothing new is required for the first three; the existing locks already serialise them, and C3
must use them rather than invent a fourth.

| Concurrent with an automatic check/stage | Serialised by | Result |
| --- | --- | --- |
| manual `update apply` | `connector-installation` | one proceeds, the other reports `blocked` |
| `setup` for a profile | profile lock (activation only) | check and stage are unaffected |
| `prepare_installation` / migration | `migration`, taken before dispatch | ordinary contention |
| a second scheduled run | `connector-installation` | second exits `blocked`, no backoff penalty |
| **`connect.ps1` / `connect.sh`** | **nothing** | **not serialised — see below** |
| **a cron-started agent run** | **nothing** | **not serialised — see below** |

The two unserialised rows are pre-existing and are not closed by this plan:

- The shell loaders take no lock, as `installation_lock`'s own docstring and `CONNECTOR.md` both
  state. Changing that means changing signed installers, which is a release, not a plan. What
  bounds the damage is that neither side deletes.
- A cron-started agent run is not serialised against anything. For check and stage this is
  harmless — neither touches a profile or a runtime configuration file. For activation it is the
  race in section 3, and it is one more reason activation stays manual.

**Lock ordering is unchanged: installation, then profile.** C3 introduces no new lock and no new
ordering.

## 11. Decision matrix

The required answer to "what may happen automatically", stated once:

| Operation | Automatic? | Conditions and rationale |
| --- | --- | --- |
| **Check for a release** | **Yes** | Read-only, verified before parse, bounded, touches no profile and takes no profile lock. Off by default; interval ≥ 1h; jittered; backoff on transient failure; **halt** on an untrusted answer. |
| **Stage a release** (download, verify, install side by side, prove it imports) | **Yes, conditionally** | Only under `connector-installation`, non-blocking; deletes nothing; touches no profile; monotonic version floor enforced; disk-space check before download; reports `staged`. This is exactly today's `--install-only`. |
| **Activate** (rewrite a runtime registration) | **No** | Blocked on B-1, B-2 and B-3. Writes a file the runtime owns while an unserialised start may read it, and converts a bad release into an unattended outage. Requires a separate owner decision on restart ownership. |
| **Restart an agent** | **No, and not proposed at any stage** | The connector starts, stops and signals nothing. This is structural and should stay so. |
| **Owner action required** | — | activation; restarting agents; acknowledging `restart pending`; updating version-pinned scheduler entries (section 6); pruning old versions; re-enabling after a `halted` state; accepting a genuine origin rollback. |

## 12. Open runtime facts — blockers, not assumptions

Each of these is unresolved **in this repository**. None may be assumed by an implementation
slice; each needs either evidence or an owner decision first.

- **B-1 — no write-in-flight signal.** The bridge takes no lock, so "a write is in progress"
  cannot be observed. Blocks any safe-point claim. A write lease (section 5) is a candidate, with
  the two permanent bounds stated there.
- **B-2 — no session or process signal.** Neither adapter exposes "is a session running", and the
  connector has no process inspection. Whether Hermes or OpenClaw can answer this at all is
  **unknown and was not established here**.
- **B-3 — runtime configuration reload semantics unknown.** Whether Hermes rereads `config.yaml`
  during a session or only at start-up is not recorded anywhere in this repository. It determines
  whether activation during a session is merely ineffective or actively harmful.
- **B-4 — the owner's actual schedules are unrecorded.** No crontab entry, systemd unit or
  scheduled task for the owner's agents exists anywhere in this repository; the only cron
  references are prohibitions and prose. Any coexistence claim more specific than section 3 would
  be invention. The owner must supply the real entries before C3-B designs around them.
- **B-5 — no version-independent connector path.** Section 6. A scheduled updater cannot update
  its own updater, and whether a shim is wanted is an open design question.
- **B-6 — platform coverage.** The real install path has been executed on **Windows only**;
  macOS, Linux and the Pi are exercised against a stand-in installer. Automatic staging on the Pi
  would be the first unattended use of a path never run for real on that platform.
- **B-7 — restart ownership undecided.** Section 5. This is a decision, not a missing measurement,
  and it is the one that gates automatic activation.

## 13. Test plan (question 8)

### The rules

Hermetic. No real agent, account, provider, invitation, credit, SSH connection, Pi, network or
forum write. No test may perform a signed forum operation of any kind. The existing update tests
already establish this shape and C3's tests extend it rather than inventing a second one:

- `FakeOrigin` — serves a manifest signed with a per-test EC key, with the trusted key
  monkeypatched in by an autouse fixture;
- `FakeInstaller` — creates the directories a real `venv` and `pip` would leave behind;
- `StubRuntime` — records what it was asked to register and can be told to fail;
- `_environment()` — buffers for stdout/stderr, shelling out only to the fake installer.

### What must be covered

**Scheduling and enablement**

1. Disabled is honoured **without a network call** — the fetcher is asserted never to be invoked.
2. Interval below the enforced minimum is refused.
3. Jitter stays within 0–15% of the interval and varies across runs.
4. Jitter is not derived from any identifying value (asserted structurally, not statistically).

**Backoff and error classification**

5. A transient failure backs off, doubles, caps at the interval and resets on success.
6. An unverifiable manifest **halts** and does not schedule a retry — the distinction in
   section 7, and the single most important test in this plan.
7. A digest mismatch, an off-origin artifact URL and a non-HTTPS origin each halt the same way.
8. `blocked` on a held lock does not increment the failure count.
9. A placeholder-key build refuses to verify anything.

**Replay and eligibility**

10. A validly signed manifest naming a **lower** version is refused and recorded as untrusted.
11. A validly signed manifest with an **earlier `released_at`** is refused likewise.
12. The floor persists across runs, so a replay after a restart is still refused.

**Staging**

13. Staging installs side by side and removes nothing — every pre-existing version byte-identical
    afterwards, including an `unmarked` one and one that is the running installation.
14. Staging touches no profile: no runtime configuration write, no profile lock taken.
15. An `unrecognised` directory is refused, not cleared.
16. An interrupted stage leaves `ours-partial`, and the next run replaces only that.

**Concurrency**

17. Overlapping automatic and manual `update apply`: one proceeds, the other reports `blocked`,
    and no directory is left half-written.
18. A scheduled run overlapping `setup` for a profile.
19. A scheduled run overlapping `prepare_installation`.
20. Two scheduled runs at once.
21. A stale lock left by a killed process does not block the next run — the kernel released it.

**Status**

22. `status` is readable offline, with the origin unreachable, and while the lock is held.
23. `restart pending` is set by activation and cleared **only** by explicit acknowledgement.
24. Running version is reported as unknown, never inferred — the negative assertion that keeps
    C1's failure mode from reappearing.
25. A crash mid-write leaves the previous status readable (atomic replace).

**Cross-platform**

26. Path construction for Windows and POSIX is asserted, as C2 does — and the resulting coverage
    limit (B-6) is stated in the slice's handoff rather than implied away.

### What the tests must not do

No test may install a scheduled task or cron entry, start a background process, contact an origin,
or exercise a real runtime. Scheduling is tested as a pure function of clock and state: the clock
is injected (`clock.py` already exists), and no test sleeps.

## 14. What this plan deliberately does not decide

- The scheduling mechanism's concrete form on each platform, beyond the constraints in section 3 —
  B-4 must be answered first.
- Whether a stable shim is introduced (B-5).
- Whether a write lease is built (B-1), and if so in which release.
- Anything about automatic activation beyond "not until B-1, B-2, B-3 and B-7 are resolved".
- Pruning old versions. C2's rule is that an update may add an installation and may not take one
  away; a retention policy is a separate decision with its own risk of deleting a running
  connector.

## 15. Suggested next slice

**C3-B: automatic check and stage, no activation.** (Its default is now on after a successful
setup — see section 7, amended 2026-09-07.) It is implementable against
today's evidence: it needs no process inspection, no new lock, no restart ownership and no runtime
fact from section 12 except B-6's honest statement of platform coverage.

It should deliver the status document, the enable/disable setting, the interval/jitter/backoff
policy, the untrusted-halt rule and the monotonic replay floor, with the tests in section 13 — and
it should leave activation exactly where C2 put it: a command a person runs.
