"use strict";
// Independent same-origin fragmented-MP4 player. No upstream page or client
// code is embedded; the existing Observer owns its playback cards.
(function (global) {
  const scopedFetch = (...args) => global.RobotFleet ? global.RobotFleet.fetch(...args) : fetch(...args);
  // Start at four preview frames; adapt only after actual playback underruns.
  // Both catch-up thresholds follow the bounded target, avoiding repeated seeks
  // immediately after the player has increased its protection against jitter.
  const MIN_TARGET = .4, MAX_TARGET = 1, LOW_BUFFER = .05, KEEP_BUFFER = 20;
  const CATCHUP_MARGIN = .3, MAX_MARGIN = .8, CATCHUP_RATE = 1.03;
  const STABLE_MS = 20000, RELAX_MS = 10000, RECOVERY_MS = 1500;
  const INIT_LIMIT = 1024 * 1024, READ_LIMIT = 4 * 1024 * 1024;
  const players = new WeakMap();
  function avcCodec(bytes) {
    // AVCDecoderConfigurationRecord stores profile/compatibility/level.
    // Read them from this stream's initialization segment, not a guessed profile.
    for (let offset = 4; offset + 8 <= bytes.length; offset += 1) {
      if (bytes[offset] !== 0x61 || bytes[offset + 1] !== 0x76 ||
        bytes[offset + 2] !== 0x63 || bytes[offset + 3] !== 0x43) continue;
      const view = new DataView(bytes.buffer, bytes.byteOffset + offset - 4, 4);
      const size = view.getUint32(0);
      if (size < 12 || offset - 4 + size > bytes.length || bytes[offset + 4] !== 1) continue;
      return "avc1." + [...bytes.slice(offset + 5, offset + 8)]
        .map(value => value.toString(16).padStart(2, "0")).join("");
    }
    return null;
  }
  function aheadOf(video) {
    const ranges = video.buffered;
    if (!ranges || !ranges.length) return null;
    const index = ranges.length - 1;
    const start = ranges.start(index), end = ranges.end(index);
    return Number.isFinite(start) && Number.isFinite(end) && end > start
      ? { start, end, ahead: end - video.currentTime } : null;
  }
  function sourceOperation(buffer, operation, signal) {
    return new Promise((resolve, reject) => {
      const done = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new Error("Video buffer failed")); };
      const aborted = () => { cleanup(); reject(new DOMException("Stopped", "AbortError")); };
      const cleanup = () => {
        buffer.removeEventListener("updateend", done);
        buffer.removeEventListener("error", failed);
        signal.removeEventListener("abort", aborted);
      };
      if (signal.aborted) { aborted(); return; }
      buffer.addEventListener("updateend", done, { once: true });
      buffer.addEventListener("error", failed, { once: true });
      signal.addEventListener("abort", aborted, { once: true });
      try { operation(); } catch (error) { cleanup(); reject(error); }
    });
  }
  class NativeCameraPlayer {
    constructor(video, options = {}) {
      this.video = video; this.options = options; this.epoch = 0;
      this.phase = "stopped"; this.frameVersion = 0; this.lastFrameAt = 0;
      this.frameTimes = []; this.targetBuffer = MIN_TARGET;
      this.rebuffering = false; this.nextRelaxAt = performance.now() + STABLE_MS;
      this.lastAppendAt = 0; this.lastBufferEnd = null;
      this.recoveryFrameAt = null; this.recoveryUntil = 0; this.limitedRetries = 0;
      video.muted = true; video.playsInline = true; video.controls = false;
      players.set(video, this);
    }
    current(epoch = this.epoch) {
      return epoch === this.epoch && !!this.controller &&
        !this.controller.signal.aborted && (!this.options.isCurrent || this.options.isCurrent());
    }
    state(status, phase, message = "") {
      if (this.phase === phase && this.status === status && this.message === message) return;
      this.phase = phase; this.status = status; this.message = message;
      this.options.onState?.(status, { phase, message });
    }
    drawable(now = performance.now()) {
      return this.current() && this.phase === "playing" && !this.video.paused &&
        (!this.options.isSourceReady || this.options.isSourceReady()) &&
        this.video.readyState >= 2 && this.video.videoWidth > 0 && this.video.videoHeight > 0 &&
        this.lastFrameAt > 0 && now - this.lastFrameAt < 1000;
    }
    decodedFps(now = performance.now()) {
      const times = this.frameTimes.filter(value => now - value <= 2000);
      const span = times[times.length - 1] - times[0];
      return this.drawable(now) && times.length > 1 && span > 0 ? (times.length - 1) * 1000 / span : null;
    }
    decoded(now) {
      this.lastFrameAt = now; this.frameVersion += 1; this.started = true;
      if (!this.video.paused) this.rebuffering = false;
      this.recoveryFrameAt = null; this.recoveryUntil = 0;
      this.frameTimes.push(now);
      if (this.frameTimes.length > 128) this.frameTimes.splice(0, this.frameTimes.length - 128);
      clearTimeout(this.initTimer); this.initTimer = null;
    }
    observeFrame(epoch) {
      if (!this.current(epoch) || !this.video.requestVideoFrameCallback) return;
      this.frameCallback = this.video.requestVideoFrameCallback((now, info) => {
        this.frameCallback = null;
        if (!this.current(epoch)) return;
        if (info.mediaTime !== this.lastMediaTime || info.presentedFrames !== this.lastPresentedFrames) {
          this.lastMediaTime = info.mediaTime; this.lastPresentedFrames = info.presentedFrames;
          this.decoded(now);
          if (!this.video.paused) this.state("AVAILABLE", "playing");
          this.options.onFrame?.({ width: this.video.videoWidth, height: this.video.videoHeight,
            mediaTime: info.mediaTime, streamSession: this.streamSession, frameVersion: this.frameVersion });
        }
        this.observeFrame(epoch);
      });
    }
    async trim(epoch, force = false) {
      const buffer = this.buffer, video = this.video;
      if (!this.current(epoch) || !buffer || buffer.updating || !buffer.buffered.length) return;
      const cut = video.currentTime - KEEP_BUFFER;
      if (cut <= 0 || buffer.buffered.start(0) >= cut || (!force && cut - (this.lastTrim || 0) < 3)) return;
      await sourceOperation(buffer, () => buffer.remove(0, cut), this.controller.signal);
      this.lastTrim = cut;
    }
    appended(now = performance.now()) {
      const range = aheadOf(this.video);
      // A partial MP4 box or repeated bytes must not renew a decoder's grace.
      if (range && (this.lastBufferEnd === null || range.end > this.lastBufferEnd + 1e-6)) {
        this.lastBufferEnd = range.end; this.lastAppendAt = now;
      }
    }
    async append(chunk, epoch) {
      if (!this.current(epoch)) return;
      await this.trim(epoch);
      try {
        await sourceOperation(this.buffer, () => this.buffer.appendBuffer(chunk), this.controller.signal);
      } catch (error) {
        if (error.name !== "QuotaExceededError" || !this.current(epoch)) throw error;
        await this.trim(epoch, true);
        await sourceOperation(this.buffer, () => this.buffer.appendBuffer(chunk), this.controller.signal);
      }
      if (this.current(epoch)) this.appended();
    }
    underrun(now = performance.now()) {
      if (this.started && !this.rebuffering) {
        this.targetBuffer = Math.min(MAX_TARGET, Math.round((this.targetBuffer + .2) * 10) / 10);
        this.rebuffering = true; this.nextRelaxAt = now + STABLE_MS;
      }
      this.video.pause(); this.state("WAITING_UPSTREAM", "buffering", "缓冲中");
    }
    tick(epoch) {
      if (epoch !== this.epoch) return;
      if (!this.current(epoch)) { this.stop(); return; }
      const now = performance.now(), video = this.video;
      if (video.error) { this.retry(epoch, "视频解码失败"); return; }
      if (!video.requestVideoFrameCallback) {
        const quality = video.getVideoPlaybackQuality?.();
        const count = quality?.totalVideoFrames ?? video.webkitDecodedFrameCount;
        const progress = Number.isFinite(count) ? count : video.currentTime;
        if (!video.paused && video.readyState >= 2 && progress !== this.lastProgress) {
          this.lastProgress = progress; this.decoded(now);
          this.state("AVAILABLE", "playing");
          this.options.onFrame?.({ width: video.videoWidth, height: video.videoHeight,
            mediaTime: video.currentTime, streamSession: this.streamSession, frameVersion: this.frameVersion });
        }
      }
      const range = aheadOf(video);
      if (this.started && now - this.lastFrameAt >= 8000) {
        const resumable = range && range.ahead > LOW_BUFFER &&
          this.lastAppendAt > this.lastFrameAt && now - this.lastAppendAt < RECOVERY_MS;
        if (resumable && this.recoveryFrameAt !== this.lastFrameAt) {
          this.recoveryFrameAt = this.lastFrameAt; this.recoveryUntil = now + RECOVERY_MS;
        }
        // Try recovered media before purging a live MSE stream. Continuous new
        // bytes cannot indefinitely extend this one decoder-recovery attempt.
        if (!resumable || now >= this.recoveryUntil) {
          this.retry(epoch, "视频停顿，正在重连"); return;
        }
      }
      if (!range) return;
      if (this.started && !video.paused && now - this.lastFrameAt < 1000 &&
          this.targetBuffer > MIN_TARGET && now >= this.nextRelaxAt) {
        this.targetBuffer = Math.max(MIN_TARGET, Math.round((this.targetBuffer - .1) * 10) / 10);
        this.nextRelaxAt = now + RELAX_MS;
      }
      if (video.currentTime < range.start) video.currentTime = range.start;
      let ahead = range.end - video.currentTime;
      if (ahead > this.targetBuffer + MAX_MARGIN) {
        video.currentTime = Math.max(range.start, range.end - this.targetBuffer);
        ahead = range.end - video.currentTime;
      }
      video.playbackRate = ahead > this.targetBuffer + CATCHUP_MARGIN ? CATCHUP_RATE : 1;
      if (!video.paused && ahead < LOW_BUFFER) {
        this.underrun(now);
      } else if (video.paused && ahead >= this.targetBuffer - 1e-6 && !this.playPending) {
        this.playPending = true;
        Promise.resolve(video.play()).catch(() => {
          if (this.current(epoch)) this.retry(epoch, "无法开始视频播放");
        }).finally(() => { if (this.epoch === epoch) this.playPending = false; });
      } else if (this.started && now - this.lastFrameAt >= 1000) {
        this.state("WAITING_UPSTREAM", "buffering", "等待新画面");
      }
    }
    start(path) {
      if (!/^\/native-camera\/(usbFront|usbLeft|usbHub2|usbHub3)\/video\.mp4$/.test(path)) throw new Error("Invalid video path");
      this.stop(); this.path = path;
      const epoch = this.epoch, controller = new AbortController();
      this.controller = controller; this.started = false; this.lastFrameAt = 0;
      this.lastMediaTime = null; this.lastPresentedFrames = null; this.lastProgress = null;
      this.frameTimes = []; this.rebuffering = false; this.nextRelaxAt = performance.now() + STABLE_MS;
      this.lastAppendAt = 0; this.lastBufferEnd = null; this.recoveryFrameAt = null; this.recoveryUntil = 0;
      this.lastTrim = 0; this.playPending = false;
      this.state("WAITING_UPSTREAM", "initializing", "正在连接视频");
      if (typeof global.MediaSource !== "function") {
        this.state("UNAVAILABLE", "unsupported", "浏览器不支持 H.264 流播放"); return;
      }
      this.waitingListener = () => {
        const video = this.video;
        // Firefox can wait with almost one frame still in buffered.end(). The
        // media event identifies this real underrun even above the low-water
        // threshold. Ignore startup, seeks, pauses and an already-recovered
        // queued event; one outage can increase the target only once.
        if (this.current(epoch) && this.started && !video.paused &&
            !video.seeking && video.readyState < 3) this.underrun();
      };
      this.video.addEventListener?.("waiting", this.waitingListener);
      this.initTimer = setTimeout(() => this.retry(epoch, "视频初始化超时"), 30000);
      this.timer = setInterval(() => this.tick(epoch), 100);
      this.observeFrame(epoch);
      this.connect(path, epoch).catch(error => {
        if (error.name !== "AbortError" && this.current(epoch)) this.retry(epoch, "视频连接中断，正在重连");
      });
    }
    async connect(path, epoch) {
      const controller = this.controller;
      const response = await scopedFetch(path, { cache: "no-store", signal: controller.signal });
      if (!this.current(epoch)) { await response.body?.cancel(); return; }
      if (response.status === 401 || response.status === 403) {
        this.stop(); this.options.onAuthLost?.(); return;
      }
      if (response.status === 429) {
        await response.body?.cancel();
        this.limitedRetries = Math.min(this.limitedRetries + 1, 4);
        const delay = Math.min(30000, 5000 * 2 ** (this.limitedRetries - 1));
        this.retry(epoch, `其他页面正在播放此相机，或播放名额已满；${delay / 1000} 秒后重试`, delay, "limited");
        return;
      }
      if (!response.ok || !response.body) {
        await response.body?.cancel(); throw new Error("Video unavailable");
      }
      if (!this.current(epoch)) { await response.body.cancel(); return; }
      this.limitedRetries = 0;
      // Detection coordinates are only meaningful in this encoder session.
      // Missing metadata keeps video compatible, but never enables old boxes.
      const session = response.headers.get("X-Video-Session");
      this.streamSession = typeof session === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(session) ? session : null;
      this.reader = response.body.getReader();
      const chunks = []; let length = 0, initialization = null, codec = null;
      while (this.current(epoch) && !codec) {
        const item = await this.reader.read();
        if (item.done) throw new Error("Missing MP4 initialization");
        length += item.value.length;
        if (length > INIT_LIMIT) throw new Error("MP4 initialization too large");
        chunks.push(item.value); initialization = new Uint8Array(length);
        let offset = 0; for (const chunk of chunks) { initialization.set(chunk, offset); offset += chunk.length; }
        codec = avcCodec(initialization);
      }
      if (!this.current(epoch)) return;
      const mime = `video/mp4; codecs="${codec}"`;
      if (!MediaSource.isTypeSupported(mime)) {
        this.stop(); this.state("UNAVAILABLE", "unsupported", "浏览器不支持此 H.264 编码"); return;
      }
      const source = new MediaSource();
      this.source = source;
      this.url = URL.createObjectURL(source);
      const opened = new Promise((resolve, reject) => {
        const done = () => { cleanup(); resolve(); };
        const aborted = () => { cleanup(); reject(new DOMException("Stopped", "AbortError")); };
        const cleanup = () => { source.removeEventListener("sourceopen", done); controller.signal.removeEventListener("abort", aborted); };
        source.addEventListener("sourceopen", done, { once: true });
        controller.signal.addEventListener("abort", aborted, { once: true });
      });
      this.video.src = this.url;
      await opened;
      if (!this.current(epoch)) return;
      this.buffer = this.source.addSourceBuffer(mime);
      await this.append(initialization, epoch);
      while (this.current(epoch)) {
        const item = await this.reader.read();
        if (item.done) throw new Error("Video ended");
        if (item.value.length > READ_LIMIT) throw new Error("Video fragment too large");
        if (item.value.length) await this.append(item.value, epoch);
      }
    }
    retry(epoch, message, delay = 1000, phase = "reconnecting") {
      if (!this.current(epoch)) return;
      const path = this.path;
      this.stop(); this.state("WAITING_UPSTREAM", phase, message);
      const token = this.epoch;
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null;
        if (this.epoch === token && (!this.options.isCurrent || this.options.isCurrent())) this.start(path);
      }, delay);
    }
    stop() {
      this.epoch += 1;
      clearInterval(this.timer); clearTimeout(this.initTimer); clearTimeout(this.retryTimer);
      this.timer = null; this.initTimer = null; this.retryTimer = null;
      this.controller?.abort(); this.controller = null;
      this.reader?.cancel().catch(() => {}); this.reader = null;
      if (this.frameCallback != null) this.video.cancelVideoFrameCallback?.(this.frameCallback);
      this.frameCallback = null;
      if (this.waitingListener) this.video.removeEventListener?.("waiting", this.waitingListener);
      this.waitingListener = null;
      try { this.video.pause(); this.buffer?.abort(); } catch (_) {}
      this.buffer = null; this.source = null;
      this.video.removeAttribute("src"); this.video.load();
      if (this.url) URL.revokeObjectURL(this.url);
      this.url = null; this.lastFrameAt = 0; this.phase = "stopped"; this.playPending = false;
      this.streamSession = null;
    }
  }
  NativeCameraPlayer.isDrawable = video => players.get(video)?.drawable() || false;
  NativeCameraPlayer.frameVersion = video => players.get(video)?.frameVersion || 0;
  NativeCameraPlayer.decodedFps = video => players.get(video)?.decodedFps() ?? null;
  NativeCameraPlayer.bufferTarget = video => players.get(video)?.targetBuffer ?? MIN_TARGET;
  NativeCameraPlayer.avcCodec = avcCodec;
  NativeCameraPlayer.aheadOf = aheadOf;
  global.NativeCameraPlayer = NativeCameraPlayer;
})(window);
