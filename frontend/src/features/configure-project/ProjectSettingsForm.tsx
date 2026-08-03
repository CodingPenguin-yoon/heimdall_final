import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';

import { saveProjectSettings } from '@/entities/project/api';
import { projectKeys } from '@/entities/project/queries';
import type {
  DeploymentConfig,
  Project,
  RouteConfig,
  ServiceConfig,
} from '@/entities/project/types';
import { ApiError } from '@/shared/api/client';
import { Icon } from '@/shared/ui/Icon';

import { ServiceEnvironmentEditor } from './ServiceEnvironmentEditor';

const emptyService = (index: number): ServiceConfig => ({
  name: index === 0 ? 'web' : `service-${index + 1}`,
  build: { context: index === 0 ? '.' : `service-${index + 1}`, dockerfile: 'Dockerfile' },
  internalPort: index === 0 ? 3000 : 8000,
  healthPath: '/health',
  environment: [],
  projectDatabaseAccess: false,
});

function initialConfig(project: Project): DeploymentConfig {
  if (!project.deploymentConfig) {
    return {
      services: [emptyService(0)],
      routes: [{ path: '/', service: 'web' }],
    };
  }
  return {
    ...project.deploymentConfig,
    services: project.deploymentConfig.services.map((service) => ({
      ...service,
      environment: service.environment ?? [],
      projectDatabaseAccess: service.projectDatabaseAccess ?? false,
    })),
  };
}

export function ProjectSettingsForm({ project }: { project: Project }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [config, setConfig] = useState(() => initialConfig(project));

  const mutation = useMutation({
    mutationFn: (payload: DeploymentConfig) =>
      saveProjectSettings(project.id, { ...payload, expectedVersion: project.configVersion }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: projectKeys.detail(project.id) });
      await queryClient.invalidateQueries({ queryKey: projectKeys.all });
      navigate(`/projects/${project.id}`);
    },
  });

  function updateService(index: number, patch: Partial<ServiceConfig>) {
    setConfig((current) => ({
      ...current,
      services: current.services.map((service, serviceIndex) =>
        serviceIndex === index ? { ...service, ...patch } : service,
      ),
    }));
  }

  function updateBuild(index: number, patch: Partial<ServiceConfig['build']>) {
    setConfig((current) => ({
      ...current,
      services: current.services.map((service, serviceIndex) =>
        serviceIndex === index ? { ...service, build: { ...service.build, ...patch } } : service,
      ),
    }));
  }

  function updateRoute(index: number, patch: Partial<RouteConfig>) {
    setConfig((current) => ({
      ...current,
      routes: current.routes.map((route, routeIndex) =>
        routeIndex === index ? { ...route, ...patch } : route,
      ),
    }));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate(config);
  }

  const message = mutation.error instanceof ApiError ? mutation.error.message : null;

  return (
    <form className="settings-layout" onSubmit={submit}>
      <section className="form-card">
        <div className="form-section-heading">
          <span className="step-index">02</span>
          <div>
            <h2>서비스 구성</h2>
            <p>같은 commit에서 함께 build하고 하나의 generation network에서 실행합니다.</p>
          </div>
        </div>

        <div className="service-stack">
          {config.services.map((service, index) => (
            <fieldset className="service-editor" key={index}>
              <legend>Service {String(index + 1).padStart(2, '0')}</legend>
              {config.services.length > 1 ? (
                <button
                  type="button"
                  className="text-button danger-text remove-service"
                  onClick={() => {
                    setConfig((current) => ({
                      ...current,
                      services: current.services.filter((_, itemIndex) => itemIndex !== index),
                      routes: current.routes.filter((route) => route.service !== service.name),
                    }));
                  }}
                >
                  제거
                </button>
              ) : null}
              <div className="field-grid compact">
                <label>
                  서비스 이름
                  <input
                    value={service.name}
                    onChange={(event) => updateService(index, { name: event.target.value })}
                    placeholder="api"
                  />
                </label>
                <label>
                  내부 포트
                  <input
                    type="number"
                    min={1}
                    max={65535}
                    value={service.internalPort}
                    onChange={(event) =>
                      updateService(index, { internalPort: Number(event.target.value) })
                    }
                  />
                </label>
                <label>
                  Build context
                  <input
                    value={service.build.context}
                    onChange={(event) => updateBuild(index, { context: event.target.value })}
                    placeholder="backend"
                  />
                </label>
                <label>
                  Dockerfile
                  <input
                    value={service.build.dockerfile}
                    onChange={(event) => updateBuild(index, { dockerfile: event.target.value })}
                  />
                </label>
                <label className="full-field">
                  Health check path
                  <input
                    value={service.healthPath}
                    onChange={(event) => updateService(index, { healthPath: event.target.value })}
                  />
                </label>
              </div>

              <ServiceEnvironmentEditor
                service={service}
                serviceIndex={index}
                onChange={(patch) => updateService(index, patch)}
              />
            </fieldset>
          ))}
        </div>

        <button
          type="button"
          className="button dashed"
          onClick={() =>
            setConfig((current) => ({
              ...current,
              services: [...current.services, emptyService(current.services.length)],
            }))
          }
        >
          <Icon name="plus" /> 서비스 추가
        </button>
      </section>

      <aside className="form-card sticky-card">
        <div className="form-section-heading compact-heading">
          <span className="step-index">03</span>
          <div>
            <h2>Gateway routes</h2>
            <p>프로젝트 전용 NGINX가 경로를 내부 서비스로 연결합니다.</p>
          </div>
        </div>

        <div className="route-stack">
          {config.routes.map((route, index) => (
            <div className="route-row" key={`${index}-${route.path}`}>
              <input
                aria-label={`Route ${index + 1} path`}
                value={route.path}
                onChange={(event) => updateRoute(index, { path: event.target.value })}
              />
              <span>→</span>
              <select
                aria-label={`Route ${index + 1} service`}
                value={route.service}
                onChange={(event) => updateRoute(index, { service: event.target.value })}
              >
                {config.services.map((service) => (
                  <option key={service.name} value={service.name}>
                    {service.name}
                  </option>
                ))}
              </select>
              {config.routes.length > 1 ? (
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`Route ${index + 1} 제거`}
                  onClick={() =>
                    setConfig((current) => ({
                      ...current,
                      routes: current.routes.filter((_, routeIndex) => routeIndex !== index),
                    }))
                  }
                >
                  ×
                </button>
              ) : null}
            </div>
          ))}
        </div>

        <button
          type="button"
          className="button dashed"
          onClick={() =>
            setConfig((current) => ({
              ...current,
              routes: [...current.routes, { path: '/api', service: current.services[0].name }],
            }))
          }
        >
          <Icon name="plus" /> Route 추가
        </button>

        <div className="network-note">
          <Icon name="branch" />
          <div>
            <strong>Private generation network</strong>
            <span>서비스는 이름 기반 DNS로만 서로 통신합니다.</span>
          </div>
        </div>

        {message ? <div className="inline-error">{message}</div> : null}

        <div className="form-actions">
          <button type="button" className="button secondary" onClick={() => navigate(-1)}>
            취소
          </button>
          <button className="button primary" disabled={mutation.isPending}>
            {mutation.isPending ? '저장 중…' : '설정 저장'}
            <Icon name="check" />
          </button>
        </div>
      </aside>
    </form>
  );
}
