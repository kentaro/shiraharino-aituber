"use strict";
(() => {
  // src/broadcast.ts
  var CFG = {
    assetBase: "assets/shiraharino/",
    charVideo: "shiraharino_mouthless.webm",
    // 緑抜き済 透過WebM
    track: "mouth_track.json",
    mouth: { closed: "mouth/closed.png", half: "mouth/half.png", open: "mouth/open.png" },
    lip: {
      openThresh: 0.045,
      halfThresh: 0.018,
      smoothing: 0.45,
      minChangeMs: 55
    },
    playlistUrl: "segments/playlist.json",
    playlistPollMs: 8e3
  };
  var $ = (id) => document.getElementById(id);
  var charEl = $("char");
  var video = $("base-video");
  var blinkEl = $("blink");
  var mouthCanvas = $("mouth-canvas");
  var mctx = mouthCanvas.getContext("2d");
  var clockTime = $("clock-time");
  var clockDate = $("clock-date");
  var themeText = $("theme-text");
  var subtitleText = $("subtitle-text");
  var vuFill = $("vu-fill");
  var boot = $("boot");
  var bootBtn = $("boot-btn");
  var track = [];
  var trackFps = 25;
  var srcW = 1254;
  var srcH = 1254;
  var mouthImg = {};
  var mouthState = "closed";
  var lastMouthChange = 0;
  var audioCtx = null;
  var analyser = null;
  var smoothedRms = 0;
  var swayEnergy = 0;
  var started = false;
  var rafStarted = false;
  var blinkValue = 0;
  var blinkPhase = "idle";
  var nextBlinkAt = 0;
  var pendingDoubleBlink = false;
  var queue = [];
  var seenIds = /* @__PURE__ */ new Set();
  var playing = false;
  function loadImage(src) {
    return new Promise((res, rej) => {
      const im = new Image();
      im.onload = () => res(im);
      im.onerror = () => rej(new Error("img load fail: " + src));
      im.src = src;
    });
  }
  async function loadAssets() {
    const tr = await fetch(CFG.assetBase + CFG.track).then((r) => r.json());
    track = tr.frames || [];
    trackFps = tr.fps || 25;
    srcW = tr.width || 1254;
    srcH = tr.height || 1254;
    mouthImg.closed = await loadImage(CFG.assetBase + CFG.mouth.closed);
    mouthImg.half = await loadImage(CFG.assetBase + CFG.mouth.half);
    mouthImg.open = await loadImage(CFG.assetBase + CFG.mouth.open);
    video.src = CFG.assetBase + CFG.charVideo + "?v=6";
    await new Promise((res) => {
      if (video.readyState >= 2) return res();
      video.onloadeddata = () => res();
    });
  }
  function currentFrame() {
    if (!track.length) return null;
    const idx = Math.floor((video.currentTime || 0) * trackFps) % track.length;
    const f = track[idx];
    return f && f.valid ? f : null;
  }
  function updateMouthState(now) {
    if (now - lastMouthChange < CFG.lip.minChangeMs) return;
    let next;
    if (smoothedRms >= CFG.lip.openThresh) next = "open";
    else if (smoothedRms >= CFG.lip.halfThresh) next = "half";
    else next = "closed";
    if (next !== mouthState) {
      mouthState = next;
      lastMouthChange = now;
    }
  }
  function sampleAudio() {
    if (!analyser || !playing) {
      smoothedRms = 0;
      return;
    }
    const buf = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const rms = Math.sqrt(sum / buf.length);
    smoothedRms = smoothedRms * (1 - CFG.lip.smoothing) + rms * CFG.lip.smoothing;
  }
  function render() {
    const now = performance.now();
    sampleAudio();
    updateMouthState(now);
    const energy = Math.min(1, smoothedRms / CFG.lip.openThresh);
    swayEnergy += (energy - swayEnergy) * 0.02;
    const t = now * 1e-3;
    const breathe = Math.sin(t * 1.05) * 2.2;
    const scaleY = 1 + (Math.sin(t * 1.05) * 0.5 + 0.5) * 6e-3;
    const swayX = Math.sin(t * 0.62) * 2.6 * swayEnergy;
    const tilt = Math.sin(t * 0.43) * 0.5;
    charEl.style.transform = `translateX(-50%) translate(${swayX.toFixed(2)}px, ${breathe.toFixed(2)}px) rotate(${tilt.toFixed(2)}deg) scaleY(${scaleY.toFixed(4)})`;
    updateBlink(now);
    blinkEl.style.opacity = blinkValue.toFixed(3);
    mctx.clearRect(0, 0, mouthCanvas.width, mouthCanvas.height);
    const frame = currentFrame();
    if (frame) {
      const fx = mouthCanvas.width / srcW, fy = mouthCanvas.height / srcH;
      const q = frame.quad;
      const x = q[0][0] * fx, y = q[0][1] * fy;
      const w = (q[1][0] - q[0][0]) * fx;
      const h = (q[3][1] - q[0][1]) * fy;
      mctx.drawImage(mouthImg[mouthState] || mouthImg.closed, x, y, w, h);
    }
    const vu = Math.min(100, Math.round(smoothedRms * 1400));
    vuFill.style.width = vu + "%";
    requestAnimationFrame(render);
  }
  function scheduleNextBlink(now) {
    nextBlinkAt = now + 1400 + Math.random() * 2400;
  }
  function updateBlink(now) {
    const CLOSE_MS = 70;
    const OPEN_MS = 110;
    if (blinkPhase === "idle") {
      if (now >= nextBlinkAt) {
        blinkPhase = "closing";
        pendingDoubleBlink = Math.random() < 0.28;
      }
      return;
    }
    if (blinkPhase === "closing") {
      blinkValue = Math.min(1, blinkValue + 16 / CLOSE_MS);
      if (blinkValue >= 1) {
        blinkValue = 1;
        blinkPhase = "opening";
      }
      return;
    }
    if (blinkPhase === "opening") {
      blinkValue = Math.max(0, blinkValue - 16 / OPEN_MS);
      if (blinkValue <= 0) {
        blinkValue = 0;
        blinkPhase = "idle";
        if (pendingDoubleBlink) {
          pendingDoubleBlink = false;
          nextBlinkAt = now + 130;
        } else {
          scheduleNextBlink(now);
        }
      }
    }
  }
  function tickClock() {
    const now = /* @__PURE__ */ new Date();
    const p = (n) => String(n).padStart(2, "0");
    clockTime.textContent = `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
    clockDate.textContent = `${now.getFullYear()}.${p(now.getMonth() + 1)}.${p(now.getDate())}`;
  }
  async function fetchPlaylist() {
    try {
      const res = await fetch(CFG.playlistUrl + "?t=" + Date.now(), { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const segs = Array.isArray(data.segments) ? data.segments : [];
      for (const s of segs) {
        const key = s.id || s.audio || s.text || "";
        if (key && !seenIds.has(key)) {
          seenIds.add(key);
          queue.push(s);
        }
      }
    } catch {
    }
  }
  function playSegment(seg) {
    return new Promise((resolve) => {
      themeText.textContent = seg.theme || "\u30D5\u30EA\u30FC\u30C8\u30FC\u30AF";
      subtitleText.textContent = seg.text || "";
      if (!seg.audio) {
        setTimeout(resolve, 1800);
        return;
      }
      const a = new Audio(seg.audio.includes("?") ? seg.audio : seg.audio + "?t=" + Date.now());
      a.crossOrigin = "anonymous";
      try {
        const node = audioCtx.createMediaElementSource(a);
        node.connect(analyser);
        analyser.connect(audioCtx.destination);
      } catch {
      }
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        playing = false;
        smoothedRms = 0;
        resolve();
      };
      a.onended = finish;
      a.onerror = finish;
      playing = true;
      a.play().catch(() => setTimeout(finish, 400));
    });
  }
  async function playbackLoop() {
    for (; ; ) {
      if (queue.length === 0) {
        themeText.textContent = "\u5F85\u6A5F\u4E2D";
        subtitleText.textContent = "\u2026";
        mouthState = "closed";
        await new Promise((r) => setTimeout(r, 1500));
        continue;
      }
      const seg = queue.shift();
      await playSegment(seg);
      await new Promise((r) => setTimeout(r, 280));
    }
  }
  async function start() {
    if (started) return;
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.3;
    }
    try {
      await audioCtx.resume();
    } catch {
    }
    if (audioCtx.state !== "running") {
      boot.classList.remove("hidden");
      return;
    }
    started = true;
    boot.classList.add("hidden");
    video.loop = true;
    await video.play().catch(() => {
    });
    video.addEventListener("pause", () => {
      if (started) video.play().catch(() => {
      });
    });
    scheduleNextBlink(performance.now());
    if (!rafStarted) {
      rafStarted = true;
      requestAnimationFrame(render);
    }
    await fetchPlaylist();
    setInterval(fetchPlaylist, CFG.playlistPollMs);
    void playbackLoop();
  }
  async function init() {
    setInterval(tickClock, 250);
    tickClock();
    try {
      await loadAssets();
    } catch (e) {
      subtitleText.textContent = "\u30A2\u30BB\u30C3\u30C8\u8AAD\u8FBC\u30A8\u30E9\u30FC: " + e.message;
      return;
    }
    bootBtn.addEventListener("click", () => void start());
    const params = new URLSearchParams(location.search);
    if (params.get("autostart") !== "0") {
      start().catch(() => boot.classList.remove("hidden"));
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => void init());
  } else {
    void init();
  }
})();
