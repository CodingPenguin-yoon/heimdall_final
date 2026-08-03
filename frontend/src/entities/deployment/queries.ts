import { queryOptions } from '@tanstack/react-query';

import { listDeployments } from './api';

export const deploymentKeys = {
  project: (projectId: string) => ['projects', projectId, 'deployments'] as const,
};

export const deploymentsQuery = (projectId: string) =>
  queryOptions({
    queryKey: deploymentKeys.project(projectId),
    queryFn: () => listDeployments(projectId),
  });
