/* =========================================================================
 * 白羽リノ 観測スタジオ — 配信用Webページ レンダラ (TypeScript)
 *
 *  描画 (MotionPNGTuber 方式 / 軽量・滑らか):
 *    - 透過WebM(緑抜き済) を <video> でネイティブ再生（呼吸モーションは動画に内包）
 *    - 口だけを小さなオーバーレイ canvas に描画（mouth_track.json の quad に追従）
 *  口パク:
 *    - 再生中TTS音声を Web Audio で解析 → RMS → closed/half/open
 *  コンテンツ:
 *    - segments/playlist.json をポーリングし、1本ずつ「直列」に再生（重なり無し）
 *    - 新規セグメントはキュー末尾に積まれ、順番に消費される
 *  オーバーレイ: 時計 / テーマ / 字幕 / VU メーター
 * ========================================================================= */

type MouthState = "closed" | "half" | "open";

interface TrackFrame {
  quad: [[number, number], [number, number], [number, number], [number, number]];
  valid: boolean;
}
interface TrackData {
  fps: number;
  width: number;
  height: number;
  frames: TrackFrame[];
}
interface Segment {
  id: string;
  audio?: string;
  text?: string;
  theme?: string;
}
interface Playlist {
  updated?: string;
  segments: Segment[];
}

const CFG = {
  assetBase: "assets/shiraharino/",
  charVideo: "shiraharino_mouthless.webm", // 緑抜き済 透過WebM
  track: "mouth_track.json",
  mouth: { closed: "mouth/closed.png", half: "mouth/half.png", open: "mouth/open.png" },
  lip: {
    openThresh: 0.045,
    halfThresh: 0.018,
    smoothing: 0.45,
    minChangeMs: 55,
  },
  playlistUrl: "segments/playlist.json",
  playlistPollMs: 8000,
};

const $ = <T extends HTMLElement = HTMLElement>(id: string): T =>
  document.getElementById(id) as T;

const charEl = $("char");
const video = $<HTMLVideoElement>("base-video");
const blinkEl = $<HTMLImageElement>("blink");
const mouthCanvas = $<HTMLCanvasElement>("mouth-canvas");
const mctx = mouthCanvas.getContext("2d")!;
const clockTime = $("clock-time");
const clockDate = $("clock-date");
const themeText = $("theme-text");
const subtitleText = $("subtitle-text");
const vuFill = $("vu-fill");
const boot = $("boot");
const bootBtn = $("boot-btn");

// ---- 状態 ------------------------------------------------------------
let track: TrackFrame[] = [];
let trackFps = 25;
let srcW = 1254,
  srcH = 1254;
const mouthImg: Record<MouthState, HTMLImageElement> = {} as Record<MouthState, HTMLImageElement>;
let mouthState: MouthState = "closed";
let lastMouthChange = 0;

let audioCtx: AudioContext | null = null;
let analyser: AnalyserNode | null = null;
let smoothedRms = 0;
let swayEnergy = 0; // 体揺れ用にゆっくり追従するエネルギー
let started = false;
let rafStarted = false;

// まばたき
let blinkValue = 0;     // 0=開 1=閉
let blinkPhase: "idle" | "closing" | "opening" = "idle";
let nextBlinkAt = 0;
let pendingDoubleBlink = false;

// 直列再生キュー
const queue: Segment[] = [];
const seenIds = new Set<string>();
let playing = false;

// ---- ユーティリティ --------------------------------------------------
function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const im = new Image();
    im.onload = () => res(im);
    im.onerror = () => rej(new Error("img load fail: " + src));
    im.src = src;
  });
}

async function loadAssets(): Promise<void> {
  const tr: TrackData = await fetch(CFG.assetBase + CFG.track).then((r) => r.json());
  track = tr.frames || [];
  trackFps = tr.fps || 25;
  srcW = tr.width || 1254;
  srcH = tr.height || 1254;
  mouthImg.closed = await loadImage(CFG.assetBase + CFG.mouth.closed);
  mouthImg.half = await loadImage(CFG.assetBase + CFG.mouth.half);
  mouthImg.open = await loadImage(CFG.assetBase + CFG.mouth.open);
  video.src = CFG.assetBase + CFG.charVideo + "?v=6";
  await new Promise<void>((res) => {
    if (video.readyState >= 2) return res();
    video.onloadeddata = () => res();
  });
}

function currentFrame(): TrackFrame | null {
  if (!track.length) return null;
  const idx = Math.floor((video.currentTime || 0) * trackFps) % track.length;
  const f = track[idx];
  return f && f.valid ? f : null;
}

// ---- 口パク状態 ------------------------------------------------------
function updateMouthState(now: number): void {
  if (now - lastMouthChange < CFG.lip.minChangeMs) return;
  let next: MouthState;
  if (smoothedRms >= CFG.lip.openThresh) next = "open";
  else if (smoothedRms >= CFG.lip.halfThresh) next = "half";
  else next = "closed";
  if (next !== mouthState) {
    mouthState = next;
    lastMouthChange = now;
  }
}

function sampleAudio(): void {
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

// ---- 描画ループ（口オーバーレイのみ） --------------------------------
function render(): void {
  const now = performance.now();
  sampleAudio();
  updateMouthState(now);

  // 体の動き（元動画が静止のため JS で付与。要素 transform = GPU合成で滑らか）
  // ゆっくりした呼吸を常時。発話時はごく僅かに横へ揺れる程度（自然・控えめ）
  const energy = Math.min(1, smoothedRms / CFG.lip.openThresh);
  swayEnergy += (energy - swayEnergy) * 0.02; // 時定数 ~0.8s でゆっくり追従
  const t = now * 0.001;
  const breathe = Math.sin(t * 1.05) * 2.2;                 // 上下 ±2.2px（周期 ~6s）
  const scaleY = 1 + (Math.sin(t * 1.05) * 0.5 + 0.5) * 0.006; // 呼吸の伸び
  const swayX = Math.sin(t * 0.62) * 2.6 * swayEnergy;       // 横揺れ（周期 ~10s・発話時のみ）
  const tilt = Math.sin(t * 0.43) * 0.5;                     // 非常に緩い傾き
  charEl.style.transform =
    `translateX(-50%) translate(${swayX.toFixed(2)}px, ${breathe.toFixed(2)}px) ` +
    `rotate(${tilt.toFixed(2)}deg) scaleY(${scaleY.toFixed(4)})`;

  // まばたき
  updateBlink(now);
  blinkEl.style.opacity = blinkValue.toFixed(3);

  mctx.clearRect(0, 0, mouthCanvas.width, mouthCanvas.height);
  const frame = currentFrame();
  if (frame) {
    const fx = mouthCanvas.width / srcW,
      fy = mouthCanvas.height / srcH;
    const q = frame.quad;
    const x = q[0][0] * fx,
      y = q[0][1] * fy;
    const w = (q[1][0] - q[0][0]) * fx;
    const h = (q[3][1] - q[0][1]) * fy;
    mctx.drawImage(mouthImg[mouthState] || mouthImg.closed, x, y, w, h);
  }

  const vu = Math.min(100, Math.round(smoothedRms * 1400));
  vuFill.style.width = vu + "%";

  requestAnimationFrame(render);
}

// ---- まばたき（ランダム間隔・たまに二度まばたき） -------------------
function scheduleNextBlink(now: number): void {
  // 1.4〜3.8 秒のランダム間隔（人間より少しこまめに＝生き生き）
  nextBlinkAt = now + 1400 + Math.random() * 2400;
}
function updateBlink(now: number): void {
  const CLOSE_MS = 70; // 閉じる
  const OPEN_MS = 110; // 開く
  if (blinkPhase === "idle") {
    if (now >= nextBlinkAt) {
      blinkPhase = "closing";
      pendingDoubleBlink = Math.random() < 0.28; // 28% で二度まばたき
    }
    return;
  }
  if (blinkPhase === "closing") {
    blinkValue = Math.min(1, blinkValue + (16 / CLOSE_MS));
    if (blinkValue >= 1) {
      blinkValue = 1;
      blinkPhase = "opening";
    }
    return;
  }
  if (blinkPhase === "opening") {
    blinkValue = Math.max(0, blinkValue - (16 / OPEN_MS));
    if (blinkValue <= 0) {
      blinkValue = 0;
      blinkPhase = "idle";
      if (pendingDoubleBlink) {
        pendingDoubleBlink = false;
        nextBlinkAt = now + 130; // すぐもう一度
      } else {
        scheduleNextBlink(now);
      }
    }
  }
}

// ---- 時計 ------------------------------------------------------------
function tickClock(): void {
  const now = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  clockTime.textContent = `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
  clockDate.textContent = `${now.getFullYear()}.${p(now.getMonth() + 1)}.${p(now.getDate())}`;
}

// ---- playlist 取得 → キュー追記 -------------------------------------
async function fetchPlaylist(): Promise<void> {
  try {
    const res = await fetch(CFG.playlistUrl + "?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) return;
    const data: Playlist = await res.json();
    const segs = Array.isArray(data.segments) ? data.segments : [];
    for (const s of segs) {
      const key = s.id || s.audio || s.text || "";
      if (key && !seenIds.has(key)) {
        seenIds.add(key);
        queue.push(s);
      }
    }
  } catch {
    /* 未生成でも沈黙 */
  }
}

// ---- 1本を再生（Promise は再生完了で resolve） -----------------------
function playSegment(seg: Segment): Promise<void> {
  return new Promise<void>((resolve) => {
    themeText.textContent = seg.theme || "フリートーク";
    subtitleText.textContent = seg.text || "";
    if (!seg.audio) {
      setTimeout(resolve, 1800);
      return;
    }
    const a = new Audio(seg.audio.includes("?") ? seg.audio : seg.audio + "?t=" + Date.now());
    a.crossOrigin = "anonymous";
    try {
      const node = audioCtx!.createMediaElementSource(a);
      node.connect(analyser!);
      analyser!.connect(audioCtx!.destination);
    } catch {
      /* 接続済み等 */
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

// ---- 直列再生ループ（重なり無し） -----------------------------------
async function playbackLoop(): Promise<void> {
  for (;;) {
    if (queue.length === 0) {
      themeText.textContent = "待機中";
      subtitleText.textContent = "…";
      mouthState = "closed";
      await new Promise((r) => setTimeout(r, 1500));
      continue;
    }
    const seg = queue.shift()!;
    await playSegment(seg);
    await new Promise((r) => setTimeout(r, 280)); // 息継ぎ
  }
}

// ---- 起動 ------------------------------------------------------------
async function start(): Promise<void> {
  if (started) return;

  if (!audioCtx) {
    audioCtx = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.3;
  }
  try {
    await audioCtx.resume();
  } catch {
    /* ジェスチャ待ち */
  }
  if (audioCtx.state !== "running") {
    boot.classList.remove("hidden");
    return;
  }

  started = true;
  boot.classList.add("hidden");

  // 動画を確実にループ再生（停止していたら再生し直す）
  video.loop = true;
  await video.play().catch(() => {});
  video.addEventListener("pause", () => {
    if (started) video.play().catch(() => {});
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

// ---- 初期化 ----------------------------------------------------------
async function init(): Promise<void> {
  setInterval(tickClock, 250);
  tickClock();
  try {
    await loadAssets();
  } catch (e) {
    subtitleText.textContent = "アセット読込エラー: " + (e as Error).message;
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
