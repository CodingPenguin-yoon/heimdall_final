import { requestJson } from '@/shared/api/client';

import type { Deployment, DeploymentEvent } from './types';

export function listDeployments(projectId: string): Promise<{ items: Deployment[] }> {
  return requestJson(`/projects/${projectId}/deployments`);
}

export function createDeployment(
  projectId: string,
  source: { type: 'MAIN_HEAD' } | { type: 'MAIN_COMMIT'; commitSha: string },
): Promise<Deployment> {
  return requestJson(`/projects/${projectId}/deployments`, {
    method: 'POST',
    body: JSON.stringify({ source }),
  });
}

export function listDeploymentEvents(deploymentId: string): Promise<{ items: DeploymentEvent[] }> {
  return requestJson(`/deployments/${deploymentId}/events`);
}
