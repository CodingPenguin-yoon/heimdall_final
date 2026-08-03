CREATE TABLE projects (
    id uuid PRIMARY KEY,
    name varchar(100) NOT NULL UNIQUE,
    repository_url text NOT NULL UNIQUE,
    branch varchar(64) NOT NULL DEFAULT 'main' CHECK (branch = 'main'),
    status varchar(16) NOT NULL CHECK (status IN ('DRAFT', 'READY')),
    config_version integer NOT NULL DEFAULT 0 CHECK (config_version >= 0),
    deployment_config jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE deployments (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    source_type varchar(32) NOT NULL CHECK (source_type IN ('MAIN_HEAD', 'MAIN_COMMIT')),
    requested_commit_sha char(40),
    resolved_commit_sha char(40) NOT NULL,
    config_version integer NOT NULL CHECK (config_version > 0),
    config_snapshot jsonb NOT NULL,
    status varchar(24) NOT NULL CHECK (
        status IN (
            'QUEUED', 'PREPARING', 'BUILDING', 'STARTING',
            'HEALTH_CHECKING', 'ACTIVATING', 'SUCCEEDED', 'FAILED'
        )
    ),
    failure_stage varchar(32),
    failure_code varchar(64),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    terminal_at timestamptz,
    CHECK (requested_commit_sha IS NULL OR requested_commit_sha ~ '^[0-9a-f]{40}$'),
    CHECK (resolved_commit_sha ~ '^[0-9a-f]{40}$')
);

CREATE UNIQUE INDEX one_active_deployment_per_project
ON deployments(project_id)
WHERE status IN (
    'QUEUED', 'PREPARING', 'BUILDING', 'STARTING',
    'HEALTH_CHECKING', 'ACTIVATING'
);

CREATE INDEX deployments_project_history
ON deployments(project_id, created_at DESC, id ASC);

CREATE TABLE deployment_jobs (
    deployment_id uuid PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
    state varchar(16) NOT NULL CHECK (state IN ('PENDING', 'CLAIMED', 'DONE')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL,
    lease_owner varchar(128),
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE INDEX deployment_jobs_claim
ON deployment_jobs(state, available_at, created_at);
