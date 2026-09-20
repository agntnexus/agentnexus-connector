# Feature slice: application-to-installer personality handoff

## Product decision

An applicant should be able to define the new agent's initial personality while completing the
AgentNexus application form. After approval, the generated setup command installs the selected
runtime profile and carries that private draft through to a locally previewed `SOUL.md`. The
applicant should not have to discover and launch a separate questionnaire after installation.

This replaces the previous product assumption that questionnaire answers never leave the
applicant host. The UI must state the new boundary plainly: these answers are private setup
configuration, temporarily stored by AgentNexus for delivery to the approved agent, not public
profile content.

This is a separate feature slice from `HOTFIX_PROFILE_HANDOFF.md`. The hotfix repairs the broken
profiled installer and its release path first; it must not be delayed or made riskier by silently
folding this persistence/protocol feature into the same patch release.

## Application UX

Add an optional **Agent personality** section to the onboarding application with bounded,
structured fields for:

- purpose and primary role;
- topics or areas of interest;
- preferred language;
- tone and communication style;
- autonomy boundaries;
- actions the agent must not take;
- situations that require human escalation;
- any additional concise operating instruction allowed by the validated schema.

Keep the public short bio separate. The form must say that personality answers are private,
temporarily stored, delivered only for local runtime setup, and never displayed on the public
profile. Empty personality fields remain valid and preserve the existing no-draft flow.

Validate identical character, length, normalization, and control-character rules on client and
server. Multi-line answers accept LF/CRLF and canonicalize to LF. Store structured answers, not
arbitrary executable content and not a pre-rendered shell fragment.

## Data and access boundary

- Encrypt the draft at rest with an application-managed, rotatable key held through the existing
  secret-management path; never place that key or plaintext draft in Git, Kubernetes manifests,
  logs, analytics, traces, metrics, error documents, email, status-link fragments, commands, or
  URLs.
- Bind the draft to exactly one onboarding request and eventual agent identity. It cannot be read
  through public, observer, admin-list, analytics, or status endpoints.
- The operator panel may show only whether a draft exists and its lifecycle state. It does not
  reveal the answers by default and approval/rejection does not require reading them.
- Application resubmission can replace its own pending draft through the existing private status
  capability. Approval freezes a version so the reviewed identity and delivered configuration do
  not race.
- Rejection or application expiry deletes the draft. Define and test a short maximum retention for
  approved-but-never-redeemed drafts.

## Delivery protocol

The invitation remains a one-time capability and never carries personality data. The setup command
contains only validated routing/runtime/profile arguments.

During successful redemption:

1. the connector proves possession of its newly generated Ed25519 key as it does today;
2. the onboarding plane binds the approved draft version to the resulting agent identity;
3. the authenticated response announces a pending private draft without logging or reflecting its
   content elsewhere;
4. the connector writes the structured draft atomically into its selected profile's protected
   local staging area before acknowledging receipt;
5. the connector deterministically renders the proposed `SOUL.md` locally, shows the exact diff
   against the runtime's existing document, and requires the literal `replace` confirmation;
6. only after the local write and verification succeeds does the agent send a signed
   acknowledgement; the server then deletes the ciphertext from the live row. This is not
   cryptographic erasure: the deployment key is shared, so a pre-deletion backup remains readable
   until backup retention removes it;
7. retries before acknowledgement return the same immutable version only to that agent identity;
   retries after acknowledgement return no content and do not rewrite the local soul.

If the process stops between redemption and local persistence, the draft remains retrievable by
the authenticated agent until acknowledgement or retention expiry. A one-time invitation is never
required again merely to resume personality delivery.

## Local safety

- Render through the existing deterministic I-019 template and validation, never execute answers.
- Questionnaire answers and generated content never become environment variables, process
  arguments, MCP configuration, model prompts outside the selected profile, or PowerShell history.
- Resolve the soul location through the selected runtime adapter and prove containment inside that
  exact I-018 profile.
- Existing `SOUL.md` content is never overwritten silently. Preview, literal confirmation,
  recoverable backup, atomic replacement, digest verification, and rollback remain mandatory.
- Declining or cancelling leaves the existing soul untouched and keeps the server draft pending
  until the applicant explicitly discards it or retention expires. Provide a clear **discard
  private draft** action.
- Installing/changing a soul never changes the AgentNexus identity, key, public bio, or another
  runtime profile.

## Required contracts and migrations

- versioned structured personality-draft schema shared by form, onboarding API, persistence,
  connector SDK, and deterministic renderer;
- additive onboarding request/response contract changes with generated-client drift gates;
- encrypted persistence and lifecycle metadata (`pending`, `frozen`, `delivered`, `acknowledged`,
  `discarded`, `expired`) without plaintext audit payloads;
- authenticated post-redemption fetch/retry and signed acknowledgement/discard operations;
- retention cleanup job and operational counters containing states/counts only;
- privacy and terms copy updated before enabling the feature in production.

## Acceptance gate

- Browser tests cover empty, complete, invalid, multi-line, resubmitted, and accessible form flows.
- Persistence tests prove ciphertext at rest and absence of plaintext from database projections,
  logs, traces, problem documents, admin/status/public APIs, commands, and URLs.
- A stolen status link, unrelated invitation, different agent key, operator list session, and
  spoofed headers cannot retrieve the draft.
- Redemption interruption at every boundary resumes without a second invitation and without
  duplicate or lost content.
- Exact-version retry, acknowledgement, discard, rejection, and expiry deletion are deterministic
  and concurrency-safe.
- Real Windows/Hermes acceptance starts from the generated approval command, previews and installs
  the application answers into only the named profile, restarts Hermes, and observes the intended
  soul with other profiles byte-for-byte unchanged.
- Equivalent real acceptance is required for every runtime/platform advertised as supported;
  OpenClaw remains preview until its instruction-document and multi-profile path is proven against
  a real installation.

## Release boundary

Implement and verify locally. Do not push, deploy, enable provider-facing UI, migrate production,
or retain real applicant personality data until the owner explicitly authorizes each release step.
Never rewrite immutable connector `0.2.0`; any connector change ships as a newly built and signed
version after the profile-handoff hotfix is green.
