import { ArrowDownOutlined, DownOutlined, UpOutlined } from '@ant-design/icons';
import { Button, Space } from 'antd';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

export function ChatViewport({ children, revision, resetKey }: { children: ReactNode; revision: unknown; resetKey: number }) {
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [edges, setEdges] = useState({ top: true, bottom: true });
  const measure = useCallback(() => {
    const element = viewport.current;
    if (!element) return;
    const next = { top: element.scrollTop <= 2, bottom: element.scrollHeight - element.scrollTop - element.clientHeight <= 2 };
    setEdges(previous => previous.top === next.top && previous.bottom === next.bottom ? previous : next);
  }, []);
  const followLatest = useCallback(() => {
    const element = viewport.current;
    if (element && follow.current) element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
    measure();
  }, [measure]);

  useEffect(() => { follow.current = true; followLatest(); }, [resetKey, followLatest]);
  useEffect(followLatest, [revision, followLatest]);
  useEffect(() => {
    const observer = new ResizeObserver(followLatest);
    if (viewport.current) observer.observe(viewport.current);
    if (content.current) observer.observe(content.current);
    return () => observer.disconnect();
  }, [followLatest]);

  const scroll = (direction: 'up' | 'down' | 'latest') => {
    const element = viewport.current;
    if (!element) return;
    const bottom = Math.max(0, element.scrollHeight - element.clientHeight);
    const top = direction === 'latest' ? bottom : Math.max(0, Math.min(bottom, element.scrollTop + (direction === 'up' ? -1 : 1) * element.clientHeight * .8));
    follow.current = bottom - top <= 2;
    element.scrollTop = top;
    measure();
  };

  return <>
    <div ref={viewport} className="ai-messages" id="ai-chat-messages" role="region" aria-label="聊天记录" tabIndex={0}
      onScroll={() => { const element = viewport.current; if (element) follow.current = element.scrollHeight - element.scrollTop - element.clientHeight <= 2; measure(); }}>
      <div ref={content} className="ai-message-list" aria-live="polite" aria-relevant="additions text">{children}</div>
    </div>
    <nav className="ai-scroll-controls" aria-label="聊天记录翻页">
      <Space size={4}>
        <Button size="small" type="text" icon={<UpOutlined aria-hidden />} disabled={edges.top} onClick={() => scroll('up')} aria-controls="ai-chat-messages">向上翻页</Button>
        <Button size="small" type="text" icon={<DownOutlined aria-hidden />} disabled={edges.bottom} onClick={() => scroll('down')} aria-controls="ai-chat-messages">向下翻页</Button>
      </Space>
      <Button size="small" type="text" icon={<ArrowDownOutlined aria-hidden />} disabled={edges.bottom} onClick={() => scroll('latest')} aria-controls="ai-chat-messages">{edges.bottom ? '已到最新' : '回到最新'}</Button>
    </nav>
  </>;
}
