import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { provisionProjectDatabase } from '@/entities/database/api';
import { databaseKeys, projectDatabaseQuery } from '@/entities/database/queries';
import { ApiError } from '@/shared/api/client';
import { Icon } from '@/shared/ui/Icon';

export function ProjectDatabasePanel({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const database = useQuery(projectDatabaseQuery(projectId, true));
  const provision = useMutation({
    mutationFn: () => provisionProjectDatabase(projectId),
    onSuccess: (result) => {
      queryClient.setQueryData(databaseKeys.detail(projectId), result);
    },
  });

  const data = database.data;
  const error =
    provision.error instanceof ApiError
      ? provision.error.message
      : database.error instanceof ApiError
        ? database.error.message
        : null;

  return (
    <section className="panel database-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Managed PostgreSQL</span>
          <h2>Project database</h2>
        </div>
        <span className={`database-state database-${(data?.status ?? 'loading').toLowerCase()}`}>
          {data?.status ?? 'LOADING'}
        </span>
      </div>

      {data?.status === 'NOT_CREATED' ? (
        <div className="database-empty">
          <p>전용 database와 최소 권한 role을 Managed PostgreSQL에 생성합니다.</p>
          <button
            className="button primary"
            onClick={() => provision.mutate()}
            disabled={provision.isPending}
          >
            <Icon name="plus" /> {provision.isPending ? '생성 중…' : 'Database 생성'}
          </button>
        </div>
      ) : data ? (
        <dl className="database-details">
          <div>
            <dt>Host</dt>
            <dd>
              {data.host ?? '—'}:{data.port ?? '—'}
            </dd>
          </div>
          <div>
            <dt>Database</dt>
            <dd>{data.databaseName ?? '—'}</dd>
          </div>
          <div>
            <dt>Username</dt>
            <dd>{data.username ?? '—'}</dd>
          </div>
          <div>
            <dt>Schema</dt>
            <dd>{data.schemaName ?? '—'}</dd>
          </div>
          <div className="database-services">
            <dt>Connected services</dt>
            <dd>{data.connectedServices.join(', ')}</dd>
          </div>
        </dl>
      ) : (
        <div className="database-empty">
          <p>Database 상태를 불러오는 중입니다.</p>
        </div>
      )}

      {data?.status === 'FAILED' ? (
        <button className="button secondary" onClick={() => provision.mutate()}>
          다시 시도
        </button>
      ) : null}
      {error ? <div className="inline-error">{error}</div> : null}
      <div className="database-contract">
        <strong>Application contract</strong>
        <code>DATABASE_HOST · DATABASE_NAME · DATABASE_USER · DATABASE_PASSWORD_FILE</code>
      </div>
    </section>
  );
}
