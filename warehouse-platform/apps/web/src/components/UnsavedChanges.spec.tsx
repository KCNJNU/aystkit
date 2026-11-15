import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UnsavedChangesProvider, useUnsavedChanges } from './UnsavedChanges';

function Harness({ action }: { action: () => void }) {
  const { dirty, setDirty, confirmDiscard } = useUnsavedChanges();
  return <><output>{dirty ? '已修改' : '未修改'}</output><button onClick={() => setDirty(true)}>Stage change</button><button onClick={() => confirmDiscard(action)}>Leave workspace</button></>;
}

describe('UnsavedChangesProvider', () => {
  it('navigates immediately and clears dirty state without a browser confirmation', () => {
    const action = vi.fn(); const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      render(<UnsavedChangesProvider><Harness action={action} /></UnsavedChangesProvider>);
      fireEvent.click(screen.getByRole('button', { name: 'Stage change' }));
      expect(screen.getByText('已修改')).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Leave workspace' }));
      expect(confirm).not.toHaveBeenCalled();
      expect(action).toHaveBeenCalledOnce();
      expect(screen.getByText('未修改')).toBeVisible();
    } finally { confirm.mockRestore(); }
  });
});
