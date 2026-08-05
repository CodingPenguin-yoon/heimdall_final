CREATE TABLE runtime_reconciliations (
    deployment_id uuid PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
    action varchar(24) NOT NULL CHECK (action IN ('RECONCILE', 'FORCE_CLEANUP')),
    requested_by varchar(16) NOT NULL CHECK (requested_by IN ('SYSTEM', 'ADMIN')),
    state varchar(16) NOT NULL CHECK (
        state IN ('PENDING', 'CLAIMED', 'RESOLVED', 'BLOCKED')
    ),
    result varchar(16) CHECK (result IN ('ACTIVE', 'CLEANED', 'UNCERTAIN')),
    result_code varchar(64),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL,
    lease_owner varchar(128),
    lease_expires_at timestamptz,
    claim_token uuid,
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
        (state IN ('RESOLVED', 'BLOCKED') AND completed_at IS NOT NULL)
        OR
        (state IN ('PENDING', 'CLAIMED') AND completed_at IS NULL)
    )
);

CREATE INDEX runtime_reconciliations_claim
ON runtime_reconciliations(state, available_at, created_at);
