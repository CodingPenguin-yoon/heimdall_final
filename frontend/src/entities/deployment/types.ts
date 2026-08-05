export type DeploymentStatus =
  | 'QUEUED'
  | 'PREPARING'
  | 'BUILDING'
  | 'STARTING'
  | 'HEALTH_CHECKING'
  | 'ACTIVATING'
  | 'SUCCEEDED'
  | 'FAILED';

export interface Deployment {
  id: string;
  projectId: string;
  sourceType: 'MAIN_HEAD' | 'MAIN_COMMIT';
  requestedCommitSha: string | null;
  resolvedCommitSha: string;
  configVersion: number;
  status: DeploymentStatus;
  failureStage: string | null;
  failureCode: string | null;
  createdAt: string;
  updatedAt: string;
  terminalAt: string | null;
}

export interface DeploymentEvent {
  id: number;
  deploymentId: string;
  stage: string;
  code: string;
  message: string;
  createdAt: string;
}

export type RuntimeReconciliationAction = 'RECONCILE' | 'FORCE_CLEANUP';

export interface RuntimeReconciliation {
  deploymentId: string;
  state: 'RETAINED' | 'PENDING' | 'CLAIMED' | 'RESOLVED' | 'BLOCKED';
  action: RuntimeReconciliationAction;
  requestedBy: 'SYSTEM' | 'ADMIN';
  result: 'ACTIVE' | 'CLEANED' | 'UNCERTAIN' | null;
  resultCode: string | null;
  attempts: number;
  availableAt: string;
  updatedAt: string;
  completedAt: string | null;
}
