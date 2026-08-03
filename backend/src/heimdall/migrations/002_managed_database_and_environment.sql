CREATE TABLE project_environment_secrets (
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    service_name varchar(32) NOT NULL,
    variable_name varchar(128) NOT NULL,
    secret_reference text NOT NULL,
    secret_version integer NOT NULL CHECK (secret_version > 0),
    secret_fingerprint char(64) NOT NULL CHECK (secret_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (project_id, service_name, variable_name)
);

CREATE TABLE project_database_resources (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL UNIQUE REFERENCES projects(id) ON DELETE RESTRICT,
    desired_state varchar(16) NOT NULL DEFAULT 'ACTIVE' CHECK (desired_state = 'ACTIVE'),
    status varchar(16) NOT NULL CHECK (status IN ('PROVISIONING', 'ACTIVE', 'FAILED')),
    phase varchar(32) NOT NULL CHECK (
        phase IN (
            'INTENT_RECORDED', 'SECRET_READY', 'ROLE_READY',
            'DATABASE_READY', 'PRIVILEGES_READY', 'ACTIVE'
        )
    ),
    database_name varchar(63) NOT NULL UNIQUE,
    role_name varchar(63) NOT NULL UNIQUE,
    schema_name varchar(63) NOT NULL DEFAULT 'app',
    credential_reference text,
    credential_version integer CHECK (credential_version IS NULL OR credential_version > 0),
    credential_fingerprint char(64) CHECK (
        credential_fingerprint IS NULL OR credential_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    state_version integer NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    failure_stage varchar(32),
    failure_code varchar(64),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (
        (credential_reference IS NULL AND credential_version IS NULL AND credential_fingerprint IS NULL)
        OR
        (credential_reference IS NOT NULL AND credential_version IS NOT NULL AND credential_fingerprint IS NOT NULL)
    )
);

CREATE INDEX project_database_resources_status
ON project_database_resources(status, updated_at);
