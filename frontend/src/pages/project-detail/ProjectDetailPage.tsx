import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';

import { deploymentEventsQuery, deploymentsQuery } from '@/entities/deployment/queries';
import { projectQuery } from '@/entities/project/queries';
import { StatusBadge } from '@/entities/project/StatusBadge';
import { PreviewAccessPanel } from '@/entities/runtime/PreviewAccessPanel';
import { projectRuntimeQuery } from '@/entities/runtime/queries';
import { DeployPanel } from '@/features/deploy-project/DeployPanel';
import { ProjectDatabasePanel } from '@/features/provision-database/ProjectDatabasePanel';
import { formatDate, shortSha } from '@/shared/lib/format';
import { Icon } from '@/shared/ui/Icon';

export function ProjectDetailPage() {
  const { projectId = '' } = useParams();
  const project = useQuery(projectQuery(projectId));
  const deployments = useQuery(deploymentsQuery(projectId));
  const runtime = useQuery(projectRuntimeQuery(projectId));
  const latestDeployment = deployments.data?.items[0];
  const latestDeploymentActive =
    latestDeployment !== undefined && !['SUCCEEDED', 'FAILED'].includes(latestDeployment.status);
  const events = useQuery(deploymentEventsQuery(latestDeployment?.id, latestDeploymentActive));

  if (project.isLoading) return <div className="loading-page">프로젝트를 불러오는 중입니다.</div>;
  if (!project.data) return <div className="loading-page">프로젝트를 찾을 수 없습니다.</div>;

  const config = project.data.deploymentConfig;
  const usesProjectDatabase =
    config?.services.some((service) => service.projectDatabaseAccess) ?? false;

  return (
    <div className="page-stack">
      <header className="project-hero">
        <div className="project-identity">
          <span>{project.data.name.slice(0, 1).toUpperCase()}</span>
          <div>
            <div className="project-title-line">
              <h1>{project.data.name}</h1>
              <StatusBadge status={project.data.status} />
            </div>
            <a href={project.data.repositoryUrl} target="_blank" rel="noreferrer">
              <Icon name="git" /> {project.data.repositoryUrl.replace('https://github.com/', '')}
            </a>
          </div>
        </div>
        <div className="hero-actions">
          <Link to={`/projects/${projectId}/settings`} className="button secondary">
            <Icon name="settings" /> 프로젝트 설정
          </Link>
        </div>
      </header>

      <PreviewAccessPanel runtime={runtime.data} />

      {project.data.status === 'DRAFT' ? (
        <section className="setup-banner">
          <div>
            <span className="step-index">!</span>
            <div>
              <strong>배포 설정이 필요합니다.</strong>
              <p>서비스와 root route를 저장하면 프로젝트가 READY로 전환됩니다.</p>
            </div>
          </div>
          <Link to={`/projects/${projectId}/settings`} className="button primary">
            설정 시작 <Icon name="arrow" />
          </Link>
        </section>
      ) : null}

      <section className="detail-grid">
        <div className="page-stack">
          <section className="panel">
            <div className="panel-heading">
              <div>
                <span className="eyebrow">Runtime topology</span>
                <h2>Services & routes</h2>
              </div>
              <span className="result-count">Config v{project.data.configVersion}</span>
            </div>
            {config ? (
              <div className="topology-list">
                {config.services.map((service) => (
                  <article key={service.name}>
                    <div className="service-avatar">{service.name.slice(0, 2).toUpperCase()}</div>
                    <div>
                      <strong>{service.name}</strong>
                      <span>
                        {service.build.context}/{service.build.dockerfile}
                      </span>
                    </div>
                    <code>:{service.internalPort}</code>
                    <span className="health-pill">{service.healthPath}</span>
                    {service.projectDatabaseAccess ? (
                      <span className="database-pill">PostgreSQL</span>
                    ) : null}
                  </article>
                ))}
                <div className="route-map">
                  {config.routes.map((route) => (
                    <span key={route.path}>
                      <code>{route.path}</code>
                      <b>→</b>
                      {route.service}
                    </span>
                  ))}
                </div>
              </div>
            ) : (
              <div className="empty-panel compact-empty">아직 저장된 서비스 설정이 없습니다.</div>
            )}
          </section>

          {usesProjectDatabase ? <ProjectDatabasePanel projectId={projectId} /> : null}

          <section className="panel">
            <div className="panel-heading">
              <div>
                <span className="eyebrow">History</span>
                <h2>최근 배포</h2>
              </div>
              <span className="result-count">{deployments.data?.items.length ?? 0} runs</span>
            </div>
            {deployments.data?.items.length ? (
              <div className="deployment-list">
                {deployments.data.items.map((deployment) => (
                  <article key={deployment.id}>
                    <span
                      className={`deployment-dot deployment-${deployment.status.toLowerCase()}`}
                    />
                    <div>
                      <strong>{deployment.status.replaceAll('_', ' ')}</strong>
                      <span>{formatDate(deployment.createdAt)}</span>
                      {deployment.failureCode ? (
                        <small className="deployment-failure">
                          {deployment.failureStage} · {deployment.failureCode}
                        </small>
                      ) : null}
                    </div>
                    <code>{shortSha(deployment.resolvedCommitSha)}</code>
                    <small>Config v{deployment.configVersion}</small>
                  </article>
                ))}
              </div>
            ) : (
              <div className="empty-panel compact-empty">아직 배포 요청이 없습니다.</div>
            )}
            {latestDeployment && events.data?.items.length ? (
              <div className="deployment-events">
                <strong>Latest activity</strong>
                {events.data.items.slice(-6).map((event) => (
                  <div key={event.id}>
                    <span>{event.stage.replaceAll('_', ' ')}</span>
                    <p>{event.message}</p>
                    <time>{formatDate(event.createdAt)}</time>
                  </div>
                ))}
              </div>
            ) : null}
          </section>
        </div>

        <DeployPanel project={project.data} />
      </section>
    </div>
  );
}
