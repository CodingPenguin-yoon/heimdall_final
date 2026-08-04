from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from heimdall.database import Database


@dataclass(frozen=True, slots=True)
class ProjectRuntime:
    project_id: UUID
    gateway_container_name: str
    preview_port: int
    active_deployment_id: UUID | None
    active_network_name: str | None
    active_container_names: tuple[str, ...]
    active_image_names: tuple[str, ...]
    updated_at: datetime


class RuntimeRepository(Protocol):
    def get(self, project_id: UUID) -> ProjectRuntime | None: ...

    def ensure_gateway(
        self, project_id: UUID, gateway_container_name: str, preview_port: int
    ) -> ProjectRuntime: ...

    def activate(
        self,
        project_id: UUID,
        deployment_id: UUID,
        network_name: str,
        container_names: tuple[str, ...],
        image_names: tuple[str, ...],
    ) -> ProjectRuntime | None: ...


class PostgresRuntimeRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, project_id: UUID) -> ProjectRuntime | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM project_runtimes WHERE project_id = %s", (project_id,)
            ).fetchone()
        return _runtime(row) if row is not None else None

    def ensure_gateway(
        self, project_id: UUID, gateway_container_name: str, preview_port: int
    ) -> ProjectRuntime:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            connection.execute(
                """
                INSERT INTO project_runtimes (
                    project_id, gateway_container_name, preview_port, updated_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (project_id) DO NOTHING
                """,
                (project_id, gateway_container_name, preview_port, now),
            )
            row = connection.execute(
                "SELECT * FROM project_runtimes WHERE project_id = %s FOR UPDATE",
                (project_id,),
            ).fetchone()
        runtime = _runtime(row)
        if (
            runtime.gateway_container_name != gateway_container_name
            or runtime.preview_port != preview_port
        ):
            raise RuntimeError("gateway observation conflicts with stored runtime metadata")
        return runtime

    def activate(
        self,
        project_id: UUID,
        deployment_id: UUID,
        network_name: str,
        container_names: tuple[str, ...],
        image_names: tuple[str, ...],
    ) -> ProjectRuntime | None:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            previous_row = connection.execute(
                "SELECT * FROM project_runtimes WHERE project_id = %s FOR UPDATE",
                (project_id,),
            ).fetchone()
            if previous_row is None:
                raise RuntimeError("project gateway metadata is missing")
            connection.execute(
                """
                UPDATE project_runtimes
                SET active_deployment_id = %s,
                    active_network_name = %s,
                    active_container_names = %s::jsonb,
                    active_image_names = %s::jsonb,
                    updated_at = %s
                WHERE project_id = %s
                """,
                (
                    deployment_id,
                    network_name,
                    json.dumps(container_names),
                    json.dumps(image_names),
                    now,
                    project_id,
                ),
            )
        return _runtime(previous_row)


def _runtime(row: dict) -> ProjectRuntime:
    return ProjectRuntime(
        project_id=row["project_id"],
        gateway_container_name=row["gateway_container_name"],
        preview_port=row["preview_port"],
        active_deployment_id=row["active_deployment_id"],
        active_network_name=row["active_network_name"],
        active_container_names=tuple(row["active_container_names"]),
        active_image_names=tuple(row["active_image_names"]),
        updated_at=row["updated_at"],
    )
