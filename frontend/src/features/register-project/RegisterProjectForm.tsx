import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';

import { createProject } from '@/entities/project/api';
import { projectKeys } from '@/entities/project/queries';
import { ApiError } from '@/shared/api/client';
import { Icon } from '@/shared/ui/Icon';

export function RegisterProjectForm() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [repositoryUrl, setRepositoryUrl] = useState('');

  const mutation = useMutation({
    mutationFn: createProject,
    onSuccess: async (project) => {
      await queryClient.invalidateQueries({ queryKey: projectKeys.all });
      navigate(`/projects/${project.id}/settings`);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate({ name: name.trim(), repositoryUrl: repositoryUrl.trim() });
  }

  const message = mutation.error instanceof ApiError ? mutation.error.message : null;

  return (
    <form className="form-card" onSubmit={submit}>
      <div className="form-section-heading">
        <span className="step-index">01</span>
        <div>
          <h2>GitHub 저장소 연결</h2>
          <p>Public 저장소와 main branch를 확인한 뒤 프로젝트 초안을 만듭니다.</p>
        </div>
      </div>

      <div className="field-grid">
        <label>
          프로젝트 이름
          <input
            required
            maxLength={100}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Customer portal"
          />
          <small>Heimdall 안에서 표시할 이름입니다.</small>
        </label>
        <label className="full-field">
          Public GitHub URL
          <div className="input-with-icon">
            <Icon name="git" />
            <input
              required
              type="url"
              value={repositoryUrl}
              onChange={(event) => setRepositoryUrl(event.target.value)}
              placeholder="https://github.com/organization/repository"
            />
          </div>
          <small>인증 정보가 없는 HTTPS 저장소만 지원합니다.</small>
        </label>
      </div>

      {message ? <div className="inline-error">{message}</div> : null}

      <div className="form-actions">
        <button type="button" className="button secondary" onClick={() => navigate('/projects')}>
          취소
        </button>
        <button className="button primary" disabled={mutation.isPending}>
          {mutation.isPending ? '저장소 확인 중…' : '저장소 연결'}
          <Icon name="arrow" />
        </button>
      </div>
    </form>
  );
}
