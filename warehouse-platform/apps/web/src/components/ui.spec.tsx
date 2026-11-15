import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DataState, StatusBadge, StatusTag } from './ui';

describe('shared UX states', () => {
  it('distinguishes not checked health from an outage', () => {
    render(<StatusBadge status="not_checked" />);
    expect(screen.getByText('未检查')).toBeVisible();
  });

  it('maps canonical operational states to Chinese labels without losing the raw value', () => {
    render(<StatusTag status="RUNNING" />);
    expect(screen.getByText('运行中')).toHaveAttribute('title', 'RUNNING');
  });

  it('renders explicit empty and error states', () => {
    const { rerender } = render(<DataState loading={false} empty emptyTitle="No files found" emptyDescription="Change the filters"><span>data</span></DataState>);
    expect(screen.getByText('No files found')).toBeVisible();
    rerender(<DataState loading={false} empty={false} emptyTitle="No files" emptyDescription="No data" error="API failed"><span>data</span></DataState>);
    expect(screen.getByText('API failed')).toBeVisible();
  });
});
