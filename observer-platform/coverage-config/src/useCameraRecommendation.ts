import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { sanitizeConfig } from './model';
import type { Config } from './model';
import type { RecommendationResult } from './optimizer';
import type { RecommendationRequest, RecommendationResponse } from './optimizer.worker';

type Status = 'idle' | 'running' | 'ready' | 'error';
interface State {
  status: Status;
  progress: number;
  result: RecommendationResult | null;
  sourceKey: string | null;
  error: string;
}
interface Job { worker: Worker; generation: number; key: string; }
const idle = (): State => ({ status: 'idle', progress: 0, result: null, sourceKey: null, error: '' });

/** A recommendation belongs to one complete configuration, never a baseline. */
export function useCameraRecommendation(config: Config, active = true) {
  const clean = useMemo(() => sanitizeConfig(config), [config]);
  const key = JSON.stringify(clean);
  const latest = useRef({ config: clean, key, active });
  latest.current = { config: clean, key, active };
  const generation = useRef(0), job = useRef<Job | null>(null);
  const [state, setState] = useState<State>(idle);
  const stop = useCallback(() => {
    generation.current++;
    if (job.current) {
      job.current.worker.onmessage = null;
      job.current.worker.onerror = null;
      job.current.worker.terminate();
      job.current = null;
    }
  }, []);
  const cancel = useCallback(() => { stop(); setState(idle()); }, [stop]);
  const start = useCallback(() => {
    stop();
    const source = latest.current;
    if (!source.active) { setState(idle()); return; }
    setState({ status: 'running', progress: 0, result: null, sourceKey: source.key, error: '' });
    let worker: Worker;
    try { worker = new Worker(new URL('./optimizer.worker.ts', import.meta.url), { type: 'module' }); }
    catch { setState({ status: 'error', progress: 0, result: null, sourceKey: source.key, error: '浏览器无法启动推荐计算，请刷新后重试。' }); return; }
    const current: Job = { worker, generation: generation.current, key: source.key };
    job.current = current;
    const isCurrent = () => job.current === current && generation.current === current.generation && latest.current.active && latest.current.key === current.key;
    const fail = () => {
      if (!isCurrent()) return;
      stop();
      setState({ status: 'error', progress: 0, result: null, sourceKey: current.key, error: '推荐计算未完成，请重试。' });
    };
    worker.onmessage = (event: MessageEvent<RecommendationResponse>) => {
      const message = event.data;
      if (!isCurrent() || message?.generation !== current.generation || message.key !== current.key) return;
      if (message.type === 'progress') {
        if (!Number.isFinite(message.progress)) return;
        setState(previous => previous.status === 'running' && previous.sourceKey === current.key
          ? { ...previous, progress: Math.max(previous.progress, Math.min(1, Math.max(0, message.progress))) } : previous);
      } else if (message.type === 'result') {
        stop();
        setState({ status: 'ready', progress: 1, result: message.result, sourceKey: current.key, error: '' });
      } else if (message.type === 'error') fail();
    };
    worker.onerror = event => { event.preventDefault(); fail(); };
    const request: RecommendationRequest = { type: 'start', generation: current.generation, key: current.key, config: source.config };
    try { worker.postMessage(request); } catch { fail(); }
  }, [stop]);
  useEffect(() => {
    if (!active || (job.current && job.current.key !== key)) stop();
    setState(previous => active && previous.sourceKey === key ? previous : idle());
  }, [key, active, stop]);
  useEffect(() => {
    const cancelRunning = () => { if (job.current) cancel(); };
    const hidden = () => { if (document.visibilityState === 'hidden') cancelRunning(); };
    window.addEventListener('pagehide', cancelRunning);
    document.addEventListener('visibilitychange', hidden);
    return () => {
      window.removeEventListener('pagehide', cancelRunning);
      document.removeEventListener('visibilitychange', hidden);
      stop();
    };
  }, [cancel, stop]);
  // Mask immediately during a configuration render, before effects can run.
  const visible = active && state.sourceKey === key ? state : idle();
  return { ...visible, start, cancel };
}
