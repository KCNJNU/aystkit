import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ChatViewport } from './ChatViewport';

describe('Chat transcript navigation', () => {
  it('keeps a reader in place during streaming and resumes following only on returning to the bottom', () => {
    const { rerender } = render(<ChatViewport revision={1} resetKey={0}>第一条回答</ChatViewport>);
    const viewport = screen.getByRole('region', { name: '聊天记录' });
    Object.defineProperties(viewport, { scrollHeight: { configurable: true, value: 1600 }, clientHeight: { value: 400 } });
    fireEvent.scroll(viewport);
    fireEvent.click(screen.getByRole('button', { name: '回到最新' }));
    expect(viewport.scrollTop).toBe(1200);
    fireEvent.click(screen.getByRole('button', { name: '向上翻页' }));
    expect(viewport.scrollTop).toBe(880);
    Object.defineProperty(viewport, 'scrollHeight', { value: 1800 });
    rerender(<ChatViewport revision={2} resetKey={0}>回答继续生成</ChatViewport>);
    expect(viewport.scrollTop).toBe(880);
    fireEvent.click(screen.getByRole('button', { name: '回到最新' }));
    expect(viewport.scrollTop).toBe(1400);
    Object.defineProperty(viewport, 'scrollHeight', { value: 2000 });
    rerender(<ChatViewport revision={3} resetKey={0}>继续生成</ChatViewport>);
    expect(viewport.scrollTop).toBe(1600);
    expect(screen.getByRole('button', { name: '已到最新' })).toBeDisabled();
  });

  it('clamps page navigation and follows newly opened conversations', () => {
    const { rerender } = render(<ChatViewport revision={1} resetKey={0}>会话一</ChatViewport>);
    const viewport = screen.getByRole('region', { name: '聊天记录' });
    expect(screen.getByRole('button', { name: '向上翻页' })).toBeDisabled();
    Object.defineProperties(viewport, { scrollHeight: { value: 600 }, clientHeight: { value: 400 } });
    fireEvent.scroll(viewport);
    fireEvent.click(screen.getByRole('button', { name: '向下翻页' }));
    expect(viewport.scrollTop).toBe(200);
    fireEvent.click(screen.getByRole('button', { name: '向上翻页' }));
    expect(viewport.scrollTop).toBe(0);
    rerender(<ChatViewport revision={2} resetKey={1}>会话二</ChatViewport>);
    expect(viewport.scrollTop).toBe(200);
  });
});
