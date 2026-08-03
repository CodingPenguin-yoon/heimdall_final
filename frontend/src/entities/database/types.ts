export type ProjectDatabaseStatus = 'NOT_CREATED' | 'PROVISIONING' | 'ACTIVE' | 'FAILED';

export interface ProjectDatabase {
  required: boolean;
  status: ProjectDatabaseStatus;
  id: string | null;
  phase: string | null;
  databaseName: string | null;
  username: string | null;
  schemaName: string | null;
  host: string | null;
  port: number | null;
  connectedServices: string[];
  failureStage: string | null;
  failureCode: string | null;
  updatedAt: string | null;
}
