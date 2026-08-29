import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';

import { deploymentKeys } from '@/entities/deployment/queries';
import { deleteProject, retryProjectDeletion } from '@/entities/project/api';
import { projectDeletionQuery, projectKeys } from '@/entities/project/queries';
import type { Project, ProjectDeletionRequest } from '@/entities/project/types';
import { ApiError } from '@/shared/api/client';

export function ProjectDeletionPanel({ project }: { project: Project }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [confirmation, setConfirmation] = useState('');
  const [managedDatabaseConfirmation, setManagedDatabaseConfirmation] = useState('');

  const requestDeletion = useMutation({
    mutationFn: (payload: ProjectDeletionRequest) => deleteProject(project.id, payload),
    onSuccess: (result) => {
      queryClient.setQueryData(projectKeys.deletion(project.id), result);
      queryClient.setQueryData(projectKeys.detail(project.id), { ...project, status: 'DELETING' });
      queryClient.setQueryData<{ items: Project[] }>(projectKeys.all, (current) =>
        current
          ? {
              items: current.items.map((item) =>
                item.id === project.id ? { ...item, status: 'DELETING' } : item,
              ),
            }
          : current,
      );
    },
  });
  const deletionActive = project.status === 'DELETING' || requestDeletion.data !== undefined;
  const deletion = useQuery(projectDeletionQuery(project.id, deletionActive));
  const retryDeletion = useMutation({
    mutationFn: (payload: ProjectDeletionRequest) => retryProjectDeletion(project.id, payload),
    onSuccess: (result) => queryClient.setQueryData(projectKeys.deletion(project.id), result),
  });

  useEffect(() => {
    if (
      !(deletion.error instanceof ApiError) ||
      deletion.error.status !== 404 ||
      deletion.error.code !== 'PROJECT_NOT_FOUND'
    )
      return;
    const deletedDeploymentIds = new Set(
      (
        queryClient.getQueryData<{ items: Array<{ id: string }> }>(
          deploymentKeys.project(project.id),
        )?.items ?? []
      ).map((item) => item.id),
    );
    for (const query of queryClient.getQueryCache().findAll({ queryKey: ['deployments'] })) {
      const data = query.state.data as { projectId?: string } | undefined;
      if (data?.projectId === project.id && typeof query.queryKey[1] === 'string') {
        deletedDeploymentIds.add(query.queryKey[1]);
      }
    }
    queryClient.removeQueries({
      predicate: (query) => {
        const [scope, id] = query.queryKey;
        return (
          (scope === 'projects' && id === project.id) ||
          (scope === 'deployments' && typeof id === 'string' && deletedDeploymentIds.has(id))
        );
      },
    });
    queryClient.setQueryData<{ items: Array<{ projectId: string }> }>(
      deploymentKeys.activity,
      (current) =>
        current
          ? { items: current.items.filter((item) => item.projectId !== project.id) }
          : current,
    );
    queryClient.setQueryData<{ items: Project[] }>(projectKeys.all, (current) =>
      current ? { items: current.items.filter((item) => item.id !== project.id) } : current,
    );
    navigate('/projects', { replace: true });
  }, [deletion.error, navigate, project.id, queryClient]);

  const deletionData = deletion.data ?? requestDeletion.data;
  const deletesManagedDatabase = deletionData?.deleteManagedDatabase ?? project.hasManagedDatabase;
  const requiredManagedConfirmation = `DELETE ${project.id} APPLICATION DATA`;
  const payload: ProjectDeletionRequest = {
    confirmation,
    deleteManagedDatabase: deletesManagedDatabase,
    managedDatabaseConfirmation: deletesManagedDatabase ? managedDatabaseConfirmation : null,
  };
  const confirmed =
    confirmation === project.id &&
    (!deletesManagedDatabase || managedDatabaseConfirmation === requiredManagedConfirmation);
  const failed = deletionData?.state === 'FAILED';
  const busy = requestDeletion.isPending || retryDeletion.isPending;
  const error = requestDeletion.error ?? retryDeletion.error ?? deletion.error;
  const message =
    error instanceof ApiError && !(error.status === 404 && error.code === 'PROJECT_NOT_FOUND')
      ? error.message
      : null;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!confirmed || busy) return;
    if (failed) retryDeletion.mutate(payload);
    else requestDeletion.mutate(payload);
  }

  return (
    <section className="panel danger-zone" aria-label="Danger zone">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Danger zone</span>
          <h2>프로젝트 영구 삭제</h2>
        </div>
        {deletionData ? (
          <span className={`deletion-state deletion-${deletionData.state.toLowerCase()}`}>
            {deletionData.state}
          </span>
        ) : null}
      </div>

      <p>
        Public hostname, Preview, Gateway, 배포 runtime과 이력, project secret을 제거합니다. 완료된
        삭제는 되돌릴 수 없습니다.
      </p>

      {deletionData ? (
        <dl className="deletion-progress">
          <div>
            <dt>현재 단계</dt>
            <dd>{deletionData.phase.replaceAll('_', ' ')}</dd>
          </div>
          <div>
            <dt>시도 횟수</dt>
            <dd>{deletionData.attempts}</dd>
          </div>
          {deletionData.lastErrorCode ? (
            <div>
              <dt>마지막 오류</dt>
              <dd>{deletionData.lastErrorCode}</dd>
            </div>
          ) : null}
          {failed ? (
            <div>
              <dt>재시도</dt>
              <dd>{deletionData.lastErrorRetryable ? '재시도 가능' : '수동 확인 필요'}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      {!deletionData || failed ? (
        <form className="deletion-confirmation" onSubmit={submit}>
          <label>
            삭제 확인 Project UUID
            <input
              aria-label="삭제 확인 Project UUID"
              value={confirmation}
              placeholder={project.id}
              autoComplete="off"
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </label>
          {deletesManagedDatabase ? (
            <label>
              Managed DB application data 삭제 확인
              <input
                aria-label="Managed DB application data 삭제 확인"
                value={managedDatabaseConfirmation}
                placeholder={requiredManagedConfirmation}
                autoComplete="off"
                onChange={(event) => setManagedDatabaseConfirmation(event.target.value)}
              />
              <small>
                전용 PostgreSQL database·role과 application data도 영구 삭제됩니다. 위 문구를 정확히
                입력하세요.
              </small>
            </label>
          ) : null}
          {message ? <div className="inline-error">{message}</div> : null}
          <button className="button danger" disabled={!confirmed || busy}>
            {failed
              ? retryDeletion.isPending
                ? '재시도 요청 중…'
                : '삭제 다시 시도'
              : requestDeletion.isPending
                ? '삭제 요청 중…'
                : deletesManagedDatabase
                  ? '프로젝트와 application data 영구 삭제'
                  : '프로젝트 영구 삭제'}
          </button>
        </form>
      ) : (
        <p className="fine-print">Worker가 안전 순서대로 외부 resource를 제거하고 있습니다.</p>
      )}
    </section>
  );
}
