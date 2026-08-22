# Heimdall

Heimdall is a self-hosted deployment manager for public GitHub repositories. It builds the `main` commit as an isolated Docker candidate and promotes it to a stable preview route only after service health and route checks pass.

> **Current status** · Alpha · Single-host validated · Public GitHub repositories

## Why Heimdall

Deploying a repository manually means coordinating source checkout, image builds, containers, health checks, and NGINX routing. When a step fails, the operator must also determine which version is serving traffic and which new resources are safe to remove.

Heimdall manages that work as one deployment flow.

```text
Register repository
  -> pin exact commit + immutable configuration snapshot
  -> build and start an isolated candidate
  -> verify service health + candidate route
  -> activate the stable preview route
  -> record state, events, and logs
```

The normal deployment path leaves the active preview unchanged until verification completes. If the runtime outcome cannot be established safely, Heimdall preserves the uncertain resources until reconciliation can establish a safe outcome.

## Core Capabilities

- Inspect the latest `main` commit of a public GitHub repository and deploy an exact SHA
- Build multi-service Docker applications with internal DNS and path-based routes
- Run per-service health checks before switching the project NGINX gateway
- Provide plain and secret environment variables and provision per-project databases and roles on external PostgreSQL
- Process durable deployment jobs through PostgreSQL claim tokens and leases
- Stream structured deployment events and bounded service logs
- Retain size-limited, redacted command and service diagnostics after failures
- Preserve uncertain runtime resources until reconciliation confirms a safe outcome

## Quick Start

Heimdall requires Docker Desktop or Docker Engine with Compose v2. Its control-plane Compose owns Control PostgreSQL, the API, the Worker, and the frontend. Application data lives in a separate PostgreSQL service reached over TCP.

```bash
git clone https://github.com/CodingPenguin-yoon/heimdall_final.git
cd heimdall_final

cp .env.example .env
mkdir -p .heimdall-local/runtime .heimdall-local/git
```

Set the following values in `.env`:

- `HEIMDALL_CONTROL_DB_PASSWORD`
- `HEIMDALL_MANAGED_DB_PROVISIONER_PASSWORD`
- `HEIMDALL_MANAGED_DB_HOST` and `HEIMDALL_MANAGED_DB_PORT`
- `HEIMDALL_RUNTIME_ROOT`: the absolute host path to `.heimdall-local/runtime`
- `HEIMDALL_GIT_WORKSPACE_ROOT`: the absolute host path to `.heimdall-local/git`

For the full database-provisioning flow, start the separate Managed PostgreSQL service first and use the same provisioner password and reachable host/port in Heimdall. To run only the deployment control plane, set `HEIMDALL_PROJECT_DB_ENABLED=false`; Compose interpolation still requires a non-empty placeholder provisioner password.

```bash
docker compose --env-file .env -f infra/dev/compose.yaml up -d --build --wait
docker compose --env-file .env -f infra/dev/compose.yaml ps
```

- UI: <http://127.0.0.1:5173>
- API health: <http://127.0.0.1:8000/api/health>
- Logs: `docker compose --env-file .env -f infra/dev/compose.yaml logs --follow`

Register a public GitHub repository in the UI, configure its services and routes, and request a deployment once the project reaches `READY`. Preview ports are currently published only on `127.0.0.1`. The external Managed PostgreSQL lifecycle is intentionally independent from the control plane.

## Architecture

```text
Browser -> NGINX / React UI -> FastAPI API -> Control PostgreSQL
                                  |               |
                                  |               +-> deployment jobs / state / history
                                  |
                                  +-> external/private TCP -> Managed PostgreSQL
                                                              project databases / roles

Project container -> external/private TCP -> Managed PostgreSQL

Python Worker -> PostgreSQL claim / lease
              -> Git exact checkout
              -> Docker candidate
              -> health + route probes
              -> project NGINX activation
```

The API and Worker use the same Python package but run as separate processes. Only the Worker receives the Docker socket; the API and frontend do not. The API requests service logs from the Worker through owner-only Unix sockets instead of accessing Docker directly.

The API provisions databases and roles through an external/private TCP endpoint. Database-enabled project containers use that same endpoint; Heimdall does not attach Managed PostgreSQL to deployment networks or manage its Docker lifecycle.

Each deployment stores its commit and service, route, and environment configuration as an immutable snapshot. Changes to project settings do not affect a deployment that has already started.

See [Architecture](project-docs/architecture.md) for component responsibilities and runtime contracts.

## Failure Handling

| Scenario                                   | Response                                                                      |
| ------------------------------------------ | ----------------------------------------------------------------------------- |
| Build, start, or health check fails        | Keep the active metadata unchanged and clean up the failed candidate          |
| NGINX config, reload, or route probe fails | Attempt last-known-good recovery and clean up only after recovery is verified |
| Worker stops during activation             | Compare database state, the NGINX marker, and Docker labels before resuming   |
| Runtime state cannot be determined safely  | Preserve it as `RECOVERY_STATE_UNCERTAIN` until reconciliation is conclusive  |

These are the intended recovery paths. Remaining crash and rollback hardening gaps are tracked in the [runtime and settings hardening plan](project-docs/plans/2026-08-17-runtime-and-settings-hardening.md).

Failure diagnostics are limited to 256 KiB per artifact and the latest 200 lines per service, with a default retention period of 30 days. If known secrets cannot be redacted safely, Heimdall records the collection failure instead of the raw output.

Relevant coverage is available in the [NGINX gateway tests](backend/tests/test_nginx_gateway.py) and [runtime integration tests](backend/tests/integration/test_worker_runtime_smoke.py).

## Current Scope

| Included                                              | Not included yet                              |
| ----------------------------------------------------- | --------------------------------------------- |
| Public HTTPS GitHub repositories                      | Private Git, SSH keys, GitLab                 |
| Fixed `main` branch and exact commit rebuilds         | Arbitrary branches, tags, or SHAs             |
| Multi-service Dockerfile builds                       | Direct Compose file execution                 |
| Path-based routes and a stable local preview port     | Public domains, TLS, multi-host routing       |
| Manual deployments                                    | Webhooks and automatic deployments            |
| Database and role provisioning on external PostgreSQL | Backup, restore, and purge automation         |
| Bounded events, logs, and diagnostics                 | Unlimited or long-term log storage and search |
| One trusted administrator                             | Multiple users and roles                      |

The canonical boundaries are documented in [Product Scope](project-docs/product-scope.md), and the operational contracts are documented in [Project Profile](project-docs/project-profile.md).

## Roadmap

The current Alpha is the self-hosted edition for a single Docker host. After its installation flow and trust boundary are ready for a first release, the planned SaaS edition will build and run user previews on Heimdall-operated infrastructure.

The SaaS edition is a future delivery model and is not part of the currently supported scope.

## Development

### Backend

Python `3.13` or `3.14` is required.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/ruff check .
```

### Frontend

Node.js `24+` and pnpm `11.13+` are required.

```bash
cd frontend
pnpm install
pnpm verify
pnpm exec playwright install chromium
pnpm e2e
```

<details>
<summary>PostgreSQL, Docker, and NGINX integration smoke tests</summary>

Start the separate Managed PostgreSQL service and the Heimdall control-plane Compose, then opt in explicitly with test-only database URLs and a public test repository.

```bash
cd backend
export HEIMDALL_TEST_CONTROL_DB_URL='postgresql://heimdall:<control-password>@127.0.0.1:55432/heimdall_control'
export HEIMDALL_TEST_MANAGED_DB_ADMIN_URL='postgresql://heimdall_provisioner:<provisioner-password>@127.0.0.1:55433/postgres'
export HEIMDALL_TEST_MANAGED_DB_RUNTIME_HOST='host.docker.internal'
export HEIMDALL_TEST_MANAGED_DB_RUNTIME_PORT='55433'
export HEIMDALL_TEST_PUBLIC_REPOSITORY_URL='https://github.com/CodingPenguin-yoon/heimdall-test'
export HEIMDALL_RUN_DOCKER_SMOKE='true'
.venv/bin/pytest tests/integration
```

</details>

## Operations Notes

- Use `docker compose ... stop` for temporary shutdowns that should preserve containers and networks.
- The control-plane Compose and Managed PostgreSQL have independent lifecycles and volumes.
- Running `down -v` against the control plane deletes Control PostgreSQL and broker volumes; running it against the Managed PostgreSQL stack deletes project application data. Use either only for an intentional reset.
- Successful project services and NGINX gateways continue running if only the API or Worker stops. A stopped Worker pauses new deployments, reconciliation, and service-log brokering. Database-enabled projects are still affected when Managed PostgreSQL is unavailable.

## Repository Layout

```text
backend/       FastAPI API, deployment Worker, runtime adapters
frontend/      React control UI
infra/         local Control Plane Compose
project-docs/  product scope, architecture, implementation plans
```

The external Managed PostgreSQL stack is intentionally not owned by this repository. Public hostname routing and TLS are documented as future design work, not current capabilities.
