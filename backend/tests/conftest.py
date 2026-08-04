from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from heimdall.deployments.models import Deployment, DeploymentSource, DeploymentStatus
from heimdall.git.client import Commit
from heimdall.projects.models import (
    Project,
    ProjectEnvironmentSecret,
    ProjectStatus,
    ProjectVersionConflictError,
)
from heimdall.secrets.store import StoredSecret


class MemorySecretStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[str, str]] = {}

    def create(self, reference_root: str, version: int, value: str | None = None) -> StoredSecret:
        resolved = value or f"generated-secret-v{version}"
        reference = f"{reference_root}/v{version}.secret"
        fingerprint = f"{version:064x}"
        self.items[reference] = (resolved, fingerprint)
        return StoredSecret(reference, version, fingerprint)

    def read(self, reference: str, fingerprint: str) -> str:
        value, stored_fingerprint = self.items[reference]
        assert stored_fingerprint == fingerprint
        return value

    def resolve(self, reference: str, fingerprint: str) -> Path:
        raise NotImplementedError


class FakeGit:
    def __init__(self) -> None:
        self.validated: list[str] = []
        self.items = [
            Commit(
                sha="a" * 40,
                author_name="Yoon",
                committed_at=datetime(2026, 8, 3, tzinfo=UTC),
                subject="Build the first slice",
            ),
            Commit(
                sha="b" * 40,
                author_name="Yoon",
                committed_at=datetime(2026, 8, 2, tzinfo=UTC),
                subject="Prepare architecture",
            ),
        ]

    def validate_main(self, repository_url: str) -> None:
        self.validated.append(repository_url)

    def recent_commits(self, _: str) -> list[Commit]:
        return list(self.items)


class MemoryProjects:
    def __init__(self) -> None:
        self.items: dict[UUID, Project] = {}
        self.secrets: dict[tuple[UUID, str, str], ProjectEnvironmentSecret] = {}

    def create(self, name: str, repository_url: str) -> Project:
        now = datetime.now(UTC)
        project = Project(
            id=uuid4(),
            name=name,
            repository_url=repository_url,
            branch="main",
            status=ProjectStatus.DRAFT,
            config_version=0,
            deployment_config=None,
            created_at=now,
            updated_at=now,
        )
        self.items[project.id] = project
        return project

    def list(self) -> Sequence[Project]:
        return list(self.items.values())

    def get(self, project_id: UUID) -> Project:
        return self.items[project_id]

    def get_environment_secret(
        self, project_id: UUID, service_name: str, variable_name: str
    ) -> ProjectEnvironmentSecret | None:
        return self.secrets.get((project_id, service_name, variable_name))

    def update_settings(
        self,
        project_id: UUID,
        expected_version: int,
        deployment_config: dict,
        environment_secrets: Sequence[ProjectEnvironmentSecret],
    ) -> Project:
        current = self.items[project_id]
        if current.config_version != expected_version:
            raise ProjectVersionConflictError
        updated = Project(
            id=current.id,
            name=current.name,
            repository_url=current.repository_url,
            branch=current.branch,
            status=ProjectStatus.READY,
            config_version=current.config_version + 1,
            deployment_config=deployment_config,
            created_at=current.created_at,
            updated_at=datetime.now(UTC),
        )
        self.items[project_id] = updated
        active = {
            (secret.project_id, secret.service_name, secret.variable_name)
            for secret in environment_secrets
        }
        self.secrets = {
            key: value
            for key, value in self.secrets.items()
            if key[0] != project_id or key in active
        }
        for secret in environment_secrets:
            self.secrets[(secret.project_id, secret.service_name, secret.variable_name)] = secret
        return updated


class MemoryDeployments:
    def __init__(self) -> None:
        self.items: dict[UUID, Deployment] = {}

    def create(
        self,
        *,
        project_id: UUID,
        source_type: DeploymentSource,
        requested_commit_sha: str | None,
        resolved_commit_sha: str,
        config_version: int,
        config_snapshot: dict[str, Any],
    ) -> Deployment:
        now = datetime.now(UTC)
        deployment = Deployment(
            id=uuid4(),
            project_id=project_id,
            source_type=source_type,
            requested_commit_sha=requested_commit_sha,
            resolved_commit_sha=resolved_commit_sha,
            config_version=config_version,
            config_snapshot=config_snapshot.copy(),
            status=DeploymentStatus.QUEUED,
            failure_stage=None,
            failure_code=None,
            created_at=now,
            updated_at=now,
            terminal_at=None,
        )
        self.items[deployment.id] = deployment
        return deployment

    def list_for_project(self, project_id: UUID) -> Sequence[Deployment]:
        return [item for item in self.items.values() if item.project_id == project_id]

    def get(self, deployment_id: UUID) -> Deployment:
        return self.items[deployment_id]
