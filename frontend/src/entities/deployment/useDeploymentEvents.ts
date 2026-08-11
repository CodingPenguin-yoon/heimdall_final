import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { subscribeDeploymentEvents } from './api';
import { deploymentEventsQuery } from './queries';
import type { DeploymentEvent } from './types';

export type DeploymentEventConnection =
  'CONNECTING' | 'LIVE' | 'RECONNECTING' | 'COMPLETE' | 'ERROR';

interface LiveDeploymentEvents {
  deploymentId: string;
  items: DeploymentEvent[];
  connection: DeploymentEventConnection;
}

export function useDeploymentEvents(deploymentId: string | undefined, active: boolean) {
  const query = useQuery(deploymentEventsQuery(deploymentId));
  const { data, isError, isLoading, isSuccess, refetch } = query;
  const [live, setLive] = useState<LiveDeploymentEvents>({
    deploymentId: '',
    items: [],
    connection: 'CONNECTING',
  });
  const endedRef = useRef({ deploymentId: '', ended: false });
  const currentLive = live.deploymentId === deploymentId ? live : null;
  const initialItems = data?.items ?? [];
  const items = mergeEvents(initialItems, currentLive?.items ?? []);

  useEffect(() => {
    if (!deploymentId || !active || !isSuccess) return;
    if (endedRef.current.deploymentId !== deploymentId) {
      endedRef.current = { deploymentId, ended: false };
    }
    if (endedRef.current.ended) return;
    const afterId = data.items.at(-1)?.id ?? 0;

    return subscribeDeploymentEvents(deploymentId, afterId, {
      onOpen: () =>
        setLive((current) => ({
          deploymentId,
          items: current.deploymentId === deploymentId ? current.items : [],
          connection: current.connection === 'RECONNECTING' ? 'RECONNECTING' : 'CONNECTING',
        })),
      onReady: () =>
        setLive((current) => ({
          deploymentId,
          items: current.deploymentId === deploymentId ? current.items : [],
          connection: 'LIVE',
        })),
      onEvent: (event) =>
        setLive((current) => {
          const currentItems = current.deploymentId === deploymentId ? current.items : [];
          if (currentItems.some((item) => item.id === event.id)) {
            return { deploymentId, items: currentItems, connection: 'LIVE' };
          }
          return {
            deploymentId,
            items: [...currentItems, event].slice(-100),
            connection: 'LIVE',
          };
        }),
      onEnd: () => {
        endedRef.current = { deploymentId, ended: true };
        setLive((current) => ({
          deploymentId,
          items: current.deploymentId === deploymentId ? current.items : [],
          connection: 'COMPLETE',
        }));
        void refetch();
      },
      onStreamError: () =>
        setLive((current) => ({
          deploymentId,
          items: current.deploymentId === deploymentId ? current.items : [],
          connection: 'ERROR',
        })),
      onConnectionError: () =>
        setLive((current) => ({
          deploymentId,
          items: current.deploymentId === deploymentId ? current.items : [],
          connection: 'RECONNECTING',
        })),
    });
  }, [active, data, deploymentId, isSuccess, refetch]);

  return {
    items,
    loading: isLoading,
    error: isError,
    connection: active ? (currentLive?.connection ?? 'CONNECTING') : 'COMPLETE',
  };
}

function mergeEvents(initial: DeploymentEvent[], live: DeploymentEvent[]): DeploymentEvent[] {
  const byId = new Map(initial.map((event) => [event.id, event]));
  live.forEach((event) => byId.set(event.id, event));
  return [...byId.values()].sort((left, right) => left.id - right.id).slice(-100);
}
