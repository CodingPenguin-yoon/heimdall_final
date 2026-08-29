export type ProjectStatus = 'DRAFT' | 'READY' | 'DELETING';

export type ProjectDeletionState = 'PENDING' | 'CLAIMED' | 'FAILED';

export interface ProjectDeletion {
  projectId: string;
  state: ProjectDeletionState;
  phase: string;
  attempts: number;
  availableAt: string;
  lastErrorCode: string | null;
  lastErrorRetryable: boolean | null;
  deleteManagedDatabase: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface ProjectDeletionRequest {
  confirmation: string;
  deleteManagedDatabase: boolean;
  managedDatabaseConfirmation: string | null;
}

export interface EnvironmentVariable {
  name: string;
  kind: 'PLAIN' | 'SECRET';
  value?: string;
  configured?: boolean;
}

export interface ServiceConfig {
  name: string;
  build: { context: string; dockerfile: string };
  internalPort: number;
  healthPath: string;
  environment: EnvironmentVariable[];
  projectDatabaseAccess: boolean;
}

export interface RouteConfig {
  path: string;
  service: string;
}

export interface DeploymentConfig {
  services: ServiceConfig[];
  routes: RouteConfig[];
}

export interface Project {
  id: string;
  name: string;
  repositoryUrl: string;
  branch: 'main';
  status: ProjectStatus;
  hasManagedDatabase: boolean;
  configVersion: number;
  deploymentConfig: DeploymentConfig | null;
  createdAt: string;
  updatedAt: string;
}

export interface Commit {
  sha: string;
  authorName: string;
  committedAt: string;
  subject: string;
}
