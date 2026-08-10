import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router';

import { deploymentActivityQuery } from '@/entities/deployment/queries';
import {
  deploymentStatusLabels,
  deploymentStatusTone,
  isDeploymentTerminal,
} from '@/entities/deployment/presentation';
import type { Deployment, DeploymentStatus } from '@/entities/deployment/types';
import { projectsQuery } from '@/entities/project/queries';
import type { Project } from '@/entities/project/types';
import { formatDate, shortSha } from '@/shared/lib/format';
import { Icon } from '@/shared/ui/Icon';

type StatusFilter = 'ALL' | 'ACTIVE' | 'SUCCEEDED' | 'FAILED';

const statusFilters: { value: StatusFilter; label: string }[] = [
  { value: 'ALL', label: '전체' },
  { value: 'ACTIVE', label: '진행 중' },
  { value: 'SUCCEEDED', label: '성공' },
  { value: 'FAILED', label: '실패' },
];

function matchesStatus(status: DeploymentStatus, filter: StatusFilter): boolean {
  if (filter === 'ALL') return true;
  if (filter === 'ACTIVE') return !isDeploymentTerminal(status);
  return status === filter;
}

function projectLabel(project: Project | undefined): string {
  return project?.name ?? '삭제된 프로젝트';
}

function sourceLabel(deployment: Deployment): string {
  return deployment.sourceType === 'MAIN_HEAD' ? 'main 최신' : '선택 commit';
}

export function DeploymentActivityPage() {
  const deployments = useQuery(deploymentActivityQuery());
  const projects = useQuery(projectsQuery());
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('ALL');
  const [projectFilter, setProjectFilter] = useState('ALL');

  const items = deployments.data?.items ?? [];
  const projectItems = projects.data?.items ?? [];
  const projectById = new Map(projectItems.map((project) => [project.id, project]));
  const filteredItems = items.filter(
    (item) =>
      matchesStatus(item.status, statusFilter) &&
      (projectFilter === 'ALL' || item.projectId === projectFilter),
  );
  const active = items.filter((item) => !isDeploymentTerminal(item.status)).length;
  const succeeded = items.filter((item) => item.status === 'SUCCEEDED').length;
  const failed = items.filter((item) => item.status === 'FAILED').length;
  const loading = deployments.isLoading || projects.isLoading;
  const error = deployments.isError || projects.isError;

  return (
    <div className="page-stack deployment-activity-page">
      <header className="page-heading">
        <div>
          <span className="eyebrow">Operations</span>
          <h1>배포 활동</h1>
          <p>모든 프로젝트의 최근 배포와 현재 진행 상태를 한곳에서 확인합니다.</p>
        </div>
        <span className="activity-live-note">
          <span /> {active > 0 ? `${active}개 배포 진행 중` : '현재 진행 중인 배포 없음'}
        </span>
      </header>

      <section className="summary-grid" aria-label="배포 활동 요약">
        <article>
          <span>Recent runs</span>
          <strong>{items.length}</strong>
          <small>최근 배포 최대 100건</small>
        </article>
        <article className="accent-summary">
          <span>In progress</span>
          <strong>{active}</strong>
          <small>자동으로 갱신되는 배포</small>
        </article>
        <article>
          <span>Succeeded</span>
          <strong>{succeeded}</strong>
          <small>정상 활성화된 generation</small>
        </article>
        <article>
          <span>Failed</span>
          <strong>{failed}</strong>
          <small>기존 Preview가 보존된 실패</small>
        </article>
      </section>

      <section className="panel">
        <div className="panel-heading activity-panel-heading">
          <div>
            <span className="eyebrow">Recent activity</span>
            <h2>최근 배포</h2>
          </div>
          <span className="result-count">{filteredItems.length} runs</span>
        </div>

        <div className="activity-filters" aria-label="배포 활동 필터">
          <div className="activity-filter-buttons" aria-label="상태 필터">
            {statusFilters.map((filter) => (
              <button
                key={filter.value}
                type="button"
                className={statusFilter === filter.value ? 'selected' : ''}
                aria-pressed={statusFilter === filter.value}
                onClick={() => setStatusFilter(filter.value)}
              >
                {filter.label}
              </button>
            ))}
          </div>
          <label className="activity-project-filter">
            <span>Project</span>
            <select
              aria-label="프로젝트 필터"
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
            >
              <option value="ALL">모든 프로젝트</option>
              {projectItems.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {loading ? <div className="empty-panel">배포 활동을 불러오는 중입니다.</div> : null}
        {error ? (
          <div className="empty-panel error-panel">
            배포 활동을 불러오지 못했습니다. Backend 연결 상태를 확인해주세요.
          </div>
        ) : null}
        {!loading && !error && items.length === 0 ? (
          <div className="empty-panel">
            <span className="empty-icon">
              <Icon name="activity" />
            </span>
            <h3>아직 배포 활동이 없습니다.</h3>
            <p>프로젝트에서 첫 배포를 요청하면 이곳에 진행 상태가 표시됩니다.</p>
            <Link to="/projects" className="button secondary">
              프로젝트로 이동
            </Link>
          </div>
        ) : null}
        {!loading && !error && items.length > 0 && filteredItems.length === 0 ? (
          <div className="empty-panel activity-filter-empty">
            <h3>조건에 맞는 배포가 없습니다.</h3>
            <p>다른 상태나 프로젝트를 선택해보세요.</p>
          </div>
        ) : null}

        {!loading && !error && filteredItems.length > 0 ? (
          <div className="table-wrap">
            <table className="activity-table">
              <colgroup>
                <col className="activity-col-project" />
                <col className="activity-col-commit" />
                <col className="activity-col-source" />
                <col className="activity-col-status" />
                <col className="activity-col-requested" />
                <col className="activity-col-finished" />
                <col className="activity-col-detail" />
              </colgroup>
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Commit</th>
                  <th>Source</th>
                  <th>Status</th>
                  <th>Requested</th>
                  <th>Finished</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.map((deployment) => {
                  const project = projectById.get(deployment.projectId);
                  const name = projectLabel(project);
                  const tone = deploymentStatusTone(deployment.status);
                  return (
                    <tr key={deployment.id}>
                      <td>
                        <Link
                          className="project-name"
                          to={`/projects/${deployment.projectId}`}
                          title={name}
                        >
                          <span>{name.slice(0, 1).toUpperCase()}</span>
                          <div>
                            <strong>{name}</strong>
                            <small>Config v{deployment.configVersion}</small>
                          </div>
                        </Link>
                      </td>
                      <td>
                        <div className="activity-commit">
                          <code title={deployment.resolvedCommitSha}>
                            {shortSha(deployment.resolvedCommitSha)}
                          </code>
                          <small title={deployment.id}>{deployment.id.slice(0, 8)}</small>
                        </div>
                      </td>
                      <td>
                        <span className="activity-source">{sourceLabel(deployment)}</span>
                      </td>
                      <td>
                        <div className="activity-status-cell">
                          <span className={`deployment-status deployment-status-${tone}`}>
                            {deploymentStatusLabels[deployment.status]}
                          </span>
                          {deployment.failureCode ? (
                            <small title={deployment.failureCode}>{deployment.failureCode}</small>
                          ) : null}
                        </div>
                      </td>
                      <td className="muted-cell">{formatDate(deployment.createdAt)}</td>
                      <td className="muted-cell">
                        {deployment.terminalAt ? formatDate(deployment.terminalAt) : '진행 중'}
                      </td>
                      <td>
                        <Link
                          className="activity-detail-link"
                          to={`/deployments/${deployment.id}`}
                          aria-label={`${shortSha(deployment.resolvedCommitSha)} 배포 상세 보기`}
                        >
                          상세 보기
                          <Icon name="arrow" />
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
