CREATE TABLE deployment_diagnostic_artifacts (
    id UUID PRIMARY KEY,
    deployment_id UUID NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    event_id BIGINT NOT NULL REFERENCES deployment_events(id) ON DELETE CASCADE,
    kind VARCHAR(24) NOT NULL CHECK (kind IN ('COMMAND_OUTPUT', 'SERVICE_LOG')),
    failure_stage VARCHAR(32) NOT NULL,
    failure_code VARCHAR(64) NOT NULL,
    capture_status VARCHAR(24) NOT NULL CHECK (capture_status IN ('CAPTURED', 'UNAVAILABLE')),
    capture_code VARCHAR(64),
    operation VARCHAR(64),
    service_name VARCHAR(32),
    return_code INTEGER,
    container_status VARCHAR(32),
    container_exit_code INTEGER,
    line_count INTEGER NOT NULL CHECK (line_count >= 0),
    byte_count INTEGER NOT NULL CHECK (byte_count >= 0 AND byte_count <= 262144),
    truncated BOOLEAN NOT NULL,
    payload JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (kind = 'COMMAND_OUTPUT' AND operation IS NOT NULL)
        OR (kind = 'SERVICE_LOG' AND service_name IS NOT NULL)
    ),
    CHECK (
        (capture_status = 'CAPTURED' AND capture_code IS NULL)
        OR (capture_status = 'UNAVAILABLE' AND capture_code IS NOT NULL AND line_count = 0)
    )
);

CREATE UNIQUE INDEX deployment_diagnostic_artifacts_event_source_uq
    ON deployment_diagnostic_artifacts (
        event_id,
        kind,
        COALESCE(operation, ''),
        COALESCE(service_name, '')
    );

CREATE INDEX deployment_diagnostic_artifacts_deployment_event_idx
    ON deployment_diagnostic_artifacts (deployment_id, event_id, captured_at, id);

CREATE INDEX deployment_diagnostic_artifacts_expiry_idx
    ON deployment_diagnostic_artifacts (expires_at, id);
