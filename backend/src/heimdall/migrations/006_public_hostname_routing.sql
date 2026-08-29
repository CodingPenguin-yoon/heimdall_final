CREATE TABLE project_public_routes (
    project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    subdomain varchar(63) NOT NULL,
    hostname varchar(253) NOT NULL UNIQUE,
    desired_state varchar(16) NOT NULL CHECK (desired_state IN ('ENABLED', 'DISABLED')),
    status varchar(16) NOT NULL CHECK (
        status IN ('PENDING', 'APPLYING', 'ACTIVE', 'INACTIVE', 'FAILED', 'UNCERTAIN')
    ),
    desired_revision bigint NOT NULL CHECK (desired_revision > 0),
    applied_revision bigint CHECK (
        applied_revision IS NULL
        OR (applied_revision > 0 AND applied_revision <= desired_revision)
    ),
    applied_hostname varchar(253),
    last_error_code varchar(64),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (subdomain ~ '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'),
    CHECK (subdomain !~ '--'),
    CHECK (hostname = lower(hostname)),
    CHECK (applied_hostname IS NULL OR applied_revision IS NOT NULL),
    CHECK (
        status <> 'ACTIVE'
        OR (
            desired_state = 'ENABLED'
            AND applied_revision IS NOT NULL
            AND applied_revision = desired_revision
            AND applied_hostname IS NOT NULL
            AND applied_hostname = hostname
        )
    ),
    CHECK (
        status <> 'INACTIVE'
        OR (
            desired_state = 'DISABLED'
            AND applied_revision IS NOT NULL
            AND applied_revision = desired_revision
            AND applied_hostname IS NULL
        )
    )
);

CREATE UNIQUE INDEX project_public_routes_applied_hostname_uq
    ON project_public_routes(applied_hostname)
    WHERE applied_hostname IS NOT NULL;

CREATE INDEX project_public_routes_routing_snapshot
    ON project_public_routes(hostname, project_id);

CREATE TABLE public_route_jobs (
    project_id uuid PRIMARY KEY REFERENCES project_public_routes(project_id) ON DELETE CASCADE,
    desired_revision bigint NOT NULL CHECK (desired_revision > 0),
    state varchar(16) NOT NULL CHECK (
        state IN ('PENDING', 'CLAIMED', 'SUCCEEDED', 'FAILED')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL,
    lease_owner varchar(128),
    lease_expires_at timestamptz,
    claim_token uuid,
    last_error_code varchar(64),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    CHECK (
        (state = 'CLAIMED' AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL AND claim_token IS NOT NULL)
        OR
        (state <> 'CLAIMED' AND lease_owner IS NULL
            AND lease_expires_at IS NULL AND claim_token IS NULL)
    ),
    CHECK (
        (state IN ('SUCCEEDED', 'FAILED') AND completed_at IS NOT NULL)
        OR
        (state IN ('PENDING', 'CLAIMED') AND completed_at IS NULL)
    )
);

CREATE INDEX public_route_jobs_claim
    ON public_route_jobs(state, available_at, created_at, project_id);
