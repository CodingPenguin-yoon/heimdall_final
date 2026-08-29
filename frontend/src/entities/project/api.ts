import { requestJson } from '@/shared/api/client';

import type {
  Commit,
  DeploymentConfig,
  Project,
  ProjectDeletion,
  ProjectDeletionRequest,
} from './types';

export function listProjects(): Promise<{ items: Project[] }> {
  return requestJson('/projects');
}

export function getProject(projectId: string): Promise<Project> {
  return requestJson(`/projects/${projectId}`);
}

export function createProject(payload: { name: string; repositoryUrl: string }): Promise<Project> {
  return requestJson('/projects', { method: 'POST', body: JSON.stringify(payload) });
}

export function saveProjectSettings(
  projectId: string,
  payload: DeploymentConfig & { expectedVersion: number },
): Promise<Project> {
  return requestJson(`/projects/${projectId}/settings`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function listCommits(projectId: string): Promise<{ items: Commit[] }> {
  return requestJson(`/projects/${projectId}/commits`);
}

export function deleteProject(
  projectId: string,
  payload: ProjectDeletionRequest,
): Promise<ProjectDeletion> {
  return requestJson(`/projects/${projectId}`, {
    method: 'DELETE',
    body: JSON.stringify(payload),
  });
}

export function getProjectDeletion(projectId: string): Promise<ProjectDeletion> {
  return requestJson(`/projects/${projectId}/deletion`);
}

export function retryProjectDeletion(
  projectId: string,
  payload: ProjectDeletionRequest,
): Promise<ProjectDeletion> {
  return requestJson(`/projects/${projectId}/deletion/retry`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
