ALTER TABLE deployment_jobs
ADD COLUMN claim_token uuid;

CREATE TABLE deployment_events (
    id bigserial PRIMARY KEY,
    deployment_id uuid NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    stage varchar(32) NOT NULL,
    code varchar(64) NOT NULL,
    message varchar(300) NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX deployment_events_recent
ON deployment_events(deployment_id, id DESC);

CREATE TABLE project_runtimes (
    project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    gateway_container_name varchar(128) NOT NULL UNIQUE,
    preview_port integer NOT NULL CHECK (preview_port BETWEEN 1 AND 65535),
    active_deployment_id uuid REFERENCES deployments(id),
    active_network_name varchar(128),
    active_container_names jsonb NOT NULL DEFAULT '[]'::jsonb,
    active_image_names jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL,
    CHECK (
        (active_deployment_id IS NULL AND active_network_name IS NULL)
        OR
        (active_deployment_id IS NOT NULL AND active_network_name IS NOT NULL)
    )
);
