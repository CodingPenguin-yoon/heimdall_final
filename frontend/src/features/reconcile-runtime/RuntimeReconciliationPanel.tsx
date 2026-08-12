import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { requestRuntimeReconciliation } from '@/entities/deployment/api';
import { deploymentKeys, runtimeReconciliationQuery } from '@/entities/deployment/queries';
import type { Deployment, RuntimeReconciliationAction } from '@/entities/deployment/types';
import { runtimeKeys } from '@/entities/runtime/queries';
import { ApiError } from '@/shared/api/client';
import { formatDate } from '@/shared/lib/format';

const stateLabels = {
  RETAINED: '안전 보존 중',
  PENDING: 'Worker 대기 중',
  CLAIMED: 'Worker 확인 중',
  RESOLVED: '처리 완료',
  BLOCKED: '확인 필요',
} as const;

export function RuntimeReconciliationPanel({
  deployment,
  projectId,
}: {
  deployment: Deployment;
  projectId: string;
}) {
  const queryClient = useQueryClient();
  const reconciliation = useQuery(runtimeReconciliationQuery(deployment.id));
  const [confirmation, setConfirmation] = useState('');
  const mutation = useMutation({
    mutationFn: ({
      action,
      confirmation,
    }: {
      action: RuntimeReconciliationAction;
      confirmation?: string;
    }) => requestRuntimeReconciliation(deployment.id, action, confirmation),
    onSuccess: async () => {
      setConfirmation('');
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: deploymentKeys.reconciliation(deployment.id),
        }),
        queryClient.invalidateQueries({ queryKey: deploymentKeys.project(projectId) }),
        queryClient.invalidateQueries({ queryKey: deploymentKeys.events(deployment.id) }),
        queryClient.invalidateQueries({ queryKey: runtimeKeys.project(projectId) }),
      ]);
    },
  });
  const message =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : reconciliation.error instanceof ApiError
        ? reconciliation.error.message
        : null;
  const item = reconciliation.data;
  const busy = mutation.isPending || item?.state === 'PENDING' || item?.state === 'CLAIMED';

  return (
    <div className="runtime-reconciliation">
      <div className="runtime-reconciliation-heading">
        <div>
          <strong>보존된 Runtime 자원</strong>
          <p>
            {item
              ? `${stateLabels[item.state]} · ${item.resultCode ?? '자동 정리 전 안전하게 보존합니다.'}`
              : '복구 상태를 불러오는 중입니다.'}
          </p>
        </div>
        {item ? (
          <span className={`reconciliation-state state-${item.state.toLowerCase()}`}>
            {stateLabels[item.state]}
          </span>
        ) : null}
      </div>

      {item?.state === 'RETAINED' ? (
        <small>자동 안전 확인 예정: {formatDate(item.availableAt)}</small>
      ) : null}
      {item?.completedAt ? <small>최근 처리: {formatDate(item.completedAt)}</small> : null}
      {message ? <div className="inline-error">{message}</div> : null}

      {item?.state !== 'RESOLVED' ? (
        <div className="reconciliation-actions">
          <button
            className="button secondary"
            disabled={busy}
            onClick={() => mutation.mutate({ action: 'RECONCILE' })}
          >
            지금 안전 확인
          </button>
          <label>
            강제 정리 확인
            <input
              aria-label="강제 정리 확인 Deployment ID"
              value={confirmation}
              placeholder={deployment.id}
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </label>
          <button
            className="button danger"
            disabled={busy || confirmation !== deployment.id}
            onClick={() => mutation.mutate({ action: 'FORCE_CLEANUP', confirmation })}
          >
            보존 자원 강제 정리
          </button>
        </div>
      ) : null}
      {item?.state !== 'RESOLVED' ? (
        <p className="reconciliation-warning">
          강제 정리는 전체 Deployment ID가 일치할 때만 요청되며 active runtime이면 Worker가
          거부합니다.
        </p>
      ) : null}
    </div>
  );
}
