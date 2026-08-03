import { requestJson } from '@/shared/api/client';

import type { Deployment } from './types';

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
