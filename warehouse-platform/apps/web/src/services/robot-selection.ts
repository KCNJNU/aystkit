const KEY = 'warehouse-robot-id';
const ALIASES_KEY = 'warehouse-observer-robots';
let selected: string | undefined;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const alias = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

/** undefined means this navigation has no explicit robot context. */
export function robotFromSearch(search: string): string | undefined {
  const value = new URLSearchParams(search).get('robotId');
  return value === 'all' ? '' : value && uuid.test(value) ? value : undefined;
}
export function rememberObserverRobot(robotId: string, observerId: string) {
  if (!uuid.test(robotId) || !alias.test(observerId)) return;
  try {
    const stored: unknown = JSON.parse(sessionStorage.getItem(ALIASES_KEY) || '{}');
    const mappings: Record<string, string> = stored && typeof stored === 'object' && !Array.isArray(stored) ? stored as Record<string, string> : {};
    sessionStorage.setItem(ALIASES_KEY, JSON.stringify({ ...mappings, [robotId]: observerId }));
  } catch { /* A missing return link must not prevent dataset access. */ }
}
export function observerRobotUrl(robotId: string, fleet: Array<{ id: string; observerId?: string }> = []): string | undefined {
  if (!robotId) return '/';
  let observerId = fleet.find(robot => robot.id === robotId)?.observerId;
  try {
    const mappings = JSON.parse(sessionStorage.getItem(ALIASES_KEY) || '{}') as Record<string, unknown>;
    const mapped = mappings?.[robotId];
    if (typeof mapped === 'string' && alias.test(mapped)) observerId = mapped;
  } catch { /* Explicit registered fleet aliases remain usable. */ }
  if (!observerId || !alias.test(observerId)) return undefined;
  return '/?robot=' + encodeURIComponent(observerId);
}
export function selectedRobot() {
  if (selected !== undefined) return selected;
  const params = new URLSearchParams(window.location.search);
  const value = robotFromSearch(window.location.search);
  if (value !== undefined) {
    const observer = params.get('observerRobot');
    if (observer) rememberObserverRobot(value, observer);
    selectRobot(value); return value;
  }
  try { const stored = sessionStorage.getItem(KEY) || ''; selected = uuid.test(stored) ? stored : ''; } catch { selected = ''; }
  return selected;
}
export function selectRobot(value: string) {
  selected = uuid.test(value) ? value : '';
  try { if (selected) sessionStorage.setItem(KEY, selected); else sessionStorage.removeItem(KEY); } catch { /* Tab memory remains usable. */ }
}
/** Every history entry carries its robot, including the explicit all-robots view. */
export function robotPath(path: string, robotId = selectedRobot()) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set('robotId', robotId || 'all');
  const observer = observerRobotUrl(robotId);
  if (observer && observer !== '/') url.searchParams.set('observerRobot', new URL(observer, window.location.origin).searchParams.get('robot')!);
  else url.searchParams.delete('observerRobot');
  return url.pathname + url.search + url.hash;
}
export function robotQuery(path: string) {
  const robotId = selectedRobot();
  if (!robotId || !/^\/(?:v1\/(?:datasets\/(?:page|versions\/page)|files|search))(?:\?|$)/.test(path)) return path;
  const url = new URL(path, 'https://warehouse.invalid');
  // A page-level robot filter must agree with the global selection.
  url.searchParams.set('robotId', robotId);
  return url.pathname + url.search;
}
