import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
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

type ServiceDraft = Omit<ServiceConfig, 'internalPort'> & {
  draftKey: string;
  internalPort: number | '';
};

interface RouteDraft {
  path: string;
  serviceKey: string;
}

interface DeploymentConfigDraft {
  services: ServiceDraft[];
  routes: RouteDraft[];
}

const emptyService = (index: number): ServiceDraft => ({
  draftKey: `service-${index}`,
  name: index === 0 ? 'web' : `service-${index + 1}`,
  build: { context: index === 0 ? '.' : `service-${index + 1}`, dockerfile: 'Dockerfile' },
  internalPort: index === 0 ? 3000 : 8000,
  healthPath: '/health',
  environment: [],
  projectDatabaseAccess: false,
});

function initialConfig(project: Project): DeploymentConfigDraft {
  if (!project.deploymentConfig) {
    const service = emptyService(0);
    return {
      services: [service],
      routes: [{ path: '/', serviceKey: service.draftKey }],
    };
  }
  const services = project.deploymentConfig.services.map((service, index) => ({
    ...service,
    draftKey: `service-${index}`,
    environment: service.environment ?? [],
    projectDatabaseAccess: service.projectDatabaseAccess ?? false,
  }));
  const serviceKeys = new Map(services.map((service) => [service.name, service.draftKey]));
  return {
    services,
    routes: project.deploymentConfig.routes.map((route) => ({
      path: route.path,
      serviceKey: serviceKeys.get(route.service) ?? '',
    })),
  };
}

export function ProjectSettingsForm({ project }: { project: Project }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const nextServiceIndex = useRef(project.deploymentConfig?.services.length ?? 1);
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

  function updateService(index: number, patch: Partial<ServiceDraft>) {
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

  function updateRoute(index: number, patch: Partial<RouteDraft>) {
    setConfig((current) => ({
      ...current,
      routes: current.routes.map((route, routeIndex) =>
        routeIndex === index ? { ...route, ...patch } : route,
      ),
    }));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const services: ServiceConfig[] = [];
    const serviceNames = new Map<string, string>();
    for (const service of config.services) {
      if (service.internalPort === '') return;
      const { draftKey, ...serviceConfig } = service;
      serviceNames.set(draftKey, service.name);
      services.push({ ...serviceConfig, internalPort: service.internalPort });
    }
    const routes: RouteConfig[] = config.routes.map((route) => ({
      path: route.path,
      service: serviceNames.get(route.serviceKey) ?? '',
    }));
    mutation.mutate({ services, routes });
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
                      routes: current.routes.filter(
                        (route) => route.serviceKey !== service.draftKey,
                      ),
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
                    required
                    value={service.internalPort}
                    onChange={(event) => {
                      const value = event.target.value;
                      updateService(index, { internalPort: value === '' ? '' : Number(value) });
                    }}
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
          onClick={() => {
            const service = emptyService(nextServiceIndex.current);
            nextServiceIndex.current += 1;
            setConfig((current) => ({
              ...current,
              services: [...current.services, service],
            }));
          }}
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
                value={route.serviceKey}
                onChange={(event) => updateRoute(index, { serviceKey: event.target.value })}
              >
                {config.services.map((service) => (
                  <option key={service.draftKey} value={service.draftKey}>
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
              routes: [
                ...current.routes,
                { path: '/api', serviceKey: current.services[0].draftKey },
              ],
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
