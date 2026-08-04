import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { createDeployment } from '@/entities/deployment/api';
import { deploymentKeys } from '@/entities/deployment/queries';
import { commitsQuery } from '@/entities/project/queries';
import type { Project } from '@/entities/project/types';
import { runtimeKeys } from '@/entities/runtime/queries';
import { ApiError } from '@/shared/api/client';
import { formatDate, shortSha } from '@/shared/lib/format';
import { Icon } from '@/shared/ui/Icon';

export function DeployPanel({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const commits = useQuery(commitsQuery(project.id));
  const [selectedSha, setSelectedSha] = useState<string>('HEAD');

  const mutation = useMutation({
    mutationFn: () =>
      createDeployment(
        project.id,
        selectedSha === 'HEAD'
          ? { type: 'MAIN_HEAD' }
          : { type: 'MAIN_COMMIT', commitSha: selectedSha },
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: deploymentKeys.project(project.id) }),
        queryClient.invalidateQueries({ queryKey: runtimeKeys.project(project.id) }),
      ]);
    },
  });

  const message = mutation.error instanceof ApiError ? mutation.error.message : null;

  return (
    <section className="panel deploy-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Deploy source</span>
          <h2>main에서 commit 선택</h2>
        </div>
        <span className="branch-chip">
          <Icon name="branch" /> main
        </span>
      </div>

      <label className="commit-select">
        배포할 commit
        <select value={selectedSha} onChange={(event) => setSelectedSha(event.target.value)}>
          <option value="HEAD">main 최신 commit</option>
          {commits.data?.items.map((commit) => (
            <option key={commit.sha} value={commit.sha}>
              {shortSha(commit.sha)} · {commit.subject}
            </option>
          ))}
        </select>
      </label>

      {selectedSha !== 'HEAD' ? (
        <div className="selected-commit">
          {commits.data?.items
            .filter((commit) => commit.sha === selectedSha)
            .map((commit) => (
              <div key={commit.sha}>
                <code>{shortSha(commit.sha)}</code>
                <strong>{commit.subject}</strong>
                <span>
                  {commit.authorName} · {formatDate(commit.committedAt)}
                </span>
              </div>
            ))}
        </div>
      ) : null}

      {message ? <div className="inline-error">{message}</div> : null}

      <button
        className="button primary wide"
        disabled={project.status !== 'READY' || mutation.isPending || commits.isLoading}
        onClick={() => mutation.mutate()}
      >
        {mutation.isPending ? '배포 요청 중…' : '새 preview 배포'}
        <Icon name="arrow" />
      </button>
      <p className="fine-print">
        선택한 source를 다시 build하며 현재 정상 preview는 성공 전까지 유지합니다.
      </p>
    </section>
  );
}
