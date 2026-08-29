ALTER TABLE projects DROP CONSTRAINT projects_status_check;
ALTER TABLE projects ADD CONSTRAINT projects_status_check
    CHECK (status IN ('DRAFT', 'READY', 'DELETING'));

CREATE TABLE project_deletion_jobs (
    project_id uuid PRIMARY KEY REFERENCES projects(id),
    state varchar(16) NOT NULL CHECK (state IN ('PENDING', 'CLAIMED', 'FAILED')),
    phase varchar(32) NOT NULL CHECK (
        phase IN (
            'REQUESTED', 'WAITING_FOR_OPERATIONS', 'ROUTE_DISABLING', 'ROUTE_REMOVED',
            'RUNTIME_CLEANUP', 'DATABASE_QUIESCING', 'DATABASE_DROP_DATABASE',
            'DATABASE_DROP_ROLE', 'SECRET_CLEANUP', 'METADATA_DELETE'
        )
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL,
    lease_owner varchar(128),
    lease_expires_at timestamptz,
    claim_token uuid,
    last_error_code varchar(64),
    last_error_retryable boolean,
    delete_managed_database boolean NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (
        (state = 'CLAIMED' AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL AND claim_token IS NOT NULL)
        OR
        (state <> 'CLAIMED' AND lease_owner IS NULL
            AND lease_expires_at IS NULL AND claim_token IS NULL)
    ),
    CHECK (
        (state = 'FAILED' AND last_error_code IS NOT NULL
            AND last_error_retryable IS NOT NULL)
        OR
        (state <> 'FAILED' AND last_error_code IS NULL
            AND last_error_retryable IS NULL)
    )
);

CREATE INDEX project_deletion_jobs_claim
    ON project_deletion_jobs(state, available_at, created_at, project_id);
