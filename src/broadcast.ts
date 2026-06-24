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
const charBase = $<HTMLImageElement>("char-base");
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
const standbyEl = $("standby");
const subtitleBox = $("subtitle");

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

// follow mode（別経路ミックス用）: 音声は外部フィーダが鳴らし、
// 配信ページは nowplaying.json の env で口パクする（音声再生・解析なし）
const followMode = new URLSearchParams(location.search).get("follow") === "1";
// 3コアboxでは大きなキャラ画像(700px)のtransformを毎描画更新すると、
// headless Chromium のソフト合成がCPUを食い切る。通常配信は口パクと
// まばたきだけを動かし、全身の揺れは明示指定時だけ有効化する。
const bodyMotion = new URLSearchParams(location.search).get("motion") === "1";
// 口パク遅延(ms)。音声は フィーダ→FIFO→ffmpeg のパイプライン分だけ遅れて
// 配信に乗るので、口パクを同じだけ遅らせて声と一致させる（?lag=1800 等）。
const lipsyncLagMs = parseInt(new URLSearchParams(location.search).get("lag") || "0", 10) || 0;
interface NowPlaying {
  id: string;
  t_start: number;   // epoch ms
  dur_ms: number;
  theme?: string;
  text?: string;
  env: number[];
  env_dt_ms: number;
}
let np: NowPlaying | null = null;
let npId = "";

// まばたき（壁時計ベース＝負荷でフレームが間引かれても閉じっぱなしにならない）
let blinkValue = 0;     // 0=開 1=閉
let blinkStart = -1;    // まばたき開始時刻(ms)。-1=開いてidle
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
  // 静止透過PNG（元動画はほぼ静止のため動画デコードを廃止＝軽量）
  if (!charBase.complete) {
    await new Promise<void>((res) => { charBase.onload = () => res(); charBase.onerror = () => res(); });
  }
}

// 口の位置はほぼ一定なので track の最初の有効フレームの quad を使う
function currentFrame(): TrackFrame | null {
  if (!track.length) return null;
  for (const f of track) if (f && f.valid) return f;
  return null;
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

// follow mode: nowplaying.json の env を t_start からの経過時間で引く
function sampleEnvelope(): void {
  let rms = 0;
  if (np && np.env && np.env.length) {
    const pos = Date.now() - np.t_start - lipsyncLagMs;
    if (pos >= 0 && pos < np.dur_ms) {
      const i = Math.floor(pos / (np.env_dt_ms || 50));
      rms = np.env[Math.min(i, np.env.length - 1)] || 0;
    }
  }
  smoothedRms = smoothedRms * (1 - CFG.lip.smoothing) + rms * CFG.lip.smoothing;
}

async function pollNowplaying(): Promise<void> {
  try {
    const res = await fetch("segments/nowplaying.json?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) return;
    const data: NowPlaying = await res.json();
    np = data;
    if (data.id !== npId) {
      npId = data.id;
      // 字幕/テーマは口パク・音声と同じだけ遅らせて出す（nowplaying は配信パイプラインより
      // lipsyncLagMs ぶん先行しているため。遅らせないとテロップだけ先に変わってズレる）。
      const theme = data.theme || "フリートーク";
      const text = data.text || "";
      setTimeout(() => {
        themeText.textContent = theme;
        subtitleText.textContent = text;
        if (text) { // 喋り始めたら待機画面→字幕に切り替え
          standbyEl.classList.add("hidden");
          subtitleBox.classList.remove("hidden");
        }
      }, lipsyncLagMs);
    }
  } catch {
    /* フィーダ未起動でも沈黙 */
  }
}

// ---- 描画ループ（口オーバーレイのみ） --------------------------------
// 描画を一定fpsに間引く。ヘッドレスchromiumのソフトウェア描画は重く、毎フレーム
// (最大60fps)で口キャンバスを再描画すると3コア箱のCPUを食い潰し、ffmpegがリアルタイムに
// 追いつけず「YouTubeの受信動画が少ない/バッファ」になる。配信は15fpsなので描画も15fpsで十分。
const DRAW_FPS = parseInt(new URLSearchParams(location.search).get("rfps") || "15", 10) || 15;
const DRAW_INTERVAL = 1000 / DRAW_FPS;
let lastDraw = 0;

function render(): void {
  requestAnimationFrame(render);
  const now = performance.now();
  // 間引き: 前回描画から所定間隔未満なら重い処理をスキップ（rAF自体は軽い）
  if (now - lastDraw < DRAW_INTERVAL - 2) return;
  lastDraw = now;
  if (followMode) sampleEnvelope();
  else sampleAudio();
  updateMouthState(now);

  if (bodyMotion) {
    // 体の動き（元動画が静止のため JS で付与）。box以外のプレビュー用。
    const energy = Math.min(1, smoothedRms / CFG.lip.openThresh);
    swayEnergy += (energy - swayEnergy) * 0.02;
    const t = now * 0.001;
    const breathe = Math.sin(t * 1.05) * 2.2;
    const scaleY = 1 + (Math.sin(t * 1.05) * 0.5 + 0.5) * 0.006;
    const swayX = Math.sin(t * 0.62) * 2.6 * swayEnergy;
    const tilt = Math.sin(t * 0.43) * 0.5;
    charEl.style.transform =
      `translateX(-50%) translate(${swayX.toFixed(2)}px, ${breathe.toFixed(2)}px) ` +
      `rotate(${tilt.toFixed(2)}deg) scaleY(${scaleY.toFixed(4)})`;
  }

  // まばたき。opacityは二値化（中間値で開き目が透けるのを防ぐ＝閉じてる間は完全不透明）
  updateBlink(now);
  blinkEl.style.opacity = blinkValue > 0.35 ? "1" : "0";

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
}

// ---- まばたき（ランダム間隔・たまに二度まばたき） -------------------
function scheduleNextBlink(now: number): void {
  // 1.4〜3.8 秒のランダム間隔（人間より少しこまめに＝生き生き）
  nextBlinkAt = now + 1400 + Math.random() * 2400;
}
function updateBlink(now: number): void {
  const CLOSE_MS = 70;  // 閉じる
  const OPEN_MS = 110;  // 開く
  const TOTAL = CLOSE_MS + OPEN_MS;
  // まばたき中でない → 時刻が来たら開始
  if (blinkStart < 0) {
    blinkValue = 0;
    if (now >= nextBlinkAt) {
      blinkStart = now;
      pendingDoubleBlink = Math.random() < 0.28; // 28% で二度まばたき
    }
    return;
  }
  // まばたき中：開始からの経過“実時間”で開閉量を決める（フレーム数に依存しない）。
  // 描画が間引かれて次の呼び出しが遅れても、その時の経過時間で正しい位置に飛ぶ＝
  // 「閉じた状態でストール」しない。経過が尺を超えていれば必ず開いてidleへ戻す。
  const e = now - blinkStart;
  if (e >= TOTAL) {
    blinkValue = 0;
    blinkStart = -1;
    if (pendingDoubleBlink) {
      pendingDoubleBlink = false;
      nextBlinkAt = now + 130; // すぐもう一度
    } else {
      scheduleNextBlink(now);
    }
  } else if (e < CLOSE_MS) {
    blinkValue = e / CLOSE_MS;          // 0→1（閉じる）
  } else {
    blinkValue = 1 - (e - CLOSE_MS) / OPEN_MS; // 1→0（開く）
  }
}

// ---- 時計 ------------------------------------------------------------
// 配信サーバのTZ(UTC等)に依存せず、常に日本時間(JST)で表示する
const jstTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const jstDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
});
function tickClock(): void {
  const now = new Date();
  clockTime.textContent = jstTime.format(now);
  clockDate.textContent = jstDate.format(now).replace(/-/g, ".");
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
    // 別経路ミックス（映像キャプチャ＋wav音声）用に再生開始時刻を記録
    console.log(`__SEG__ ${seg.id} ${Date.now()} ${seg.audio || ""}`);
    await playSegment(seg);
    await new Promise((r) => setTimeout(r, 280)); // 息継ぎ
  }
}

// ---- 起動（follow mode: 音声は外部フィーダ。ページは env で口パク） ----
async function startFollow(): Promise<void> {
  if (started) return;
  started = true;
  boot.classList.add("hidden");
  // 起動直後は待機画面を出す（字幕は隠す）。喋り始めたら切り替わる
  standbyEl.classList.remove("hidden");
  subtitleBox.classList.add("hidden");
  scheduleNextBlink(performance.now());
  if (!rafStarted) {
    rafStarted = true;
    requestAnimationFrame(render);
  }
  await pollNowplaying();
  setInterval(pollNowplaying, 120);
}

// ---- 起動（標準: ページ自身が音声を再生して口パク・プレビュー/pulse用） ----
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
  // 埋め込みフォントの読込を待ってから描画開始（フォールバック表示を防ぐ）
  try {
    await (document as Document & { fonts: { ready: Promise<unknown> } }).fonts.ready;
  } catch {
    /* fonts API 無し環境でも続行 */
  }
  try {
    await loadAssets();
  } catch (e) {
    subtitleText.textContent = "アセット読込エラー: " + (e as Error).message;
    return;
  }
  bootBtn.addEventListener("click", () => void start());

  // follow mode は音声を鳴らさないので自動再生制限に掛からない → 即起動
  if (followMode) {
    void startFollow();
    return;
  }

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
