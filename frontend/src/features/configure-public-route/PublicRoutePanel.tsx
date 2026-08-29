import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { disablePublicRoute, savePublicRoute } from '@/entities/public-route/api';
import { publicRouteKeys, publicRouteQuery } from '@/entities/public-route/queries';
import type { PublicRoute, PublicRouteStatus } from '@/entities/public-route/types';
import { ApiError } from '@/shared/api/client';
import { Icon } from '@/shared/ui/Icon';

const statusLabels: Record<PublicRouteStatus, string> = {
  PENDING: '적용 대기',
  APPLYING: '적용 중',
  ACTIVE: '공개 중',
  INACTIVE: '비활성',
  FAILED: '적용 실패',
  UNCERTAIN: '상태 확인 필요',
};

const statusDescriptions: Record<PublicRouteStatus, string> = {
  PENDING: 'Routing Worker가 project gateway와 Edge route를 확인할 때까지 기다립니다.',
  APPLYING: '검증된 Edge 설정을 적용하고 hostname 응답을 확인하고 있습니다.',
  ACTIVE: '인증 없는 공개 HTTP 주소로 현재 project gateway에 연결됩니다.',
  INACTIVE: 'Edge 설정에서 제거되어 이 hostname으로 project에 접근할 수 없습니다.',
  FAILED: '기존 Edge 설정은 유지되었습니다. 같은 hostname으로 다시 요청할 수 있습니다.',
  UNCERTAIN: '기존 Edge 설정을 보존한 채 실제 적용 상태를 다시 확인해야 합니다.',
};

function errorMessage(error: unknown): string | null {
  if (error instanceof ApiError) return error.message;
  return error ? 'Public hostname 요청을 처리하지 못했습니다.' : null;
}

export function PublicRoutePanel({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const publicRoute = useQuery(publicRouteQuery(projectId));
  const [draftSubdomain, setDraftSubdomain] = useState<string | null>(null);

  function storeRoute(result: PublicRoute) {
    queryClient.setQueryData(publicRouteKeys.detail(projectId), result);
    setDraftSubdomain(result.subdomain);
  }

  const save = useMutation({
    mutationFn: (subdomain: string) => savePublicRoute(projectId, subdomain),
    onSuccess: storeRoute,
  });
  const disable = useMutation({
    mutationFn: () => disablePublicRoute(projectId),
    onSuccess: storeRoute,
  });

  const route = publicRoute.data;
  const subdomain = draftSubdomain ?? route?.subdomain ?? '';
  const busy = save.isPending || disable.isPending;
  const message = errorMessage(save.error ?? disable.error ?? publicRoute.error);
  const retryable = route?.status === 'FAILED' || route?.status === 'UNCERTAIN';
  const desiredUrl = route ? `http://${route.hostname}` : null;
  const appliedUrl = route?.appliedHostname ? `http://${route.appliedHostname}` : null;

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = subdomain.trim();
    if (!value) return;
    disable.reset();
    save.mutate(value);
  }

  function retry() {
    if (!route) return;
    if (route.desiredState === 'DISABLED') {
      save.reset();
      disable.mutate();
      return;
    }
    disable.reset();
    save.mutate(route.subdomain);
  }

  function requestDisable() {
    save.reset();
    disable.mutate();
  }

  return (
    <section className="panel public-route-panel" aria-label="Public hostname">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Public hostname</span>
          <h2>공개 접속 주소</h2>
        </div>
        <span
          className={`public-route-state ${
            route ? `public-route-${route.status.toLowerCase()}` : 'public-route-unconfigured'
          }`}
        >
          {route ? statusLabels[route.status] : publicRoute.isLoading ? '확인 중' : '미설정'}
        </span>
      </div>

      {route && desiredUrl ? (
        <div className="public-route-address">
          <div>
            <strong>{appliedUrl ? '현재 적용 HTTP URL' : '요청 HTTP URL'}</strong>
            <span>{statusDescriptions[route.status]}</span>
            {appliedUrl !== null && appliedUrl !== desiredUrl ? (
              <span>
                요청 URL: <code>{desiredUrl}</code>
              </span>
            ) : null}
          </div>
          {appliedUrl ? (
            <a href={appliedUrl} target="_blank" rel="noreferrer">
              {appliedUrl}
            </a>
          ) : (
            <code>{desiredUrl}</code>
          )}
        </div>
      ) : !publicRoute.isLoading && !publicRoute.isError ? (
        <p className="public-route-empty">
          배포용 base domain 아래의 subdomain label을 예약합니다. 첫 성공 배포 전에도 예약할 수
          있습니다.
        </p>
      ) : null}

      {route ? (
        <dl className="public-route-metadata">
          <div>
            <dt>Desired state</dt>
            <dd>{route.desiredState}</dd>
          </div>
          <div>
            <dt>Revision</dt>
            <dd>
              {route.appliedRevision ?? '—'} / {route.desiredRevision}
            </dd>
          </div>
          {route.lastErrorCode ? (
            <div className="public-route-error-code">
              <dt>Last error</dt>
              <dd>{route.lastErrorCode}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      {!publicRoute.isLoading && !publicRoute.isError ? (
        <form className="public-route-form" onSubmit={submit}>
          <label>
            Subdomain label
            <input
              aria-label="Public hostname subdomain"
              value={subdomain}
              required
              maxLength={63}
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              placeholder="student-a"
              onChange={(event) => setDraftSubdomain(event.target.value)}
            />
            <small>hostname, scheme, port와 upstream은 Backend가 결정합니다.</small>
          </label>
          <div className="public-route-actions">
            {route?.desiredState === 'ENABLED' ? (
              <button
                type="button"
                className="button secondary"
                disabled={busy}
                onClick={requestDisable}
              >
                비활성화
              </button>
            ) : null}
            {retryable ? (
              <button type="button" className="button secondary" disabled={busy} onClick={retry}>
                <Icon name="refresh" /> 다시 시도
              </button>
            ) : null}
            <button className="button primary" disabled={busy || !subdomain.trim()}>
              {save.isPending
                ? '요청 중…'
                : route?.desiredState === 'DISABLED'
                  ? '다시 활성화'
                  : route
                    ? 'Hostname 변경'
                    : 'Hostname 예약'}
            </button>
          </div>
        </form>
      ) : null}

      {publicRoute.isLoading ? (
        <div className="public-route-loading">Public hostname 상태를 불러오는 중입니다.</div>
      ) : null}
      {message ? <div className="inline-error">{message}</div> : null}
    </section>
  );
}
