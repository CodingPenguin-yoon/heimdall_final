import type { ProjectRuntime } from './types';

interface PreviewAccessPanelProps {
  runtime: ProjectRuntime | undefined;
  hostname?: string;
}

export function PreviewAccessPanel({
  runtime,
  hostname = window.location.hostname,
}: PreviewAccessPanelProps) {
  const previewUrl =
    runtime?.status === 'ACTIVE' && runtime.previewPort
      ? `http://${hostname}:${runtime.previewPort}`
      : null;

  return (
    <section
      className={`preview-access-panel${previewUrl ? ' preview-access-active' : ''}`}
      aria-label="Preview 접속"
    >
      <div>
        <span className="eyebrow">Preview access</span>
        <h2>접속 주소</h2>
      </div>

      {previewUrl ? (
        <div className="preview-access-value">
          <a href={previewUrl} target="_blank" rel="noreferrer" className="preview-url">
            {previewUrl}
          </a>
          <a
            href={previewUrl}
            target="_blank"
            rel="noreferrer"
            className="button primary"
            aria-label="Preview 열기"
          >
            Preview 열기
          </a>
        </div>
      ) : (
        <div className="preview-access-empty">
          <strong>
            {runtime === undefined
              ? 'Preview 상태를 확인하고 있습니다.'
              : '배포 성공 후 접속 주소가 생성됩니다.'}
          </strong>
          <span>현재 활성화된 Preview가 없습니다.</span>
        </div>
      )}

      <p>Managed PostgreSQL의 5432 포트는 내부 서비스용이며 브라우저 접속 주소가 아닙니다.</p>
    </section>
  );
}
