#!/usr/bin/env bash
# =========================================================================
# box 用 自己修復 keepalive デーモン（git 経由で配布＝relay破損を受けない）
#
#   ★単一インスタンス保証の要：
#     flock を「デーモンの生涯ずっと保持」する。よって2本目を起動しても
#     flock -n が即失敗して何もせず終了する＝supervisorが二重に立つことが
#     物理的に起きない（重複ingest・暴走の根絶）。
#
#   20秒ごとに健全性を確認し、落ちていれば残骸を一掃して1本だけ起動する。
#   死活判定はスナップショット(frame.jpg)の鮮度で行う（pgrepのnamespace問題回避）。
#   - 直近40秒以内にフレーム更新あり かつ コード最新 → 何もしない
#   - 起動直後(75秒未満) → 立ち上げ中として待つ
#   - それ以外（落ちている/版が古い）→ 全部killして1本だけ起動
#
#   起動方法（ブートストラップ rino_launch.sh から）:  bash box_keepalive.sh
#   多重起動しても安全（2本目以降は即exit）。
# =========================================================================
set -uo pipefail
REPO=/opt/data/home/shiraharino-aituber
SNAP=/opt/data/home/MotionPNGTuber_Player/live_snap
VOICEVOX_START=/opt/data/scripts/voicevox-start.sh
mkdir -p "$SNAP"

# --- 単一インスタンス: このロックをデーモン稼働中ずっと保持する -------------
exec 9>/tmp/rino_keepalive.lock
if ! flock -n 9; then
  echo "$(date '+%F %T') keepalive already running -> exit" >> "$SNAP/keepalive.log"
  exit 0
fi
echo "$(date '+%F %T') keepalive daemon start pid=$$" >> "$SNAP/keepalive.log"

# 子の配信を道連れにしないため、TERM受領時は run.sh ツリーごと畳む
cleanup() {
  echo "$(date '+%F %T') keepalive stopping" >> "$SNAP/keepalive.log"
  pkill -9 -f "$REPO/scripts/r[u]n.sh" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

launch_once() {
  echo "$(date '+%T') down/stale -> restart" >> "$SNAP/keepalive.log"
  # 残骸を一掃（run.sh監督ツリー・配信・ブラウザ・音声を全部）
  for p in "$REPO/scripts/r[u]n.sh" "$REPO/scripts/s[t]ream.sh" "audio_[f]eeder" \
           "x11[g]rab" "X[v]fb :99" "chrome-[p]rofile" "rtmp.*y[o]utube" "http.server 878[0]"; do
    pkill -9 -f "$p" 2>/dev/null || true
  done
  pkill -9 -x ffmpeg 2>/dev/null || true
  rm -f "$REPO/var/audio.fifo" 2>/dev/null || true
  sleep 3
  cd "$REPO" || return 1
  set -a; [ -f var/live.env ] && . var/live.env; set +a
  date +%s > "$SNAP/launched_at"
  MODE=live RUN_CONTENT="${RUN_CONTENT:-0}" SNAPSHOT_DIR="$SNAP" \
    setsid bash scripts/run.sh > var/live.log 2>&1 < /dev/null &
  echo "$(date '+%T') git=$(git rev-parse --short HEAD 2>/dev/null) launched pid=$!" >> "$SNAP/keepalive.log"
}

ensure_voicevox() {
  if [ -x "$VOICEVOX_START" ]; then
    "$VOICEVOX_START" >> "$SNAP/voicevox.log" 2>&1 || \
      echo "$(date '+%F %T') voicevox ensure failed rc=$?" >> "$SNAP/keepalive.log"
  fi
}

# --- 監視ループ（このプロセスは常駐し、ロックを離さない） -----------------
while true; do
  now=$(date +%s)

  # 最新コードを取得。runtime 生成物を巻き戻さないため、remote HEAD が
  # 進んだ時だけ reset --hard する（20秒ごとの無条件 reset は禁止）。
  cd "$REPO" 2>/dev/null && git fetch -q origin 2>/dev/null
  LOCAL_FULL=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo x)
  REMOTE_FULL=$(git -C "$REPO" rev-parse origin/main 2>/dev/null || echo x)
  if [ "$LOCAL_FULL" != "$REMOTE_FULL" ] && [ "$REMOTE_FULL" != "x" ]; then
    git -C "$REPO" reset -q --hard origin/main 2>/dev/null
  fi
  LATEST=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo x)
  RUNNING=$(cat "$SNAP/running_git" 2>/dev/null || echo none)
  ensure_voicevox

  healthy=0
  if [ -f "$SNAP/frame.jpg" ]; then
    age=$(( now - $(stat -c %Y "$SNAP/frame.jpg" 2>/dev/null || echo 0) ))
    [ "$age" -lt 40 ] && [ "$LATEST" = "$RUNNING" ] && healthy=1
  fi

  if [ "$healthy" = "1" ]; then
    :   # 健全 → 触らない
  elif [ -f "$SNAP/launched_at" ] && [ "$(( now - $(cat "$SNAP/launched_at" 2>/dev/null || echo 0) ))" -lt 75 ]; then
    :   # 起動直後 → 立ち上げ待ち
  else
    launch_once
  fi

  sleep 20
done
