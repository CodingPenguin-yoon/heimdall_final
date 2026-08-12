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

export type DeploymentDiagnosticKind = 'COMMAND_OUTPUT' | 'SERVICE_LOG';
export type DeploymentDiagnosticCaptureStatus = 'CAPTURED' | 'UNAVAILABLE';

export interface DeploymentDiagnosticMetadata {
  id: string;
  deploymentId: string;
  eventId: number;
  kind: DeploymentDiagnosticKind;
  failureStage: string;
  failureCode: string;
  captureStatus: DeploymentDiagnosticCaptureStatus;
  captureCode: string | null;
  operation: string | null;
  serviceName: string | null;
  returnCode: number | null;
  containerStatus: string | null;
  containerExitCode: number | null;
  lineCount: number;
  byteCount: number;
  truncated: boolean;
  capturedAt: string;
  expiresAt: string;
}

export interface DeploymentDiagnostic extends DeploymentDiagnosticMetadata {
  lines: Array<{
    timestamp: string | null;
    stream: 'STDOUT' | 'STDERR';
    message: string;
  }>;
}

export interface ServiceLogLine {
  timestamp: string;
  stream: 'STDOUT' | 'STDERR';
  message: string;
}

export interface ServiceLogSnapshot {
  deploymentId: string;
  services: string[];
  serviceName: string;
  retrievedAt: string;
  lines: ServiceLogLine[];
  truncated: boolean;
}

export interface ServiceLogStreamReady {
  deploymentId: string;
  services: string[];
  serviceName: string;
  connectedAt: string;
}

export interface ServiceLogStreamLine extends ServiceLogLine {
  truncated: boolean;
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
