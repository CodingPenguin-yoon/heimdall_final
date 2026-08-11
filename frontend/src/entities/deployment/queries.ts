import { queryOptions } from '@tanstack/react-query';

import {
  getDeployment,
  getRuntimeReconciliation,
  getServiceLogs,
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
  serviceLogs: (deploymentId: string, serviceName?: string) =>
    ['deployments', deploymentId, 'service-logs', serviceName ?? 'root'] as const,
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

export const deploymentEventsQuery = (deploymentId: string | undefined) =>
  queryOptions({
    queryKey: deploymentKeys.events(deploymentId ?? 'none'),
    queryFn: () => listDeploymentEvents(deploymentId ?? ''),
    enabled: Boolean(deploymentId),
  });

export const deploymentServiceLogsQuery = (deploymentId: string, serviceName?: string) =>
  queryOptions({
    queryKey: deploymentKeys.serviceLogs(deploymentId, serviceName),
    queryFn: () => getServiceLogs(deploymentId, serviceName),
    enabled: Boolean(deploymentId),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  });

export const runtimeReconciliationQuery = (deploymentId: string) =>
  queryOptions({
    queryKey: deploymentKeys.reconciliation(deploymentId),
    queryFn: () => getRuntimeReconciliation(deploymentId),
    refetchInterval: (query) =>
      ['PENDING', 'CLAIMED'].includes(query.state.data?.state ?? '') ? 1_000 : false,
  });
