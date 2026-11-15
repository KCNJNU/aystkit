import type { Config } from './model';
import { optimizeCameraPositions } from './optimizer';
import type { OptimizeCameraOptions, RecommendationResult } from './optimizer';

export interface RecommendationRequest {
  type: 'start';
  generation: number;
  key: string;
  config: Config;
}
export interface RecommendationCancelRequest {
  type: 'cancel';
  generation?: number;
  key?: string;
}
export type RecommendationResponse =
  | { type: 'progress'; generation: number; key: string; progress: number }
  | { type: 'result'; generation: number; key: string; result: RecommendationResult }
  | { type: 'error'; generation: number; key: string; error: string };

type Runner = (config: Config, options?: OptimizeCameraOptions) => Promise<RecommendationResult>;
/** Separate transport from the search so stale-result behavior is testable. */
export function createRecommendationController(send: (message: RecommendationResponse) => void, run: Runner = optimizeCameraPositions) {
  let active: {generation:number; key:string} | null = null;
  return async (request: RecommendationRequest | RecommendationCancelRequest): Promise<void> => {
    if (request?.type === 'cancel') {
      if (active && (request.generation === undefined || request.generation === active.generation) &&
          (request.key === undefined || request.key === active.key)) active = null;
      return;
    }
    if (request?.type !== 'start' || !Number.isFinite(request.generation) || typeof request.key !== 'string') return;
    const task = {generation:request.generation, key:request.key};
    active = task;
    let lastProgress = -1, lastSent = 0;
    const progress = (value: number) => {
      if (active !== task || !Number.isFinite(value)) return;
      const next = Math.max(lastProgress,Math.min(1,Math.max(0,value)));
      const now = performance.now();
      if (next < 1 && next-lastProgress < .015 && now-lastSent < 75) return;
      lastProgress = next; lastSent = now;
      send({type:'progress',...task,progress:next});
    };
    try {
      progress(0);
      const result = await run(request.config,{onProgress:progress,shouldCancel:() => active !== task});
      if (active !== task) return;
      progress(1);
      send({type:'result',...task,result});
      active = null;
    } catch (error) {
      if (active !== task) return;
      active = null;
      if (error instanceof Error && error.name === 'AbortError') return;
      send({type:'error',...task,error:'推荐计算未完成，请重试。'});
    }
  };
}
if (typeof self !== 'undefined') {
  const scope = self as unknown as {
    onmessage: ((event: MessageEvent<RecommendationRequest | RecommendationCancelRequest>) => void) | null;
    postMessage: (message: RecommendationResponse) => void;
  };
  const handle = createRecommendationController(message => scope.postMessage(message));
  scope.onmessage = event => { void handle(event.data); };
}
