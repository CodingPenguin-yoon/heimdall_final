import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { ProjectRuntime } from './types';

import { PreviewAccessPanel } from './PreviewAccessPanel';

afterEach(cleanup);

describe('PreviewAccessPanel', () => {
  it('shows the browser-accessible URL for an active runtime', () => {
    const runtime: ProjectRuntime = {
      status: 'ACTIVE',
      previewPort: 49152,
      activeDeploymentId: 'deployment-1',
      updatedAt: '2026-08-04T06:00:00Z',
    };

    render(<PreviewAccessPanel runtime={runtime} hostname="127.0.0.1" />);

    const link = screen.getByRole('link', { name: 'http://127.0.0.1:49152' });
    expect(link).toHaveAttribute('href', 'http://127.0.0.1:49152');
    expect(screen.getByRole('link', { name: 'Preview 열기' })).toHaveAttribute(
      'href',
      'http://127.0.0.1:49152',
    );
  });

  it('explains why no address exists before a successful deployment', () => {
    render(
      <PreviewAccessPanel
        runtime={{
          status: 'NOT_ACTIVE',
          previewPort: null,
          activeDeploymentId: null,
          updatedAt: null,
        }}
        hostname="127.0.0.1"
      />,
    );

    expect(screen.getByText('배포 성공 후 접속 주소가 생성됩니다.')).toBeInTheDocument();
    expect(screen.getByText(/5432/)).toBeInTheDocument();
  });
});
