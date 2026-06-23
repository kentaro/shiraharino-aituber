#!/usr/bin/env bash
# =========================================================================
# 白羽リノ AITuber — 配信パイプライン（単一サーバ完結・音声別経路）
#
#   映像: HTML/CSS(配信ページ follow mode) → Xvfb → chromium --kiosk
#         → ffmpeg(x11grab)
#   音声: audio_feeder.py が playlist の wav を PCM で FIFO に供給
#         → ffmpeg がそれを音声入力として多重化
#   出力: 録画(mp4) or YouTube Live(rtmps)
#
#   ★ブラウザ音声を一切キャプチャしない（PulseAudio不要）。
#     ページは nowplaying.json の env で口パクするので、映像と音声は
#     壁時計で完全同期する。Linux/Mac どちらでも動く。
#
# すべて1ホスト内で完結。OBS不要。落ちても run.sh が再起動する。
#
# 環境変数:
#   MODE         record | live           (default: record)
#   STREAM_KEY   YouTube ストリームキー   (live 時必須)
#   YT_URL       RTMPS ingest            (default: rtmps://a.rtmps.youtube.com/live2)
#   WIDTH/HEIGHT 解像度                   (default: 1280x720)
#   FPS          フレームレート           (default: 30)
#   DISPLAY_NUM  Xvfb ディスプレイ番号    (default: 99)
#   WEB_PORT     配信ページの待受ポート   (default: 8780)
#   OUT_FILE     record 時の出力          (default: var/record.mp4)
#   DURATION     record 時の尺(秒/空=無限) (default: 20)
#   CHROME       chromium バイナリパス    (default: 自動検出)
#   VBR/ABR      映像/音声ビットレート     (default: 4500k / 128k)
# =========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$ROOT/web"
VAR="$ROOT/var"; mkdir -p "$VAR"

MODE="${MODE:-record}"
WIDTH="${WIDTH:-1280}"; HEIGHT="${HEIGHT:-720}"; FPS="${FPS:-30}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
WEB_PORT="${WEB_PORT:-8780}"
OUT_FILE="${OUT_FILE:-$VAR/record.mp4}"
DURATION="${DURATION:-20}"
YT_URL="${YT_URL:-rtmps://a.rtmps.youtube.com/live2}"
STREAM_KEY="${STREAM_KEY:-}"
VBR="${VBR:-4500k}"; ABR="${ABR:-128k}"
RUN_FEEDER="${RUN_FEEDER:-1}"   # 0 にすると音声フィーダを起動しない（無音）
FIFO="$VAR/audio.fifo"

# chromium 自動検出（Playwright 同梱を優先）
CHROME="${CHROME:-}"
if [[ -z "$CHROME" ]]; then
  # Playwright は版により chrome-linux / chrome-linux64 の両方がある。新しい版を優先。
  for c in \
    /opt/data/.cache/ms-playwright/chromium-*/chrome-linux64/chrome \
    /opt/data/.cache/ms-playwright/chromium-*/chrome-linux/chrome \
    "$HOME/.cache/ms-playwright/chromium-*/chrome-linux64/chrome" \
    "$HOME/.cache/ms-playwright/chromium-*/chrome-linux/chrome" \
    "$(command -v chromium 2>/dev/null || true)" \
    "$(command -v chromium-browser 2>/dev/null || true)" \
    "$(command -v google-chrome 2>/dev/null || true)"; do
    for g in $c; do [[ -x "$g" ]] && CHROME="$g"; done
  done
fi
[[ -z "$CHROME" ]] && { echo "[stream] chromium not found. set CHROME=..." >&2; exit 1; }

PIDS=()
cleanup() {
  echo "[stream] cleanup..."
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  pkill -f "Xvfb :$DISPLAY_NUM" 2>/dev/null || true
  rm -f "$FIFO" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[stream] mode=$MODE  ${WIDTH}x${HEIGHT}@${FPS}  chrome=$CHROME"

# --- 1) 配信ページを HTTP 配信 ----------------------------------------
( cd "$WEB" && exec python3 -m http.server "$WEB_PORT" --bind 127.0.0.1 ) \
  >"$VAR/web.log" 2>&1 &
PIDS+=($!)
sleep 1

# --- 2) Xvfb 仮想ディスプレイ -----------------------------------------
Xvfb ":$DISPLAY_NUM" -screen 0 "${WIDTH}x${HEIGHT}x24" -nolisten tcp \
  >"$VAR/xvfb.log" 2>&1 &
PIDS+=($!)
export DISPLAY=":$DISPLAY_NUM"
sleep 1.5

# --- 3) 音声フィーダ（別経路）: playlist の wav を PCM で FIFO に供給 --
AUDIO_IN=( -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 )
if [[ "$RUN_FEEDER" == "1" ]]; then
  rm -f "$FIFO"; mkfifo "$FIFO"
  # フィーダは FIFO に書き込み（読み手= ffmpeg が開くまでブロック）
  ( cd "$ROOT" && SR=44100 CH=2 python3 scripts/audio_feeder.py > "$FIFO" 2>"$VAR/feeder.log" ) &
  PIDS+=($!)
  # 壁時計タイムスタンプで取り込み → x11grab(同じく壁時計)と同期する
  AUDIO_IN=( -use_wallclock_as_timestamps 1 -f s16le -ar 44100 -ac 2 -i "$FIFO" )
  echo "[stream] audio: feeder -> FIFO (PulseAudio不要)"
else
  echo "[stream] audio: (none) 無音"
fi

# --- 4) chromium --kiosk で配信ページを描画（follow mode） ------------
LIPSYNC_LAG_MS="${LIPSYNC_LAG_MS:-1800}"   # 口パク遅延(ms)。声と口を合わせる
"$CHROME" \
  --kiosk --start-fullscreen --no-first-run --no-default-browser-check \
  --disable-infobars --disable-translate --lang=ja \
  --disable-features=Translate,TranslateUI,TranslateSubFrames \
  --no-sandbox --disable-setuid-sandbox --disable-gpu-sandbox \
  --disable-dev-shm-usage --no-zygote \
  --use-gl=swiftshader --disable-gpu --mute-audio \
  --disable-crash-reporter --disable-breakpad \
  --window-size="${WIDTH},${HEIGHT}" --window-position=0,0 \
  --user-data-dir="$VAR/chrome-profile" \
  --app="http://127.0.0.1:${WEB_PORT}/index.html?follow=1&lag=${LIPSYNC_LAG_MS}" \
  >"$VAR/chrome.log" 2>&1 &
PIDS+=($!)
sleep 4

# --- 5) ffmpeg で画面(x11grab)＋音声(FIFO)をキャプチャ → 配信/録画 ----
# 同期は「ページ側の口パク遅延(LIPSYNC_LAG_MS)」で取る。映像は遅らせない
# （映像を itsoffset すると配信冒頭が黒くなるため）。
COMMON_IN=( -f x11grab -draw_mouse 0 -video_size "${WIDTH}x${HEIGHT}" -framerate "$FPS" -i ":${DISPLAY_NUM}.0"
            "${AUDIO_IN[@]}" )
COMMON_ENC=( -c:v libx264 -preset veryfast -pix_fmt yuv420p -g $((FPS*2)) -b:v "$VBR" -maxrate "$VBR" -bufsize "$VBR"
             -c:a aac -b:a "$ABR" -ar 44100 )

if [[ "$MODE" == "live" ]]; then
  [[ -z "$STREAM_KEY" ]] && { echo "[stream] live は STREAM_KEY 必須" >&2; exit 1; }
  echo "[stream] → YouTube Live (rtmps)"
  ffmpeg -hide_banner -loglevel warning "${COMMON_IN[@]}" "${COMMON_ENC[@]}" \
    -f flv "${YT_URL}/${STREAM_KEY}"
else
  echo "[stream] → record $OUT_FILE (duration=${DURATION:-inf})"
  DUR_ARG=(); [[ -n "$DURATION" ]] && DUR_ARG=( -t "$DURATION" )
  ffmpeg -hide_banner -loglevel warning -y "${COMMON_IN[@]}" "${COMMON_ENC[@]}" \
    "${DUR_ARG[@]}" "$OUT_FILE"
fi
