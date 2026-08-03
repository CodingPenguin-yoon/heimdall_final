import { Link } from 'react-router';

import { RegisterProjectForm } from '@/features/register-project/RegisterProjectForm';

export function ProjectCreatePage() {
  return (
    <div className="narrow-page page-stack">
      <header className="page-heading">
        <div>
          <div className="back-line">
            <Link to="/projects">Projects</Link>
            <span>/</span>
            <span>New project</span>
          </div>
          <h1>새 프로젝트 등록</h1>
          <p>저장소를 먼저 연결하고 다음 화면에서 배포 서비스를 구성합니다.</p>
        </div>
      </header>
      <RegisterProjectForm />
    </div>
  );
}
