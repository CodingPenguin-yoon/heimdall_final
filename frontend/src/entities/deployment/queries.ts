import { queryOptions } from '@tanstack/react-query';

import { getRuntimeReconciliation, listDeploymentEvents, listDeployments } from './api';

const terminalStatuses = new Set(['SUCCEEDED', 'FAILED']);

export const deploymentKeys = {
  project: (projectId: string) => ['projects', projectId, 'deployments'] as const,
  events: (deploymentId: string) => ['deployments', deploymentId, 'events'] as const,
  reconciliation: (deploymentId: string) =>
    ['deployments', deploymentId, 'runtime-reconciliation'] as const,
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

export const runtimeReconciliationQuery = (deploymentId: string) =>
  queryOptions({
    queryKey: deploymentKeys.reconciliation(deploymentId),
    queryFn: () => getRuntimeReconciliation(deploymentId),
    refetchInterval: (query) =>
      ['PENDING', 'CLAIMED'].includes(query.state.data?.state ?? '') ? 1_000 : false,
  });
