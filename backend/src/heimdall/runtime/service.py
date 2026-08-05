from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol

from heimdall.deployments.models import Deployment, DeploymentStatus
from heimdall.deployments.worker import RecoveryDisposition, RuntimeFailure, RuntimeProgress
from heimdall.git.client import GitAccessError, GitClient
from heimdall.projects.service import ProjectService
from heimdall.runtime.docker import CandidateGeneration, DockerRuntime
from heimdall.runtime.models import RuntimeConfigurationError, RuntimeDeployment
from heimdall.secrets.store import SecretStore


class RuntimeActivator(Protocol):
    def recover(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        progress: RuntimeProgress,
    ) -> RecoveryDisposition: ...

    def is_active(self, deployment: Deployment) -> bool: ...

    def activate(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        candidate: CandidateGeneration,
        progress: RuntimeProgress,
    ) -> None: ...

    def rollback_candidate(self, deployment: Deployment) -> None: ...


class DockerDeploymentProcessor:
    def __init__(
        self,
        projects: ProjectService,
        git: GitClient,
        docker: DockerRuntime,
        activator: RuntimeActivator,
        secret_store: SecretStore,
        workspace_root: Path,
    ) -> None:
        self._projects = projects
        self._git = git
        self._docker = docker
        self._activator = activator
        self._secret_store = secret_store
        self._workspace_root = workspace_root.resolve()

    def recover(self, deployment: Deployment, progress: RuntimeProgress) -> RecoveryDisposition:
        try:
            runtime = RuntimeDeployment.from_deployment(deployment)
        except RuntimeConfigurationError:
            return RecoveryDisposition.UNCERTAIN
        return self._activator.recover(deployment, runtime, progress)

    def process(self, deployment: Deployment, progress: RuntimeProgress) -> None:
        try:
            runtime = RuntimeDeployment.from_deployment(deployment)
        except RuntimeConfigurationError as error:
            raise RuntimeFailure("CONFIGURATION", "SNAPSHOT_INVALID") from error
        if self._activator.is_active(deployment):
            progress.stage(
                DeploymentStatus.ACTIVATING,
                "GATEWAY_ALREADY_ACTIVE",
                "The candidate was already activated before the worker resumed",
            )
            return
        project = self._projects.get(deployment.project_id)
        workspace = self._workspace(deployment)
        self._reset_workspace(workspace)
        progress.heartbeat()
        try:
            self._git.checkout_exact(
                project.repository_url,
                deployment.resolved_commit_sha,
                workspace,
            )
        except GitAccessError as error:
            raise RuntimeFailure("SOURCE", "SOURCE_CHECKOUT_FAILED", retryable=True) from error
        progress.heartbeat()
        candidate = self._docker.start_candidate(
            deployment,
            runtime,
            workspace,
            self._secret_store,
            progress,
        )
        progress.stage(
            DeploymentStatus.ACTIVATING,
            "GATEWAY_ACTIVATING",
            "Switching the stable project gateway to the healthy candidate",
        )
        self._activator.activate(deployment, runtime, candidate, progress)
        self._remove_workspace(workspace)

    def cleanup_candidate(self, deployment: Deployment) -> None:
        try:
            runtime = RuntimeDeployment.from_deployment(deployment)
        except RuntimeConfigurationError:
            return
        self._activator.rollback_candidate(deployment)
        self._docker.cleanup_candidate(deployment, runtime)
        self._remove_workspace(self._workspace(deployment))

    def cleanup_candidate_verified(self, deployment: Deployment, progress: RuntimeProgress) -> None:
        try:
            runtime = RuntimeDeployment.from_deployment(deployment)
        except RuntimeConfigurationError as error:
            raise RuntimeFailure(
                "RECONCILIATION",
                "SNAPSHOT_INVALID",
                cleanup_candidate=False,
            ) from error
        if self._activator.is_active(deployment):
            raise RuntimeFailure(
                "RECONCILIATION",
                "ACTIVE_GENERATION_CANNOT_BE_CLEANED",
                cleanup_candidate=False,
            )
        self._activator.rollback_candidate(deployment)
        progress.heartbeat()
        self._docker.cleanup_candidate_verified(deployment, runtime, progress)
        self._remove_workspace(self._workspace(deployment))

    def _workspace(self, deployment: Deployment) -> Path:
        return self._workspace_root / deployment.id.hex

    def _reset_workspace(self, workspace: Path) -> None:
        _ensure_private_directory(self._workspace_root)
        self._remove_workspace(workspace)
        workspace.mkdir(mode=0o700)

    def _remove_workspace(self, workspace: Path) -> None:
        resolved_parent = workspace.parent.resolve()
        if resolved_parent != self._workspace_root or workspace.name == "":
            raise RuntimeFailure("SOURCE", "WORKSPACE_PATH_INVALID")
        if workspace.is_symlink():
            workspace.unlink()
        elif workspace.exists():
            shutil.rmtree(workspace)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeFailure("SOURCE", "WORKSPACE_ROOT_INVALID")
    os.chmod(path, 0o700)
