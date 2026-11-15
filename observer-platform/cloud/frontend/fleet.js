"use strict";
(function (global) {
  const current = new URL(global.location.href);
  let selected = current.searchParams.get('robot') || '';
  let robots = [];
  const nativeFetch = global.fetch.bind(global);
  function scoped(path) {
    const url = new URL(path, global.location.href);
    return url.origin === global.location.origin && /^(?:\/snapshot\.json$|\/visuals\.json$|\/camera\/|\/visual\/|\/native-camera\/|\/api\/(?:tests(?:\/|$)|test-issues(?:\/|$)|fault-logs(?:\/|$)|admin\/tests\/))/.test(url.pathname);
  }
  function robotFetch(path, options = {}) {
    if (!selected || !scoped(typeof path === 'string' ? path : path.url || String(path))) return nativeFetch(path, options);
    const headers = new Headers(options.headers || (path instanceof Request ? path.headers : undefined));
    headers.set('X-Observer-Robot', selected);
    return nativeFetch(path, { ...options, headers });
  }
  function socketUrl(path) {
    const url = new URL(path, global.location.href);
    if (selected) url.searchParams.set('robotId', selected);
    return url.pathname + url.search;
  }
  function warehouseLink() {
    const url = new URL('/dbprog/', global.location.href);
    const robot = robots.find(row => row.robotId === selected);
    if (selected) url.searchParams.set('observerRobot', selected);
    if (robot?.warehouseId) url.searchParams.set('robotId', robot.warehouseId);
    document.querySelectorAll('a.dbprog-nav').forEach(link => { link.href = url.pathname + url.search; });
    const library = new URL('/sensor-library/', global.location.href);
    if (selected) library.searchParams.set('robot', selected);
    document.querySelectorAll('a.sensor-library-nav').forEach(link => { link.href = library.pathname + library.search; });
  }
  const picker = document.getElementById('robot-picker');
  async function load() {
    if (!picker) return;
    try {
      const response = await nativeFetch('/api/robots', { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error('robot list unavailable');
      const payload = await response.json();
      if (!Array.isArray(payload.robots) || !payload.robots.length) throw new Error('robot list empty');
      robots = payload.robots;
      selected = selected || payload.defaultRobotId || robots[0].robotId;
      picker.replaceChildren();
      for (const robot of robots) {
        const option = document.createElement('option');
        option.value = robot.robotId;
        option.textContent = `${robot.name || robot.robotId} · ${robot.online ? '在线' : '离线'}`;
        picker.append(option);
      }
      if (!robots.some(robot => robot.robotId === selected)) {
        const option = document.createElement('option');option.value = selected;option.textContent = '机器人未注册';picker.append(option);
      }
      picker.value = selected;
      picker.disabled = false;
      warehouseLink();
    } catch (_) {
      picker.replaceChildren();
      const option = document.createElement('option');option.textContent = '机器人列表暂不可用';picker.append(option);
    }
  }
  picker?.addEventListener('change', () => {
    if (!robots.some(robot => robot.robotId === picker.value)) return;
    const url = new URL(global.location.href);url.searchParams.set('robot', picker.value);
    // Full navigation closes old sockets, videos, requests, charts and map layers.
    global.location.assign(url.href);
  });
  global.RobotFleet = Object.freeze({ fetch: robotFetch, socketUrl, selected: () => selected, ready: load() });
})(window);
