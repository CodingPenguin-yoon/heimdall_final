import { requestJson } from '@/shared/api/client';

import type {
  Deployment,
  DeploymentEvent,
  RuntimeReconciliation,
  RuntimeReconciliationAction,
  ServiceLogSnapshot,
  ServiceLogStreamLine,
  ServiceLogStreamReady,
} from './types';

export function listDeployments(projectId: string): Promise<{ items: Deployment[] }> {
  return requestJson(`/projects/${projectId}/deployments`);
}

export function listRecentDeployments(): Promise<{ items: Deployment[] }> {
  return requestJson('/deployments');
}

export function getDeployment(deploymentId: string): Promise<Deployment> {
  return requestJson(`/deployments/${deploymentId}`);
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

export function getServiceLogs(
  deploymentId: string,
  serviceName?: string,
): Promise<ServiceLogSnapshot> {
  const query = serviceName ? `?service=${encodeURIComponent(serviceName)}` : '';
  return requestJson(`/deployments/${deploymentId}/service-logs${query}`);
}

export interface ServiceLogStreamHandlers {
  onOpen: () => void;
  onReady: (event: ServiceLogStreamReady) => void;
  onLine: (event: ServiceLogStreamLine) => void;
  onEnd: (reason: string) => void;
  onStreamError: (code: string) => void;
  onConnectionError: () => void;
}

export function subscribeServiceLogs(
  deploymentId: string,
  serviceName: string | undefined,
  handlers: ServiceLogStreamHandlers,
): () => void {
  const query = serviceName ? `?service=${encodeURIComponent(serviceName)}` : '';
  const source = new EventSource(`/api/deployments/${deploymentId}/service-logs/stream${query}`);

  source.onopen = handlers.onOpen;
  source.addEventListener('ready', (event) => {
    const value = parseEvent<ServiceLogStreamReady>(event);
    if (value) handlers.onReady(value);
  });
  source.addEventListener('log', (event) => {
    const value = parseEvent<ServiceLogStreamLine>(event);
    if (value) handlers.onLine(value);
  });
  source.addEventListener('end', (event) => {
    const value = parseEvent<{ reason?: string }>(event);
    source.close();
    handlers.onEnd(value?.reason ?? 'CONTAINER_LOG_ENDED');
  });
  source.addEventListener('stream-error', (event) => {
    const value = parseEvent<{ code?: string }>(event);
    source.close();
    handlers.onStreamError(value?.code ?? 'RUNTIME_LOG_STREAM_UNAVAILABLE');
  });
  source.onerror = handlers.onConnectionError;

  return () => source.close();
}

function parseEvent<T>(event: Event): T | null {
  if (!(event instanceof MessageEvent)) return null;
  try {
    return JSON.parse(String(event.data)) as T;
  } catch {
    return null;
  }
}

export function getRuntimeReconciliation(deploymentId: string): Promise<RuntimeReconciliation> {
  return requestJson(`/deployments/${deploymentId}/runtime-reconciliation`);
}

export function requestRuntimeReconciliation(
  deploymentId: string,
  action: RuntimeReconciliationAction,
  confirmation?: string,
): Promise<RuntimeReconciliation> {
  return requestJson(`/deployments/${deploymentId}/runtime-reconciliation`, {
    method: 'POST',
    body: JSON.stringify({ action, ...(confirmation ? { confirmation } : {}) }),
  });
}
