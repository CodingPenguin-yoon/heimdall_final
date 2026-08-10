import { queryOptions } from '@tanstack/react-query';

import {
  getDeployment,
  getRuntimeReconciliation,
  listDeploymentEvents,
  listDeployments,
  listRecentDeployments,
} from './api';
import { isDeploymentTerminal } from './presentation';

export const deploymentKeys = {
  activity: ['deployments', 'activity'] as const,
  project: (projectId: string) => ['projects', projectId, 'deployments'] as const,
  detail: (deploymentId: string) => ['deployments', deploymentId] as const,
  events: (deploymentId: string) => ['deployments', deploymentId, 'events'] as const,
  reconciliation: (deploymentId: string) =>
    ['deployments', deploymentId, 'runtime-reconciliation'] as const,
};

export const deploymentActivityQuery = () =>
  queryOptions({
    queryKey: deploymentKeys.activity,
    queryFn: listRecentDeployments,
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => !isDeploymentTerminal(item.status)) ? 1_000 : false,
  });

export const deploymentsQuery = (projectId: string) =>
  queryOptions({
    queryKey: deploymentKeys.project(projectId),
    queryFn: () => listDeployments(projectId),
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => !isDeploymentTerminal(item.status)) ? 1_000 : false,
  });

export const deploymentQuery = (deploymentId: string) =>
  queryOptions({
    queryKey: deploymentKeys.detail(deploymentId),
    queryFn: () => getDeployment(deploymentId),
    enabled: Boolean(deploymentId),
    refetchInterval: (query) =>
      query.state.data && !isDeploymentTerminal(query.state.data.status) ? 1_000 : false,
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
