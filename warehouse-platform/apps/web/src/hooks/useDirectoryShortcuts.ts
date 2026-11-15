import { useEffect } from 'react';

export function useDirectoryShortcuts({ canBack, canForward, canUp, back, forward, up }: {
  canBack: boolean; canForward: boolean; canUp: boolean;
  back: () => void; forward: () => void; up: () => void;
}) {
  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.repeat || event.defaultPrevented) return;
      const target = event.target;
      if (target instanceof Element && target.closest('input, textarea, select, [contenteditable="true"], [role="dialog"], .ant-drawer')) return;
      // Ant keeps closed dialogs mounted. Only a rendered dialog should block
      // workspace shortcuts; hidden wrappers produce an empty client-rect list.
      if (Array.from(document.querySelectorAll('[role="dialog"][aria-modal="true"]')).some(dialog =>
        !dialog.closest('[hidden]') && dialog.getClientRects().length > 0
        && !['hidden', 'collapse'].includes(window.getComputedStyle(dialog).visibility))) return;
      const action = event.key === 'ArrowLeft' && canBack ? back
        : event.key === 'ArrowRight' && canForward ? forward
        : event.key === 'ArrowUp' && canUp ? up : undefined;
      if (action) { event.preventDefault(); action(); }
    };
    window.addEventListener('keydown', handle);
    return () => window.removeEventListener('keydown', handle);
  }, [canBack, canForward, canUp, back, forward, up]);
}
