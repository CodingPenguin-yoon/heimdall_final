import { requestJson } from '@/shared/api/client';

import type { ProjectRuntime } from './types';

export function getProjectRuntime(projectId: string): Promise<ProjectRuntime> {
  return requestJson(`/projects/${projectId}/runtime`);
}
