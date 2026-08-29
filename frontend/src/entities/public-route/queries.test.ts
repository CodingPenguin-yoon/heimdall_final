import { describe, expect, it } from 'vitest';

import { publicRouteQuery } from './queries';
import type { PublicRoute, PublicRouteStatus } from './types';

const route: PublicRoute = {
  projectId: 'project-1',
  subdomain: 'student-a',
  hostname: 'student-a.deploy.example',
  desiredState: 'ENABLED',
  status: 'PENDING',
  desiredRevision: 1,
  appliedRevision: null,
  appliedHostname: null,
  lastErrorCode: null,
  createdAt: '2026-08-21T00:00:00Z',
  updatedAt: '2026-08-21T00:00:00Z',
};

describe('publicRouteQuery', () => {
  it('polls only while a route is pending or applying', () => {
    const interval = publicRouteQuery('project-1').refetchInterval as unknown as (query: {
      state: { data: PublicRoute | null | undefined };
    }) => number | false;
    const value = (status: PublicRouteStatus) =>
      interval({ state: { data: { ...route, status } } });

    expect(value('PENDING')).toBe(1_000);
    expect(value('APPLYING')).toBe(1_000);
    expect(value('ACTIVE')).toBe(false);
    expect(value('INACTIVE')).toBe(false);
    expect(value('FAILED')).toBe(false);
    expect(value('UNCERTAIN')).toBe(false);
    expect(interval({ state: { data: null } })).toBe(false);
  });
});
