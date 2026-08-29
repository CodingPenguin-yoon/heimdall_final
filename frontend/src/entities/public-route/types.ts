export type PublicRouteDesiredState = 'ENABLED' | 'DISABLED';

export type PublicRouteStatus =
  'PENDING' | 'APPLYING' | 'ACTIVE' | 'INACTIVE' | 'FAILED' | 'UNCERTAIN';

export interface PublicRoute {
  projectId: string;
  subdomain: string;
  hostname: string;
  desiredState: PublicRouteDesiredState;
  status: PublicRouteStatus;
  desiredRevision: number;
  appliedRevision: number | null;
  appliedHostname: string | null;
  lastErrorCode: string | null;
  createdAt: string;
  updatedAt: string;
}
