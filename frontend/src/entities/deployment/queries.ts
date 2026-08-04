import { queryOptions } from '@tanstack/react-query';

import { listDeploymentEvents, listDeployments } from './api';

const terminalStatuses = new Set(['SUCCEEDED', 'FAILED']);

export const deploymentKeys = {
  project: (projectId: string) => ['projects', projectId, 'deployments'] as const,
  events: (deploymentId: string) => ['deployments', deploymentId, 'events'] as const,
};

export const deploymentsQuery = (projectId: string) =>
  queryOptions({
    queryKey: deploymentKeys.project(projectId),
    queryFn: () => listDeployments(projectId),
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => !terminalStatuses.has(item.status)) ? 1_000 : false,
  });

export const deploymentEventsQuery = (deploymentId: string | undefined, active: boolean) =>
  queryOptions({
    queryKey: deploymentKeys.events(deploymentId ?? 'none'),
    queryFn: () => listDeploymentEvents(deploymentId ?? ''),
    enabled: Boolean(deploymentId),
    refetchInterval: active ? 1_000 : false,
  });
