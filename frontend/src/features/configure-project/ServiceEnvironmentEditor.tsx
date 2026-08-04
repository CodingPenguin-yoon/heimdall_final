import type { EnvironmentVariable, ServiceConfig } from '@/entities/project/types';

interface ServiceEnvironmentEditorProps {
  service: ServiceConfig;
  serviceIndex: number;
  onChange: (patch: Partial<ServiceConfig>) => void;
}

export function ServiceEnvironmentEditor({
  service,
  serviceIndex,
  onChange,
}: ServiceEnvironmentEditorProps) {
  function updateVariable(index: number, patch: Partial<EnvironmentVariable>) {
    onChange({
      environment: service.environment.map((variable, variableIndex) =>
        variableIndex === index ? { ...variable, ...patch } : variable,
      ),
    });
  }

  return (
    <>
      <div className="database-access-toggle">
        <input
          id={`database-access-${serviceIndex}`}
          aria-label="Managed PostgreSQL 연결"
          type="checkbox"
          checked={service.projectDatabaseAccess}
          onChange={(event) => onChange({ projectDatabaseAccess: event.target.checked })}
        />
        <label htmlFor={`database-access-${serviceIndex}`}>
          <strong>Managed PostgreSQL 연결</strong>
          <small>Heimdall이 DATABASE_* 값과 password secret을 자동 주입합니다.</small>
        </label>
      </div>

      <div className="environment-editor">
        <div className="environment-heading">
          <div>
            <strong>Environment variables</strong>
            <span>
              DATABASE_*와 HEIMDALL_*는 예약 이름이며 Secret 변수에는 파일 경로가 전달됩니다.
            </span>
          </div>
          <button
            type="button"
            className="text-button"
            onClick={() =>
              onChange({
                environment: [...service.environment, { name: '', kind: 'PLAIN', value: '' }],
              })
            }
          >
            + 변수 추가
          </button>
        </div>
        {service.environment.length ? (
          <div className="environment-list">
            {service.environment.map((variable, variableIndex) => (
              <div className="environment-row" key={`${variableIndex}-${variable.name}`}>
                <input
                  aria-label={`${service.name} environment ${variableIndex + 1} name`}
                  value={variable.name}
                  placeholder="APP_ENV"
                  onChange={(event) => updateVariable(variableIndex, { name: event.target.value })}
                />
                <select
                  aria-label={`${service.name} environment ${variableIndex + 1} kind`}
                  value={variable.kind}
                  onChange={(event) => {
                    const kind = event.target.value as EnvironmentVariable['kind'];
                    updateVariable(variableIndex, {
                      kind,
                      value: kind === 'PLAIN' ? (variable.value ?? '') : undefined,
                    });
                  }}
                >
                  <option value="PLAIN">일반 값</option>
                  <option value="SECRET">Secret</option>
                </select>
                <input
                  aria-label={`${service.name} environment ${variableIndex + 1} value`}
                  type={variable.kind === 'SECRET' ? 'password' : 'text'}
                  value={variable.value ?? ''}
                  placeholder={
                    variable.kind === 'SECRET' && variable.configured
                      ? '기존 Secret 유지'
                      : '값 입력'
                  }
                  onChange={(event) =>
                    updateVariable(variableIndex, { value: event.target.value || undefined })
                  }
                />
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`${variable.name || '환경변수'} 제거`}
                  onClick={() =>
                    onChange({
                      environment: service.environment.filter(
                        (_, environmentIndex) => environmentIndex !== variableIndex,
                      ),
                    })
                  }
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="environment-empty">사용자 환경변수가 없습니다.</p>
        )}
      </div>
    </>
  );
}
