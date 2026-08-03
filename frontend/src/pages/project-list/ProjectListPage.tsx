import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';

import { projectsQuery } from '@/entities/project/queries';
import { StatusBadge } from '@/entities/project/StatusBadge';
import { formatDate } from '@/shared/lib/format';
import { Icon } from '@/shared/ui/Icon';

export function ProjectListPage() {
  const projects = useQuery(projectsQuery());
  const items = projects.data?.items ?? [];
  const ready = items.filter((project) => project.status === 'READY').length;

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <span className="eyebrow">Overview</span>
          <h1>Preview projects</h1>
          <p>Public GitHub 저장소의 서비스와 배포 환경을 한곳에서 관리합니다.</p>
        </div>
        <Link to="/projects/new" className="button primary">
          <Icon name="plus" /> 프로젝트 등록
        </Link>
      </header>

      <section className="summary-grid" aria-label="프로젝트 요약">
        <article>
          <span>Total projects</span>
          <strong>{items.length}</strong>
          <small>등록된 Public 저장소</small>
        </article>
        <article>
          <span>Ready</span>
          <strong>{ready}</strong>
          <small>지금 배포 가능한 프로젝트</small>
        </article>
        <article>
          <span>Setup required</span>
          <strong>{items.length - ready}</strong>
          <small>서비스 설정이 필요한 초안</small>
        </article>
        <article className="accent-summary">
          <span>Runtime model</span>
          <strong className="summary-word">Isolated</strong>
          <small>프로젝트별 NGINX gateway</small>
        </article>
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">Projects</span>
            <h2>등록된 저장소</h2>
          </div>
          <span className="result-count">{items.length} repositories</span>
        </div>

        {projects.isLoading ? (
          <div className="empty-panel">프로젝트를 불러오는 중입니다.</div>
        ) : null}
        {projects.isError ? (
          <div className="empty-panel error-panel">
            API에 연결하지 못했습니다. FastAPI와 PostgreSQL 실행 상태를 확인해주세요.
          </div>
        ) : null}
        {!projects.isLoading && !projects.isError && items.length === 0 ? (
          <div className="empty-panel">
            <span className="empty-icon">
              <Icon name="grid" />
            </span>
            <h3>첫 프로젝트를 연결해보세요.</h3>
            <p>Public GitHub URL만 있으면 DRAFT 프로젝트를 만들 수 있습니다.</p>
            <Link to="/projects/new" className="button secondary">
              저장소 등록
            </Link>
          </div>
        ) : null}

        {items.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Repository</th>
                  <th>Branch</th>
                  <th>Status</th>
                  <th>Updated</th>
                  <th aria-label="열기" />
                </tr>
              </thead>
              <tbody>
                {items.map((project) => (
                  <tr key={project.id}>
                    <td>
                      <Link className="project-name" to={`/projects/${project.id}`}>
                        <span>{project.name.slice(0, 1).toUpperCase()}</span>
                        <div>
                          <strong>{project.name}</strong>
                          <small>Config v{project.configVersion}</small>
                        </div>
                      </Link>
                    </td>
                    <td>
                      <span className="repo-cell">
                        <Icon name="git" />
                        {project.repositoryUrl.replace('https://github.com/', '')}
                      </span>
                    </td>
                    <td>
                      <span className="branch-chip">
                        <Icon name="branch" /> {project.branch}
                      </span>
                    </td>
                    <td>
                      <StatusBadge status={project.status} />
                    </td>
                    <td className="muted-cell">{formatDate(project.updatedAt)}</td>
                    <td>
                      <Link className="row-arrow" to={`/projects/${project.id}`} aria-label="열기">
                        <Icon name="arrow" />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
