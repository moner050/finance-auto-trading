# Trading Backoffice Design

**Status:** Approved for implementation planning  
**Date:** 2026-08-25  
**Authority:** User-approved backoffice requirements in the current task  
**Target:** Ubuntu Server, Docker Compose, public HTTPS

## 1. Purpose

Create a single-operator GUI backoffice for every supported trading control in
the finance-auto-trading service. The GUI must expose the existing governed
execution paths without bypassing readiness, lock order, promotion evidence,
incident attribution, or atomic activation and halt contracts.

The service is operated by one authorized Google identity:
`BACKOFFICE_ALLOWED_EMAIL`. It is not a general multi-user administration product.

## 2. Success Criteria

The implementation is complete when:

1. the authorized operator can inspect and control accounts, provider bindings,
   risk policies, exact strategy universes, provider evidence, reconciliation,
   Shadow/Paper promotion, LIVE activation, halt, retirement, incidents, and
   global trading controls from the GUI;
2. every trading mutation calls a typed application command service rather than
   issuing ad hoc SQL from a route or template;
3. Google OAuth admits only the exact verified authorized email;
4. provider credentials, account identifiers, the Google OAuth secret, and
   other secret material are encrypted at rest in MySQL;
5. the second password is stored only as an Argon2id verifier;
6. the database connection information and active master encryption key are the
   only secret bootstrap values required from the server `.env` during normal
   operation;
7. dangerous changes require a fresh, action-bound second-password approval;
8. HALT and EMERGENCY remain immediate safety paths and never depend on the
   second password, provider availability, secret decryption, or readiness;
9. every authentication and control attempt emits a redacted structured
   security log and, when MySQL is available, produces a durable audit record;
10. the Docker deployment exposes only Caddy's HTTP/HTTPS ports and fails
    closed on migration, authentication, secret, database, Redis, provider, or
    lease failures; and
11. all new tests and the existing safety regression suite pass.

## 3. Scope

### 3.1 Included

- Google OAuth login and exact-email authorization.
- Redis-backed browser sessions and one-use action approvals.
- CSRF protection for every mutation.
- Encrypted, versioned secret registration, activation, rotation, and
  retirement.
- Versioned second-password enrollment and rotation.
- Operational dashboard and health reporting.
- Account, provider binding, and policy-binding controls.
- Risk-policy version creation, comparison, and activation.
- Exact KOSPI 200 common-share, S&P 100 common-share, and BTCUSDT universe
  authority workflows.
- KIS, Toss, and Binance provider evidence and reconciliation controls already
  supported by the backend.
- David v6 Shadow/Paper manifest and promotion controls.
- Exact readiness, LIVE activation, HALT, and RETIRED controls.
- GLOBAL ARMED/DISARMED and kill-switch controls.
- Incident inspection, attribution, and resolution.
- Redacted audit history and system status.
- Docker Compose packaging for Caddy, backoffice, worker, MySQL, Redis, and a
  one-shot migration service.

### 3.2 Excluded

- Direct SQL consoles or generic table editors.
- Viewing an existing plaintext secret after registration.
- Reading or changing the master encryption key from the GUI.
- Manually assigning fencing tokens, scheduler leases, or runtime ownership.
- Docker socket access or container lifecycle management from the GUI.
- Additional users, teams, roles, or delegated permissions.
- A public backoffice API intended for third-party clients.
- Mobile-native applications. The web UI remains usable at narrow widths but is
  desktop-first.

## 4. Chosen Approach

Use FastAPI, Jinja, and HTMX within the existing Python project.

This approach keeps control orchestration in the current Python application
boundary, avoids a second JavaScript application and token API, and minimizes
the authentication and deployment surface. Routes return complete pages or
small HTML fragments. JavaScript is limited to HTMX and small progressive
enhancements; business rules never run in the browser.

Rejected alternatives:

- FastAPI plus a React SPA adds an API contract, separate build, CORS and token
  handling, and a larger client-side security boundary without a current need.
- Django Admin encourages direct model CRUD and would make it too easy to bypass
  the existing application safety services.

## 5. Runtime Architecture

```text
Internet
   |
   | HTTPS
   v
Caddy
   |
   v
Backoffice Web (FastAPI + Jinja + HTMX)
   |-- Google OAuth / OIDC validation
   |-- session, CSRF, and second-password gates
   |-- typed query and command facades
   |-- redacted audit recording
   |
   +-- MySQL: trading state, authority, encrypted secrets, audit
   +-- Redis: browser sessions, OAuth state/nonce, rate limits,
              one-use dangerous-action approvals

Trading Worker
   |-- existing orchestrators, provider adapters, and reconciliation
   +-- the same MySQL and Redis, with independent process lifecycle
```

The web container does not invoke the CLI as a subprocess. CLI and web entry
points share the same typed application services. Web handlers do not import
ORM mutation models for control changes; only query projections may read ORM
rows through a dedicated query layer.

## 6. Docker Deployment

Docker Compose contains:

- `caddy`: publishes ports 80 and 443, manages TLS and security headers;
- `backoffice`: serves FastAPI on the internal network only;
- `worker`: runs trading and reconciliation workloads;
- `mysql`: uses a persistent volume and has no public port;
- `redis`: uses a persistent or explicitly ephemeral operational volume and has
  no public port; and
- `migrate`: runs Alembic once and exits successfully before application
  services start.

Startup order is:

```text
MySQL healthy + Redis healthy
  -> migration succeeds at the expected head
  -> backoffice and worker start
  -> Caddy routes healthy backoffice traffic
```

If migration is absent, failed, or at an unexpected revision, backoffice serves
only an unavailable/status response and rejects all mutations. Worker start is
also refused.

The backoffice and worker images contain no `.env` file. Compose injects the
database URL and active master key at runtime through `env_file`. During a
controlled master-key rotation, old and new key versions may be injected
temporarily; normal steady state contains only the active key.

The exact authorized email is a code-level authorization policy for this
single-user service. Changing it requires a reviewed deployment, not a database
edit.

## 7. Authentication and Session Security

### 7.1 Google OAuth

Use Google OAuth Authorization Code flow with PKCE and OIDC validation. The
callback validates:

- state and nonce;
- PKCE verifier;
- HTTPS redirect URI;
- signature, issuer, and audience;
- issued-at and expiration time;
- `email_verified` is exactly `true`; and
- normalized email is exactly `BACKOFFICE_ALLOWED_EMAIL`.

Any mismatch is rejected without revealing whether the email or another claim
failed. OAuth state and nonce are one-use Redis values with short expirations.

### 7.2 Browser Sessions

Redis stores the server-side session. The browser cookie contains only a
cryptographically random session ID and has:

- `Secure`;
- `HttpOnly`;
- `SameSite=Lax`;
- a narrow path; and
- a 12-hour absolute lifetime.

Session rotation occurs after successful login and second-password changes.
Redis loss invalidates every login and dangerous-action approval. No bearer
token is stored in local storage or session storage.

### 7.3 CSRF

Every state-changing request requires a session-bound CSRF token. OAuth callback
state is separate from the application CSRF token. HTMX requests do not receive
an exception from CSRF validation.

## 8. Secret Storage

### 8.1 Secret Material

MySQL stores versioned encrypted values for:

- KIS credentials and account identifiers;
- Toss credentials and account identifiers;
- Binance API and secret keys;
- Google OAuth client configuration that is secret;
- provider-specific signing or recovery credentials introduced by an approved
  adapter contract; and
- other values explicitly classified as secret by the settings schema.

The second password is not encrypted secret material. It is stored only as an
Argon2id verifier as specified in Section 9.

### 8.2 Encryption

Use AES-256-GCM envelope records. Each secret version has a fresh nonce and AAD
containing:

```text
secret name | secret version | provider | environment | schema version
```

Persist at least:

- secret version UUID;
- logical secret name and category;
- provider and environment scope;
- ciphertext;
- nonce;
- AAD schema version;
- master-key version;
- non-reversible fingerprint;
- created and retired timestamps;
- activation state; and
- redacted actor/audit reference.

The root AES key is never stored in MySQL. It is supplied from the Ubuntu server
`.env`. A database backup therefore contains ciphertext but not the key needed
to decrypt it.

### 8.3 Resolver

All consumers use the explicit reference form:

```text
secret://db/<logical-name>@active
```

The resolver requires exact uniqueness and active scope, validates the GCM tag,
and returns a short-lived in-process secret object. It must never return the
plaintext through JSON, templates, logs, exception messages, audit details, or
debug representations.

Replacing a secret creates a new version. Existing versions are never edited in
place. Activation retires the old version and activates the new one inside one
transaction. A secret rotation invalidates provider readiness derived from the
old fingerprint until fresh evidence is captured.

### 8.4 Bootstrap

Before the first OAuth login, an Ubuntu-local `backoffice-bootstrap` command
collects Google OAuth client configuration and the initial second password using
non-echoed terminal input. It writes encrypted OAuth material and the Argon2id
verifier to MySQL.

Bootstrap succeeds only when no bootstrap authority exists. Normal rotation is
performed from the authenticated GUI. Loss of both OAuth access and the second
password is an offline operational recovery requiring database backup and
master-key custody; the first implementation does not add a remote recovery
bypass.

## 9. Second Password and Dangerous Actions

Store the second password in a versioned table using an Argon2id verifier and
per-version salt/parameters. No plaintext or reversible value is stored.

Dangerous actions require the operator to enter the second password again. A
successful check creates a Redis approval bound to:

- authenticated session and authorized email;
- action name;
- exact target identity;
- expected state and readiness digest when applicable;
- CSRF context; and
- a 60-second expiration.

The approval is consumed exactly once. It cannot authorize a different action,
target, digest, session, or request. Password failures are rate-limited by both
session identity and source IP and are always audited without storing password
material.

Second-password changes invalidate all sessions and outstanding approvals.

The following require the second password:

- binding activation or replacement;
- account enablement;
- risk-policy activation;
- universe activation;
- GLOBAL ARMED;
- lowering kill-switch severity;
- incident resolution;
- secret activation, rotation, or retirement;
- Paper-to-LIVE activation; and
- RETIRED transitions.

HALT, DISARM, raising kill-switch severity, and EMERGENCY do not require the
second password. They still require an authenticated session and CSRF token and
must remain available when provider secrets, readiness, or Redis approval state
is stale. If Redis is completely unavailable, the GUI cannot authenticate; the
existing local safety CLI remains the independent emergency path.

## 10. Command and Query Boundaries

### 10.1 Commands

Every mutation is represented by a typed command with:

- command UUIDv7/idempotency key;
- authenticated operator identity;
- source IP and request correlation ID;
- exact target identity;
- expected row version or authority digest;
- optional one-use second-password approval ID; and
- requested timestamp in UTC.

The application command handler re-reads and locks authoritative rows, validates
the approval, executes the existing domain/application service, and writes the
audit outcome. Routes never decide readiness or transition legality.

Commands for currently exposed service functions wrap, rather than duplicate:

- v6 readiness, session manifest, activation, halt, and retirement;
- provider binding activation;
- KRX and v6 universe authority activation;
- Toss zero-exposure capture and reconciliation;
- KIS and Binance readiness/evidence services; and
- global execution safety controls.

Missing command boundaries for policy versioning, secret rotation, incident
resolution, and global control are added as small application services with the
same lock and audit conventions.

### 10.2 Queries

Read pages consume purpose-built projections. Query code may join ORM tables but
must return redacted DTOs. It never returns ciphertext, nonce, raw account
identifier, API key, access token, refresh token, or second-password verifier.

Readiness is calculated on demand using the existing service. Dashboard caches
may shorten repeated rendering work but never authorize a command.

## 11. Pages and Controls

### 11.1 Operations Dashboard

- GLOBAL ARMED and kill-switch state.
- Worker runtime, scheduler lease, and fencing status.
- KIS, Toss, and Binance connectivity/evidence freshness.
- Per-binding readiness and blocker summaries.
- Shadow/Paper progress.
- LIVE ownership, positions, orders, and open incident summaries.

### 11.2 Accounts and Bindings

- Accounts, environments, enabled state, and provider scope.
- Provider binding versions and activation history.
- Risk-policy binding.
- Secret availability and fingerprint only.
- Typed create, replace, activate, disable, and bind operations.

### 11.3 Secrets

- Register, stage, activate, rotate, and retire secrets.
- Show fingerprint, category, scope, version, creation time, and status.
- Never redisplay plaintext after the registration POST completes.
- Require the second password for activation, rotation, and retirement.

### 11.4 Risk Policies

- Create an immutable policy version.
- Compare versions and validate all invariants.
- Display the approved absolute and percentage limits, including KRW 1,000,000,
  USD 2,000, Binance margin 2,000 USDT, risk per trade 1%, and maximum leverage
  7 where applicable to the currently approved v6 rules.
- Activate or replace an account policy binding.

The GUI does not silently reinterpret units or broaden a market scope.

### 11.5 Universe Authority

- Show exact KOSPI 200 common-share, S&P 100 common-share, and BTCUSDT
  authorities and history.
- Upload an authoritative manifest and validate its provenance/digest.
- Compare staged and active snapshots.
- Activate a complete exact snapshot.

There is no generic row editor that can add a single symbol and silently widen
the strategy universe.

### 11.6 Provider Evidence and Reconciliation

- Run supported one-time provider fact captures.
- Inspect recurring reconciliation state and latest outcomes.
- Display rate-limit and freshness summaries.
- Inspect redacted mismatches and related orders/positions.
- Capture and inspect Binance permission evidence without displaying secrets or
  raw provider payloads.

### 11.7 Shadow and Paper Promotion

- Display the binding-level promotion timeline.
- Claim Shadow or Paper sessions for exact exchange dates.
- Inspect manifest completeness, durable evidence links, and blockers.
- Complete only a fully verified manifest.
- Show the two distinct Shadow and two distinct Paper session requirements.

### 11.8 LIVE Control

- Recalculate and display exact readiness and blocker codes.
- Navigate from a blocker to its redacted authority/evidence detail.
- Display the current readiness digest, active universe, and expected ownership.
- Activate LIVE through the existing atomic activation service.
- HALT immediately.
- Retire an exact binding.

The displayed digest is informational. Activation locks the same authority rows
and recomputes readiness before any ownership or account state is changed.

### 11.9 Safety Controls and Incidents

- GLOBAL ARMED/DISARMED.
- Kill-switch NONE, BLOCK_NEW_EXPOSURE, and EMERGENCY.
- Open/resolved incident list, exact attribution, evidence, and resolution.
- Immediate safety action always visually separated from exposure-enabling
  actions.

### 11.10 Audit and System Status

- Actor, source IP, action, target, expected/actual state, redacted digest,
  result, failure code, and UTC time.
- Git commit, migration revision, service versions, and DB/Redis/worker health.
- Required secret configuration status without plaintext or ciphertext.

## 12. User Interface Rules

- Korean is the primary operator language; stable backend reason codes remain
  visible alongside explanations.
- The layout is desktop-first with a persistent navigation rail and responsive
  narrow-screen fallback.
- Read-only summaries and mutation controls are visually distinct.
- Exposure-enabling actions use an explicit confirmation panel containing the
  exact account alias, provider, environment, policy version, universe, and
  readiness digest.
- Safety-reducing actions are never the default button.
- HALT and EMERGENCY remain prominent and require only one confirmation click
  after authentication; they do not share the dangerous-action dialog.
- HTMX replaces the smallest relevant page region and preserves filters and
  navigation state on failures.
- Every successful mutation renders the committed result retrieved by command
  ID, not a speculative client-side state.

## 13. Audit Contract

Authentication attempts and every command emit a redacted structured security
log. They also produce a durable MySQL audit record containing:

- audit UUIDv7;
- authorized email or redacted attempted identity;
- source IP and request correlation ID;
- action and target scope;
- command/idempotency key;
- prior and resulting state or digest where safe;
- success or stable failure code;
- second-password verification success/failure when required; and
- UTC occurrence time.

Audit details never contain plaintext secrets, ciphertext, nonces, OAuth tokens,
passwords, password verifiers, authorization headers, raw provider payloads, or
full account identifiers.

For a trading mutation, the committed state and success audit are one MySQL
transaction. If success audit persistence fails, the mutation rolls back. A
failed pre-transaction attempt is recorded in a separate best-effort durable
security-audit path in addition to the structured log; failure to persist it
still does not permit the action.

## 14. Idempotency and Concurrency

Each mutation receives a UUIDv7 command ID generated by the server and bound to
the session form. Repeated submission of the same command ID returns the stored
outcome. Reusing a command ID with different action or payload is rejected.

The web layer does not weaken existing row locks. Activation, policy binding,
universe activation, secret rotation, incident resolution, and global controls
use expected versions/digests and stable lock ordering. A stale page therefore
returns a conflict and refreshes the authoritative fragment instead of
overwriting newer state.

## 15. Failure Behavior

- Database unavailable: reject login completion and all data/control pages.
- Redis unavailable: reject login and second-password approvals; never fall
  back to unsigned client sessions.
- Master key absent or wrong: reject secret consumers and exposure-enabling
  operations; create a blocking incident when a valid authenticated operational
  path detects a GCM authentication failure.
- OAuth configuration unavailable: fail closed before redirect or callback.
- Provider unavailable: preserve committed state and block new evidence or
  activation; never infer success.
- Migration mismatch: expose health-only unavailable response and block worker.
- Worker lease expired: block new exposure and activation.
- HTMX timeout: show unknown request state and resolve by durable command ID.
- Audit success record failure: roll back the mutation.
- Redis data loss: invalidate sessions and action approvals.
- Secret rotation: invalidate readiness components tied to the old fingerprint.

Exceptions returned to the browser use stable redacted codes. Detailed internal
errors are scrubbed before structured logging.

## 16. Verification Strategy

### 16.1 Unit Tests

- AES-GCM round trips and known invariants.
- Ciphertext, nonce, AAD, and key-version tamper rejection.
- Resolver exactness and redacted representations.
- Argon2id enrollment, verification, rotation, and non-storage of plaintext.
- Dangerous-action binding and one-use approval behavior.
- Command validation, authorization, and stable failure codes.

### 16.2 Authentication and Web Tests

- Exact authorized verified email succeeds.
- Other email, unverified email, issuer/audience mismatch, expired token, and
  state/nonce replay fail.
- CSRF rejection includes HTMX requests.
- Session fixation and expiration behavior.
- Second-password throttling and approval replay rejection.
- Every page and fragment returns redacted content.
- No secret appears in HTML, JSON, headers, browser storage, logs, or errors.
- Keyboard access, form labels, focus behavior, and narrow-screen fallback.

### 16.3 MySQL and Redis Integration Tests

- Secret registration, activation, rotation, retirement, and resolver use.
- Binding, policy, universe, incident, and global-control command atomicity.
- Success audit atomicity and rollback on audit failure.
- Durable idempotency under duplicate and concurrent requests.
- Existing LIVE activation locks and recomputes readiness.
- HALT, DISARM, kill-switch escalation, and EMERGENCY operate independently of
  stale readiness and provider secret availability.
- Redis session loss and one-use action approvals.

### 16.4 Docker End-to-End Tests

- Test OIDC provider drives login through Caddy to the backoffice.
- Complete configuration, Shadow/Paper, readiness, and control workflows use
  real MySQL and Redis containers.
- Only Caddy publishes public ports.
- Migration failure, Redis restart, provider timeout, and worker lease expiry
  fail closed.
- A separate private Ubuntu staging smoke test verifies real Google OAuth and
  its registered HTTPS callback.

### 16.5 Regression Gate

Ruff formatting and linting, strict Pyright, the complete existing pytest suite,
focused authorized-MySQL safety tests, secret-pattern scans, container health,
and browser verification must all pass for the exact release commit.

## 17. Operational Safety Boundary

The GUI is an operator interface to existing authority, not a new authority
source. It cannot convert missing evidence into readiness, manually edit a
promotion state, invent provider permissions, widen an exact universe, override
the global lock order, or infer that an unknown provider operation succeeded.

LIVE remains closed until the same database-backed evidence and atomic
activation service required by the CLI report READY under lock. The presence of
the backoffice does not itself authorize Shadow, Paper, or LIVE trading.
