import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';

import { projectQuery } from '@/entities/project/queries';
import { ProjectSettingsForm } from '@/features/configure-project/ProjectSettingsForm';

export function ProjectSettingsPage() {
  const { projectId = '' } = useParams();
  const project = useQuery(projectQuery(projectId));

  if (project.isLoading)
    return <div className="loading-page">프로젝트 설정을 불러오는 중입니다.</div>;
  if (!project.data) return <div className="loading-page">프로젝트를 찾을 수 없습니다.</div>;

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <div className="back-line">
            <Link to={`/projects/${projectId}`}>{project.data.name}</Link>
            <span>/</span>
            <span>Settings</span>
          </div>
          <h1>배포 설정</h1>
          <p>서비스 build, 환경변수, Managed PostgreSQL, NGINX route를 함께 관리합니다.</p>
        </div>
        <span className="config-version">Config version {project.data.configVersion}</span>
      </header>
      <ProjectSettingsForm project={project.data} />
    </div>
  );
}
