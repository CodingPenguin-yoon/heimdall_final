export type ProjectStatus = 'DRAFT' | 'READY';

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
