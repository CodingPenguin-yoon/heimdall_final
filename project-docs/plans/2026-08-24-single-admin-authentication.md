# Single Admin Authentication Plan

- Status: `APPROVED`
- Date: `2026-08-24`
- Approval basis: the user approved the authentication direction in advance and explicitly asked
  implementation to start after recording this Plan and its acceptance criteria.

## Decision summary

Heimdall will have exactly one fixed administrator identity, `admin`, without a user or session
database. The API will verify an operator-initialized Argon2id password hash and issue a Starlette
signed, host-only session cookie. The Backend is the authorization boundary for every management
API, including SSE handshakes. The Frontend route guard is only a UX boundary.

```text
HTTPS management hostname
Browser -> existing operator-managed Edge TLS -> Control frontend -> FastAPI
                                                     |                |
                                                     |                +-- public health/auth entry points
                                                     |                `-- authenticated management API
                                                     `-- public /login and guarded management UI

Public project hostname
Browser -> existing Edge route -> Project Gateway -> active generation
          (no Heimdall admin authentication)
```

The Edge TLS listener and certificates remain operator-managed. This change neither installs nor
modifies certificates and does not add a load balancer, tunnel, Worker, container, database table,
or migration.

## Current behavior and problem

The management UI currently renders every route without checking a session. All project,
deployment, runtime, database, public-route, diagnostics, log, and SSE APIs are callable without
authentication. The AppShell displays a static administrator label, and the shared API client has no
cookie, CSRF, or global unauthorized-session handling.

The Control Compose already keeps the API free of the Docker socket, while the two Workers own their
existing Docker effects. Authentication must preserve those boundaries and must not expose the
password hash or signing key through a Compose environment, Docker inspect, the Frontend image, a
Worker, the Edge, an application container, the Control database, or logs.

## Goals

- Provide login, session inspection, and logout for the single `admin` identity.
- Protect the complete management UI and every existing management API by default.
- Require a session-bound CSRF token on authenticated `POST`, `PUT`, `PATCH`, and `DELETE` requests.
- Keep `/api/health`, login, and the session-check entry point reachable without a prior session.
- Keep public project hostname traffic and loopback Preview behavior unchanged.
- Initialize and deliver authentication secrets as owner-only files outside the repository.
- Reject direct lexical overlap between the authentication root and every Worker- or Edge-visible
  host root in either direction, with canonical non-symlink host paths as an operator invariant.
- Distinguish an unauthenticated browser from a management API outage in the UI.

## Scope

- `POST /api/auth/login`, `GET /api/auth/session`, and `POST /api/auth/logout`
- Argon2id verification for the fixed `admin` username
- Starlette signed cookie session with an eight-hour absolute lifetime
- session-bound CSRF validation and a shared Frontend API client
- Backend default-deny protection for Project, Deployment, Runtime, Database, Public Route, logs,
  diagnostics, reconciliation, and SSE endpoints
- public `/login`, authenticated deep-link return, session-aware AppShell, and desktop/mobile logout
- an interactive `getpass` administrator initialization command
- API-only, read-only authentication secret mount in Control Compose
- focused Backend, Frontend, and Chromium tests plus documentation synchronization

## Out of scope

- signup, multiple users, user management, RBAC, ownership, password recovery, and password UI
- user/session tables, migrations, server-side session storage, and distributed session revocation
- authentication for Public Hostname or loopback Preview traffic
- TLS certificate issuance, installation, renewal, or existing Edge TLS configuration changes
- OCI Load Balancer, SSH tunnel, Edge NGINX topology changes, or a new Worker/container
- changes to the separate `heimdall-managed-db` directory

## Security and data contract

### Secret initialization and delivery

The operator runs `heimdall-admin-init` with a new canonical absolute authentication path outside the
repository. The command prompts for the password twice with `getpass`, generates an Argon2id hash and
a random session signing key, creates the directory as `0700`, and creates both files as `0600`. It
rejects symlink path components, a target below any `.git` worktree marker, malformed paths, and any
attempt to overwrite an existing target. The raw password is never accepted through an environment
variable or CLI argument and is never logged or persisted.

Control Compose bind-mounts that directory read-only at `/run/secrets/heimdall/auth` in the API
container only. It sets the container path in `HEIMDALL_AUTH_SECRET_ROOT` and derives API-only,
non-secret `HEIMDALL_AUTH_SECRET_SOURCE_ROOT` metadata from the same host source; `.env.example`
continues to declare only the single operator-provided host root. API startup rejects direct lexical
equality or containment between the source root and `HEIMDALL_RUNTIME_ROOT`,
`HEIMDALL_GIT_WORKSPACE_ROOT`, or `HEIMDALL_EDGE_CONFIG_ROOT`. The initializer rejects path-component
symlinks, and the operator must keep that exact path unchanged because Docker dereferences host
bind-source symlinks before the API can inspect them. Neither Worker, the Frontend, Edge, Control
PostgreSQL, nor project containers receive either auth key or the authentication mount. Compose may
contain the non-secret host path, but never the password hash value or signing key value in an
environment field.

API startup validates the directory and both files before serving. The directory must be exactly
`0700`, both files exactly `0600`, both must be regular non-symlink files, the password hash must be
valid Argon2id, and the signing key must meet the configured minimum entropy representation. Invalid
or missing authentication configuration fails API startup.

### Login and session

The username is the fixed literal `admin`. Username and password inputs are length-bounded. The API
always performs password verification and returns the same stable `401` problem for an unknown
username or wrong password. It does not log credentials or echo them in errors.

On success, the session contains only:

- administrator identity;
- absolute expiry;
- a random CSRF token; and
- a credential revision derived from the active password hash.

The session cookie is signed by Starlette, `Secure`, `HttpOnly`, host-only, `SameSite=Strict`, and
expires after eight hours. It contains no password, password hash, or signing key. The Backend
rejects a missing, malformed, tampered, expired, or credential-revision-mismatched session. Replacing
the signing key or password hash and restarting the API invalidates previous sessions.

Successful and failed authentication responses use `Cache-Control: no-store`. Logout requires the
current CSRF token, clears the session cookie, and returns a JSON `200` response because the shared
Frontend client parses successful responses as JSON.

### CSRF and authorization

The API composition root separates public and protected routers. `/api/health`, login, and the
session-check entry point are public entry points; session inspection still returns `401` when no
valid session exists. Every feature router is included under a protected router with common
authentication and CSRF dependencies. Dependencies run before feature service resolution, so an
unauthorized or CSRF-invalid request cannot call a feature service. Authenticated `GET` and SSE
handshakes do not require CSRF; authenticated unsafe methods require an exact
`X-CSRF-Token` match.

CORS is not enabled. Same-origin requests and the existing management hostname remain the only
browser contract.

## Frontend contract

The Frontend checks `/api/auth/session` before rendering the protected route tree. During that check,
no management page or management query is mounted. A `401` becomes an unauthenticated state and
redirects to `/login`; network or `5xx` failure renders a distinct retryable service-unavailable
state.

After login, the browser returns only to a validated internal path that it originally requested.
The session and CSRF token stay in React/module memory and are never written to localStorage or
sessionStorage. The signed cookie remains browser-managed.

The shared API client uses same-origin credentials, merges caller headers through
`new Headers(init.headers)`, and attaches the in-memory CSRF token to unsafe methods. A protected API
`401` clears the session state, CSRF token, and TanStack Query cache; the route guard then moves the
browser to login. AppShell displays the authenticated username and exposes logout at desktop and
mobile widths. Same-origin `EventSource` connections continue to use the browser session cookie.
Because native EventSource hides the handshake status, either stream's connection error triggers one
deduplicated session lookup: a `401` takes the same cleanup path, while network and `503` failures
preserve the current session. Absolute-expiry handling also removes a page containing an active
stream when its session expires.

## Failure impact and recovery

| Failure                                     | Result                                                          | Recovery                                                                        |
| ------------------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| Missing or unsafe auth secret files         | API fails startup; existing public project routes continue      | Fix owner-only files and restart only the API                                   |
| Auth source directly overlaps a shared root | API fails startup before serving                                | Select a disjoint canonical auth directory and recreate the API                 |
| Wrong login input                           | Generic `401`; no session                                       | Retry with the operator-held password                                           |
| Tampered or expired cookie                  | Management request returns `401`; feature service is not called | Login again                                                                     |
| Missing or wrong CSRF token                 | Unsafe request returns `403`; no mutation starts                | Refresh session/login and retry through the UI                                  |
| API or Control DB outage                    | Login/session check shows management service unavailable        | Restore Control Plane; project hostname and Preview data paths remain unchanged |
| Password hash or signing key replacement    | Existing sessions become invalid after API restart              | Login with the new credential                                                   |

No authentication failure mutates project, deployment, runtime, database, routing, Docker, NGINX,
or public-hostname state.

## Selected direction and tradeoffs

- File-backed credentials avoid a database and migration, matching the single-operator scope. They
  require an API restart for rotation and deliberately provide no per-session revocation list.
- Starlette signed cookies keep session state out of the database. The cookie contents are signed,
  not encrypted, so only the four non-secret session fields are stored.
- A protected router at the API composition root provides default deny and covers future feature
  endpoints added to that router. Public endpoints stay explicit and small.
- Session-bound CSRF plus a host-only `SameSite=Strict` cookie protects unsafe same-origin requests
  without opening CORS or adding a token store.
- Frontend session state improves navigation but is not trusted for authorization; direct API calls
  are rejected by the Backend.

## Vertical implementation and verification

### 1. Owner-only credentials and authentication service

- Implement the `getpass` initializer, Argon2id verification, strict file validation, credential
  revision, session creation/validation, and auth schemas.
- Verify no-overwrite behavior, modes, malformed configuration, generic login failure, log/response
  secrecy, cookie flags, expiry, tamper rejection, and credential/signing-key rotation.

Safe stopping point: no existing management endpoint is protected until the common API boundary is
installed.

### 2. Backend default deny and CSRF

- Add the three auth endpoints and install Starlette session middleware from validated secrets.
- Put all existing feature routers behind the shared authentication/CSRF dependencies while leaving
  health and required auth entry points public.
- Verify session inspection, logout, exact/missing/wrong CSRF, CSRF-free GET, all management route
  families, SSE handshakes, health, and that rejected requests never call a feature service.

Safe stopping point: direct management APIs are secure even before the Frontend flow is enabled.

### 3. Frontend login and guarded management shell

- Add the in-memory auth provider, session gate, login page, safe deep-link return, session-aware
  AppShell, shared-client credentials/CSRF/401 handling, and mobile logout.
- Update existing E2E mocks with an authenticated session and add focused login/logout coverage.
- Verify unauthenticated deep links, good/bad login, API outage distinction, automatic CSRF, logout,
  expiry redirect, and mobile logout.

Safe stopping point: the UI and API use the same session contract; direct Backend enforcement
remains authoritative.

### 4. API-only secret mount and documentation

- Add only the non-secret host auth-directory setting to `.env.example` and mount it read-only into
  the API service. Derive `HEIMDALL_AUTH_SECRET_SOURCE_ROOT` inside Compose for API-only overlap
  validation; do not add a second operator setting.
- Render Control, Edge, and Managed DB Compose configurations and inspect environment, mounts,
  sockets, public ports, and networks. Confirm no signing key value is rendered or inspectable and no
  authentication mount reaches a Worker, Frontend, Edge, database, or application container.
- Update README and current architecture, profile, and scope documents in English with implemented
  facts only.

Safe stopping point: removing the API auth mount or reverting the application image prevents the
management API from starting but does not remove existing Edge routes or project runtimes.

### 5. Aggregate gates and smoke

- Backend: `ruff format --check`, `ruff check`, and complete `pytest`.
- Frontend: `pnpm verify` and Chromium login/logout E2E.
- Infrastructure: Control, Edge, and Managed DB Compose config; mount/environment/socket/port/network
  review; `git diff --check`.
- On an operator-configured HTTPS management hostname, verify redirect, bad login, good login,
  protected page access, an authenticated mutation, logout, post-logout `401`, and cookie flags.
  Also verify Public Hostname and loopback Preview remain reachable.

The real HTTPS smoke requires the operator-managed DNS/TLS endpoint and initialized production
credential. If that external state is unavailable, it must be reported as not run rather than
simulated or claimed successful.

## Acceptance criteria

- Only `admin` with the initialized password can create a valid management session.
- Wrong username and password are indistinguishable through the API response.
- The cookie is signed, `Secure`, `HttpOnly`, host-only, `SameSite=Strict`, and bounded to eight hours.
- Tampered, expired, old-signing-key, and old-credential sessions are rejected.
- `/api/health` stays public; every management API family and SSE handshake defaults to authenticated.
- Unsafe protected methods require the exact session CSRF token; authenticated GET remains allowed.
- Authentication or CSRF failure occurs before any feature service call.
- The UI renders no management data before session confirmation and distinguishes `401` from outage.
- Login restores a safe internal deep link; logout and expiry return to login on desktop and mobile.
- The CSRF token is attached centrally. The signed cookie is browser-managed, while password,
  returned session payload, and CSRF token are absent from `localStorage` and `sessionStorage`.
- Only the API receives both auth path keys and the read-only auth secret mount; no signing key value
  appears in Compose environment or Docker inspect.
- With canonical non-symlink host paths, API startup rejects an auth source that directly equals,
  contains, or is contained by the runtime, Git workspace, or Edge config root.
- Public Hostname and loopback Preview access remain unauthenticated and structurally unchanged.
- Backend, Frontend, Compose, diff, and available smoke gates have recorded evidence.

## Documentation impact

After implementation and verification, synchronize implemented facts in:

- `README.md`
- `project-docs/architecture.md`
- `project-docs/project-profile.md`
- `project-docs/product-scope.md`
- this Plan's verification record

No existing Public Hostname Routing contract or historical verification record will be removed.

## Remaining decisions and residual risks

There are no implementation-blocking product decisions. The actual management hostname,
operator-held password, secret host path, and Edge TLS/certificate lifecycle remain deployment-time
operator inputs outside this repository change. Docker resolves bind-source symlinks on the host
before the API sees its mount, so retaining the exact canonical non-symlink path created by
`heimdall-admin-init` remains an operator-enforced invariant rather than a container-enforced one.
The fixed login endpoint has no application-level rate limit or backoff in this scope, so the
operator-managed front Edge must provide appropriate request limiting to bound Argon2 brute-force
and resource-exhaustion attempts. A successful login in a second browser tab replaces the shared
cookie and CSRF value; an already-open tab can receive `403` on its next mutation until it reloads or
signs in again.

## Verification record

- `2026-08-24`: confirmed the approved direction, existing uncommitted Public Hostname Routing work,
  current Backend/Frontend composition boundaries, separate Edge lifecycle, and API-without-Docker-
  socket invariant before implementation.
- `2026-08-24`: Backend Ruff format and lint passed. The full suite reported `250 passed` and `18
skipped`; the first sandboxed run could not bind the existing Unix broker sockets, and the identical
  suite passed outside that sandbox restriction.
- `2026-08-24`: `pnpm verify` passed formatting, lint, `15` Vitest files with `50` tests, TypeScript,
  and the production Vite build. Chromium Playwright passed all `11` E2E tests, including bad/good
  login, deep-link return, outage distinction, CSRF logout, protected `401`, absolute expiry, and
  mobile logout.
- `2026-08-24`: Control, Edge, and Managed DB Compose `config --quiet` passed with non-secret,
  disjoint verification-path overrides. A targeted rendered-Control assertion confirmed only the API
  has the two auth path keys and read-only auth bind, only the two Workers have Docker socket access,
  and API/frontend/database published ports remain loopback-bound. Targeted Edge and Managed DB
  assertions confirmed neither receives auth configuration. No hash, signing-key filename, or
  Argon2 value appeared in the rendered structure.
- `2026-08-24`: the checked-in private Control `.env` was deliberately not edited and still lacks the
  existing Public Hostname Routing deployment-domain setting (and the new auth path), so its plain
  `config --quiet` does not yet pass. Live Docker inspect was not available because no Control
  containers were running.
- `2026-08-24`: the operator-managed HTTPS hostname, DNS/TLS endpoint, and production credential were
  unavailable, so the real HTTPS login/mutation/logout/cookie and Public Hostname/Preview smoke was
  not run. The checked-in Edge listener and certificate lifecycle were not changed.
- `2026-08-24`: documentation Prettier and `git diff --check` passed, the installed
  `heimdall-admin-init --help` entry point passed, and a Frontend source/E2E scan found no
  `localStorage`, `sessionStorage`, or `document.cookie` access.
