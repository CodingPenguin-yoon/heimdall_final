import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { StatusBadge } from './StatusBadge';

describe('StatusBadge', () => {
  it('uses an actionable label for draft projects', () => {
    render(<StatusBadge status="DRAFT" />);

    expect(screen.getByText('Setup required')).toBeVisible();
  });

  it('shows ready projects', () => {
    render(<StatusBadge status="READY" />);

    expect(screen.getByText('Ready')).toBeVisible();
  });

  it('shows projects locked for deletion', () => {
    render(<StatusBadge status="DELETING" />);

    expect(screen.getByText('Deleting')).toBeVisible();
  });
});
