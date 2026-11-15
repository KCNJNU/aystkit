import { fireEvent, renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useDirectoryShortcuts } from './useDirectoryShortcuts';

it('uses only available directory actions and releases shortcuts when unmounted', () => {
  const back = vi.fn(), forward = vi.fn(), up = vi.fn();
  const { unmount } = renderHook(() => useDirectoryShortcuts({ canBack: true, canForward: false, canUp: true, back, forward, up }));
  const left = new KeyboardEvent('keydown', { key: 'ArrowLeft', altKey: true, cancelable: true }); window.dispatchEvent(left);
  expect(left.defaultPrevented).toBe(true); expect(back).toHaveBeenCalledTimes(1);
  const right = new KeyboardEvent('keydown', { key: 'ArrowRight', altKey: true, cancelable: true }); window.dispatchEvent(right);
  expect(right.defaultPrevented).toBe(false); expect(forward).not.toHaveBeenCalled();
  fireEvent.keyDown(window, { key: 'ArrowUp', altKey: true }); expect(up).toHaveBeenCalledTimes(1);
  unmount(); fireEvent.keyDown(window, { key: 'ArrowLeft', altKey: true }); expect(back).toHaveBeenCalledTimes(1);
});

it('preserves text editing and dialog interaction instead of changing directories', () => {
  const back = vi.fn(), forward = vi.fn(), up = vi.fn();
  renderHook(() => useDirectoryShortcuts({ canBack: true, canForward: true, canUp: true, back, forward, up }));
  const input = document.createElement('input'); document.body.append(input);
  const modal = document.createElement('section'); modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true');
  vi.spyOn(modal, 'getClientRects').mockReturnValue([new DOMRect(0, 0, 300, 200)] as unknown as DOMRectList);
  try {
    fireEvent.keyDown(input, { key: 'ArrowLeft', altKey: true });
    fireEvent.keyDown(window, { key: 'ArrowUp', altKey: true, ctrlKey: true });
    document.body.append(modal); fireEvent.keyDown(window, { key: 'ArrowRight', altKey: true });
    expect(back).not.toHaveBeenCalled(); expect(forward).not.toHaveBeenCalled(); expect(up).not.toHaveBeenCalled();
  } finally { input.remove(); modal.remove(); }
});

it('restores shortcuts after a closed dialog stays mounted in its hidden wrapper', () => {
  const back = vi.fn(), forward = vi.fn(), up = vi.fn();
  renderHook(() => useDirectoryShortcuts({ canBack: true, canForward: true, canUp: true, back, forward, up }));
  const wrapper = document.createElement('div');
  const modal = document.createElement('section'); modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true');
  wrapper.append(modal); document.body.append(wrapper);
  const rects = vi.spyOn(modal, 'getClientRects').mockReturnValue([new DOMRect(0, 0, 300, 200)] as unknown as DOMRectList);
  try {
    fireEvent.keyDown(window, { key: 'ArrowLeft', altKey: true }); expect(back).not.toHaveBeenCalled();
    // Simulate Ant's closing result: the modal remains in the DOM, but its
    // display:none wrapper means it no longer contributes any layout boxes.
    wrapper.style.display = 'none'; rects.mockReturnValue([] as unknown as DOMRectList);
    expect(modal).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'ArrowLeft', altKey: true }); expect(back).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: 'ArrowRight', altKey: true }); expect(forward).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: 'ArrowUp', altKey: true }); expect(up).toHaveBeenCalledTimes(1);
  } finally { wrapper.remove(); }
});
