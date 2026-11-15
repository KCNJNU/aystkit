"use strict";
// One camera page per browser origin. This lease contains no account or device
// credentials, and has no effect on viewers using another browser or device.
(function (global) {
  const NAME = "observer-camera-playback-v1:" + (global.RobotFleet?.selected() || "default"), HEARTBEAT_MS = 1500, LEASE_MS = 6000;
  const id = global.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  let channel = null;
  try { if (typeof global.BroadcastChannel === "function") channel = new global.BroadcastChannel(NAME); } catch (_) {}
  let locks = global.navigator?.locks;
  if (typeof locks?.request !== "function") locks = null;
  const peers = new Map();
  let wanted = false, active = false, yielded = false, requesting = false, releaseLock = null;
  let phase = "inactive", preferred = null, epoch = 0, settleTimer = null, retryTimer = null, changed = false, lastBeat = 0;
  function state() { return { active, wanted, phase, method: locks ? "web-locks" : channel ? "broadcast" : "single-page" }; }
  function notify() {
    if (changed) return;
    changed = true;
    global.queueMicrotask(() => { changed = false; global.dispatchEvent(new Event("observer-video-lease-change")); });
  }
  function announce(kind = "status") {
    try { channel?.postMessage({ kind, id, wanted, active, yielded }); } catch (_) {}
  }
  function update(next) { if (phase !== next) { phase = next; notify(); } }
  function activate() {
    if (!wanted || yielded || active) return;
    active = true; lastBeat = Date.now(); update("playing"); notify(); announce();
  }
  function relinquish(next = wanted ? "blocked" : "inactive") {
    clearTimeout(settleTimer); settleTimer = null;
    clearTimeout(retryTimer); retryTimer = null;
    const wasActive = active;
    active = false;
    const release = releaseLock; releaseLock = null;
    // Consumers abort their fetches before another page can acquire the lock.
    // The lock promise resolves only after the synchronous event listeners run.
    if (wasActive) global.dispatchEvent(new Event("observer-video-lease-change"));
    update(next); notify();
    release?.();
    if (wasActive) announce("released");
  }
  function later(delay = 60) {
    if (retryTimer || !wanted || active || yielded) return;
    retryTimer = setTimeout(() => { retryTimer = null; attempt(); }, delay);
  }
  function fallbackElection() {
    const now = Date.now();
    for (const [key, peer] of peers) if (now - peer.seen >= LEASE_MS) peers.delete(key);
    if ([...peers.values()].some(peer => peer.active)) { update("blocked"); return; }
    const candidates = [id, ...[...peers.values()].filter(peer => peer.wanted && !peer.yielded).map(peer => peer.id)].sort();
    if (candidates[0] === id) activate(); else update("blocked");
  }
  function attempt() {
    if (!wanted || active || yielded || requesting || settleTimer) return;
    if (phase !== "blocked") update("acquiring");
    if (locks) {
      requesting = true;
      const token = epoch;
      Promise.resolve(locks.request(NAME, { mode: "exclusive", ifAvailable: true }, async lock => {
        if (token !== epoch || !wanted || yielded) return;
        if (!lock) { update("blocked"); return; }
        await new Promise(resolve => { releaseLock = resolve; activate(); });
      })).catch(() => {
        // A denied Web Locks API can still use the short browser-only lease.
        locks = null;
      }).finally(() => { requesting = false; if (!locks) later(); });
      return;
    }
    if (!channel) { activate(); return; }
    announce("probe");
    settleTimer = setTimeout(() => {
      settleTimer = null;
      if (wanted && !active && !yielded) fallbackElection();
    }, 220);
  }
  function setWanted(value) {
    value = value === true;
    if (wanted === value) return;
    wanted = value; epoch += 1; yielded = false; preferred = null;
    if (!wanted) relinquish("inactive");
    else { update("acquiring"); attempt(); }
    notify(); announce("probe");
  }
  function takeover() {
    if (!wanted) return;
    epoch += 1; yielded = false; preferred = id;
    // Existing pages yield first; an exclusive Web Lock is never stolen while
    // its old owner could still be downloading video.
    announce("takeover");
    if (!active) { update("acquiring"); later(80); }
  }
  if (channel) channel.onmessage = event => {
    const item = event.data;
    if (!item || typeof item !== "object" || item.id === id || typeof item.id !== "string" || item.id.length > 128 ||
      !["status", "probe", "takeover", "released"].includes(item.kind) ||
      typeof item.wanted !== "boolean" || typeof item.active !== "boolean" || typeof item.yielded !== "boolean") return;
    peers.set(item.id, { ...item, seen: Date.now() });
    if (peers.size > 64) peers.delete(peers.keys().next().value);
    if (item.kind === "takeover" && wanted) {
      preferred = item.id; yielded = true; epoch += 1; relinquish("blocked"); announce();
    } else if (item.active && active && !locks && item.id < id) {
      // Simultaneous BroadcastChannel elections have one deterministic winner.
      relinquish("blocked");
    } else if ((item.kind === "released" || !item.wanted) && wanted && !active &&
      (!preferred || preferred === item.id)) {
      preferred = null; yielded = false; later();
    }
    if (item.kind === "probe") announce();
  };
  setInterval(() => {
    if (!wanted) return;
    const now = Date.now();
    if (active && !locks && lastBeat && now - lastBeat >= LEASE_MS) {
      // A resumed, previously frozen fallback owner must recheck its lease.
      relinquish("acquiring");
    }
    // A takeover owner may disappear or yield to a simultaneous takeover.
    // Clear only the preference; the real lock or fallback election still wins.
    const owner = preferred ? peers.get(preferred) : null;
    if (yielded && (!owner || owner.yielded || !owner.wanted || now - owner.seen >= LEASE_MS)) {
      preferred = null; yielded = false;
    }
    lastBeat = now; announce();
    if (!active && !yielded) attempt();
  }, HEARTBEAT_MS);
  global.addEventListener("pagehide", () => setWanted(false));
  global.ObserverVideoLease = { setWanted, takeover, state };
})(window);
