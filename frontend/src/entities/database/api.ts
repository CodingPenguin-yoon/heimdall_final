import { requestJson } from '@/shared/api/client';

import type { ProjectDatabase } from './types';

export function getProjectDatabase(projectId: string): Promise<ProjectDatabase> {
  return requestJson(`/projects/${projectId}/database`);
}

export function provisionProjectDatabase(projectId: string): Promise<ProjectDatabase> {
  return requestJson(`/projects/${projectId}/database`, { method: 'POST' });
}
