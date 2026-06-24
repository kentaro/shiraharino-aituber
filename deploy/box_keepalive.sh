#!/usr/bin/env bash
# =========================================================================
# box 用 自己修復 keepalive（git 経由で配布＝relay破損を受けない）
#
#   死活判定はスナップショットの鮮度で行う（pgrep の namespace 問題を回避）。
#   - 直近40秒以内にフレーム更新があれば健全 → 何もしない
#   - 起動直後(75秒未満)は立ち上げ中 → 待つ
#   - それ以外（落ちている）→ 残骸を全部kill してから 1本だけ起動
#
#   これにより「単一インスタンス」を保証し、重複ingest/暴走を防ぐ。
#   cron は 1分毎にこれを呼ぶ（ASCIIブートストラップ経由）。
# =========================================================================
set -uo pipefail
REPO=/opt/data/home/shiraharino-aituber
SNAP=/opt/data/home/MotionPNGTuber_Player/live_snap
mkdir -p "$SNAP"

# 同時実行を物理的に1つに絞る（二重起動・重複ingestを根絶）
exec 9>/tmp/rino_keepalive.lock
flock -n 9 || exit 0

now=$(date +%s)

# 最新コードを取得（git=整合性保証で relay 破損を受けない）
cd "$REPO" 2>/dev/null && git fetch -q origin 2>/dev/null && git reset -q --hard origin/main 2>/dev/null
LATEST=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo x)
RUNNING=$(cat "$SNAP/running_git" 2>/dev/null || echo none)

# 0) 配信中だが「コード版が古い」→ 最新へ入れ替えるため強制再起動
if [ "$LATEST" != "$RUNNING" ] && [ -f "$SNAP/launched_at" ]; then
  la=$(( now - $(cat "$SNAP/launched_at" 2>/dev/null || echo 0) ))
  if [ "$la" -ge 75 ]; then
    echo "$(date '+%T') version $RUNNING -> $LATEST, restart" >> "$SNAP/keepalive.log"
    rm -f "$SNAP/frame.jpg"   # 健全判定を外して下の再起動へ進める
  fi
fi

# 1) 健全（スナップショットが新しい かつ コード版も最新）→ 触らない
if [ -f "$SNAP/frame.jpg" ] && [ "$LATEST" = "$RUNNING" ]; then
  age=$(( now - $(stat -c %Y "$SNAP/frame.jpg" 2>/dev/null || echo 0) ))
  [ "$age" -lt 40 ] && exit 0
fi
# 2) 起動直後 → 立ち上げ待ち
if [ -f "$SNAP/launched_at" ]; then
  la=$(( now - $(cat "$SNAP/launched_at" 2>/dev/null || echo 0) ))
  [ "$la" -lt 75 ] && exit 0
fi

# 3) 落ちている → 残骸を一掃して1本だけ起動
echo "$(date '+%T') down -> restart" >> "$SNAP/keepalive.log"
for p in "scripts/run.sh" "scripts/stream.sh" "audio_feeder" "x11grab" "Xvfb :99" \
         "chrome-profile" "rtmp.*youtube" "http.server 8780"; do
  pkill -9 -f "$p" 2>/dev/null || true
done
rm -f "$REPO/var/audio.fifo" 2>/dev/null || true
sleep 3
cd "$REPO" || exit 1
set -a; [ -f var/live.env ] && . var/live.env; set +a
echo "$now" > "$SNAP/launched_at"
MODE=live RUN_CONTENT="${RUN_CONTENT:-0}" SNAPSHOT_DIR="$SNAP" \
  setsid bash scripts/run.sh > var/live.log 2>&1 < /dev/null &
echo "$(date '+%T') git=$(git rev-parse --short HEAD 2>/dev/null) launched pid=$!" >> "$SNAP/keepalive.log"
