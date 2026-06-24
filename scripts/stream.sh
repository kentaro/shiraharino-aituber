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
#   WIDTH/HEIGHT 解像度                   (default: 540x960 portrait)
#   FPS          フレームレート           (default: 12)
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

# stream.sh 自身でも単一インスタンスを保証する。run.sh/keepalive の
# supervisor が重複しても、YouTube ingest はここで1本に抑える。
STREAM_LOCK="${STREAM_LOCK:-/tmp/rino_stream.lock}"
exec 7>"$STREAM_LOCK"
if ! flock -n 7; then
  echo "[stream] another stream.sh is already active (lock=$STREAM_LOCK) -> exit" >&2
  exit 0
fi

MODE="${MODE:-record}"
WIDTH="${WIDTH:-540}"; HEIGHT="${HEIGHT:-960}"; FPS="${FPS:-12}"
PRESET="${PRESET:-ultrafast}"   # 低スペック箱向け（CPU節約）
RENDER_FPS="${RENDER_FPS:-$FPS}" # ブラウザ描画fps。YouTube送出fpsとは分離する。
BODY_MOTION="${BODY_MOTION:-0}"   # 1 なら配信画面の全身揺れを有効化する。
DISPLAY_NUM="${DISPLAY_NUM:-99}"
WEB_PORT="${WEB_PORT:-8780}"
OUT_FILE="${OUT_FILE:-$VAR/record.mp4}"
DURATION="${DURATION:-20}"
YT_URL="${YT_URL:-rtmps://a.rtmps.youtube.com/live2}"
STREAM_KEY="${STREAM_KEY:-}"
VBR="${VBR:-2800k}"; ABR="${ABR:-128k}"
if [[ -z "${VIDEO_BUFSIZE:-}" ]]; then
  if [[ "$VBR" =~ ^([0-9]+)k$ ]]; then
    VIDEO_BUFSIZE="$((BASH_REMATCH[1] * 2))k"
  else
    VIDEO_BUFSIZE="$VBR"
  fi
fi
VIDEO_QUEUE_SIZE="${VIDEO_QUEUE_SIZE:-180}"
AUDIO_QUEUE_SIZE="${AUDIO_QUEUE_SIZE:-256}"
KEYINT_SECONDS="${KEYINT_SECONDS:-2}"
RUN_FEEDER="${RUN_FEEDER:-1}"   # 0 にすると音声フィーダを起動しない（無音）
FIFO="$VAR/audio.fifo"
CHROME_NICE="${CHROME_NICE:-15}"
SNAPSHOT_INTERVAL="${SNAPSHOT_INTERVAL:-20}"

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
  # キーパーループの子（chromium/feeder）も確実に始末（重複ingest防止）
  pkill -9 -f "chrome-[p]rofile" 2>/dev/null || true
  pkill -9 -f "audio_[f]eeder" 2>/dev/null || true
  pkill -9 -f "X[v]fb :$DISPLAY_NUM" 2>/dev/null || true
  pkill -9 -f "http.server ${WEB_PORT%?}[${WEB_PORT: -1}]" 2>/dev/null || true
  rm -f "$FIFO" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[stream] mode=$MODE  ${WIDTH}x${HEIGHT}@${FPS}  chrome=$CHROME"

# --- 0) 起動前に前インスタンスの残骸を一掃（重複ingest/重複描画を絶対防止）---
pkill -9 -f "rtmp.*y[o]utube" 2>/dev/null || true
pkill -9 -f "chrome-[p]rofile" 2>/dev/null || true
pkill -9 -f "audio_[f]eeder" 2>/dev/null || true
pkill -9 -f "x11[g]rab" 2>/dev/null || true
pkill -9 -f "X[v]fb :$DISPLAY_NUM" 2>/dev/null || true
pkill -9 -f "http.server ${WEB_PORT%?}[${WEB_PORT: -1}]" 2>/dev/null || true
sleep 1

# 起動したコードの git 版を記録（keepalive の版チェック用）
if [[ -n "${SNAPSHOT_DIR:-}" ]]; then
  mkdir -p "$SNAPSHOT_DIR"
  git -C "$ROOT" rev-parse --short HEAD 2>/dev/null > "$SNAPSHOT_DIR/running_git" || true
fi

# --- 1) 配信ページを HTTP 配信 ----------------------------------------
( exec 7>&-; cd "$WEB" && exec python3 -m http.server "$WEB_PORT" --bind 127.0.0.1 ) \
  >"$VAR/web.log" 2>&1 &
PIDS+=($!)
sleep 1

# --- 2) Xvfb 仮想ディスプレイ -----------------------------------------
( exec 7>&-; Xvfb ":$DISPLAY_NUM" -screen 0 "${WIDTH}x${HEIGHT}x24" -nolisten tcp ) \
  >"$VAR/xvfb.log" 2>&1 &
PIDS+=($!)
export DISPLAY=":$DISPLAY_NUM"
sleep 1.5

# --- 3) 音声フィーダ（別経路）: playlist の wav を PCM で FIFO に供給 --
AUDIO_IN=( -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 )
if [[ "$RUN_FEEDER" == "1" ]]; then
  rm -f "$FIFO"; mkfifo "$FIFO"
  # フィーダは FIFO に書き込み（読み手= ffmpeg が開くまでブロック）。
  # -u でアンバッファ化し PCM を確実に流す。落ちても再起動。
  ( exec 7>&-; cd "$ROOT"; while true; do SR=44100 CH=2 python3 -u scripts/audio_feeder.py > "$FIFO" 2>>"$VAR/feeder.log"; echo "[feeder] exited -> restart" >>"$VAR/feeder.log"; sleep 1; done ) &
  PIDS+=($!)
  # 生PCMはサンプル数でタイムスタンプ（rtmp向けに単調）。同期はページ側の lag で取る
  AUDIO_IN=( -thread_queue_size "$AUDIO_QUEUE_SIZE" -re -f s16le -ar 44100 -ac 2 -i "$FIFO" )
  echo "[stream] audio: feeder -> FIFO (PulseAudio不要)"
else
  echo "[stream] audio: (none) 無音"
fi

# --- 4) chromium --kiosk で配信ページを描画（follow mode・自動再起動） --
# ソフトGLで chromium がたまにクラッシュしても配信を止めないよう、
# キーパーループで落ちたら即再起動する（ffmpeg はそのまま流し続ける）。
LIPSYNC_LAG_MS="${LIPSYNC_LAG_MS:-1800}"   # 口パク遅延(ms)。声と口を合わせる
( exec 7>&-; while true; do
    # 毎回フレッシュなプロフィールで起動（クラッシュ後の「profile error」ダイアログを防ぐ）
    rm -rf "$VAR/chrome-profile" 2>/dev/null
    nice -n "$CHROME_NICE" "$CHROME" \
      --kiosk --start-fullscreen --no-first-run --no-default-browser-check \
      --disable-infobars --disable-translate --lang=ja \
      --disable-features=Translate,TranslateUI,TranslateSubFrames,CalculateNativeWinOcclusion \
      --disable-backgrounding-occluded-windows \
      --no-sandbox --disable-setuid-sandbox --disable-gpu-sandbox \
      --disable-dev-shm-usage \
      --disable-gpu --disable-gpu-compositing --disable-software-rasterizer \
      --disable-accelerated-2d-canvas --disable-accelerated-video-decode \
      --disable-accelerated-video-encode --mute-audio \
      --disable-background-networking --disable-sync --disable-extensions \
      --disable-default-apps --disable-renderer-backgrounding \
      --disable-background-timer-throttling --autoplay-policy=no-user-gesture-required \
      --disable-crash-reporter --disable-breakpad \
      --disable-session-crashed-bubble --hide-crash-restore-bubble \
      --noerrdialogs --disable-component-update \
      --window-size="${WIDTH},${HEIGHT}" --window-position=0,0 \
      --user-data-dir="$VAR/chrome-profile" \
      --app="http://127.0.0.1:${WEB_PORT}/index.html?follow=1&lag=${LIPSYNC_LAG_MS}&rfps=${RENDER_FPS}&motion=${BODY_MOTION}" \
      >"$VAR/chrome.log" 2>&1
    echo "[stream] chromium exited rc=$? -> relaunch" >&2
    sleep 2
  done ) &
PIDS+=($!)
sleep 5

# --- 4.5) スナップショット（SNAPSHOT_DIR 指定時、配信中の画面を定期保存）---
# 配信中の見え方を URL で確認できるようにする（運用・デバッグ用）。
if [[ -n "${SNAPSHOT_DIR:-}" ]]; then
  mkdir -p "$SNAPSHOT_DIR"
  ( exec 7>&-; while true; do
      ffmpeg -y -loglevel error -f x11grab -draw_mouse 0 -video_size "${WIDTH}x${HEIGHT}" -i ":${DISPLAY_NUM}.0" \
        -frames:v 1 "$SNAPSHOT_DIR/frame.jpg" 2>/dev/null
      sleep "$SNAPSHOT_INTERVAL"
    done ) &
  PIDS+=($!)
fi

# --- 5) ffmpeg で画面(x11grab)＋音声(FIFO)をキャプチャ → 配信/録画 ----
# 同期は「ページ側の口パク遅延(LIPSYNC_LAG_MS)」で取る。映像は遅らせない
# （映像を itsoffset すると配信冒頭が黒くなるため）。
# BGM（ローファイ）を声の下にループ・低音量でミックス。BGM_FILE があれば有効。
BGM_FILE="${BGM_FILE:-$WEB/assets/bgm/lofi_loop.mp3}"
BGM_VOL="${BGM_VOL:-0.13}"
BGM_IN=(); AUDIO_MAP=( -map 0:v:0 -map 1:a:0 )
if [[ -f "$BGM_FILE" ]]; then
  # -re 必須: ファイル入力を実時間で読む。無いと最速デコードして muxer に溢れ
  # 「エンコーダがリアルタイムより高速」エラー＝バースト送出になる。
  BGM_IN=( -re -stream_loop -1 -i "$BGM_FILE" )   # input2 = BGM(無限ループ)
  AUDIO_MAP=( -filter_complex
    "[1:a]aresample=44100[v];[2:a]aresample=44100,volume=${BGM_VOL}[b];[v][b]amix=inputs=2:duration=first:normalize=0[aout]"
    -map 0:v:0 -map "[aout]" )
  echo "[stream] BGM: $BGM_FILE (vol=$BGM_VOL)"
fi
COMMON_IN=( -thread_queue_size "$VIDEO_QUEUE_SIZE"
            -f x11grab -draw_mouse 0 -video_size "${WIDTH}x${HEIGHT}" -framerate "$FPS" -i ":${DISPLAY_NUM}.0"
            "${AUDIO_IN[@]}" "${BGM_IN[@]}" )
COMMON_ENC=( "${AUDIO_MAP[@]}"
             # YouTube Live は低遅延RTMPより、固定fpsと安定したVBV/GOPの通常ペーシングを優先する。
             -fps_mode cfr -r "$FPS" -max_muxing_queue_size 1024
             -c:v libx264 -preset "$PRESET" -pix_fmt yuv420p
             -g $((FPS*KEYINT_SECONDS)) -keyint_min "$FPS" -sc_threshold 0 -bf 2
             -b:v "$VBR" -maxrate "$VBR" -bufsize "$VIDEO_BUFSIZE"
             -c:a aac -b:a "$ABR" -ar 44100 -ac 2 )

if [[ "$MODE" == "live" ]]; then
  [[ -z "$STREAM_KEY" ]] && { echo "[stream] live は STREAM_KEY 必須" >&2; exit 1; }
  # この ffmpeg 以外に YouTube へ送出している ffmpeg を確実に消す（単一ingest保証）
  pkill -9 -f "rtmp.*y[o]utube" 2>/dev/null || true; sleep 1
  echo "[stream] → YouTube Live: ${YT_URL}"
  ffmpeg -hide_banner -loglevel warning -stats_period 5 -progress "$VAR/ffmpeg.progress" \
    "${COMMON_IN[@]}" "${COMMON_ENC[@]}" \
    -f flv "${YT_URL}/${STREAM_KEY}"
else
  echo "[stream] → record $OUT_FILE (duration=${DURATION:-inf})"
  DUR_ARG=(); [[ -n "$DURATION" ]] && DUR_ARG=( -t "$DURATION" )
  ffmpeg -hide_banner -loglevel warning -y "${COMMON_IN[@]}" "${COMMON_ENC[@]}" \
    "${DUR_ARG[@]}" "$OUT_FILE"
fi
