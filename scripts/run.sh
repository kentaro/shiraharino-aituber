#!/usr/bin/env bash
# =========================================================================
# 白羽リノ AITuber — 24/365 マスター起動（自動復帰 harness）
#
#   2つのプロセスを監督し、落ちたら指数バックオフで再起動し続ける:
#     1) content_loop.py … 台本生成(box Codec/gpt-5.5・サブスク)＋VOICEVOX合成
#     2) stream.sh        … Xvfb+chromium+ffmpeg で録画 or YouTube Live
#
#   テスト(録画):  MODE=record DURATION= ./scripts/run.sh
#   本番(配信):    MODE=live STREAM_KEY=xxxx ./scripts/run.sh
#   停止:          SIGTERM/SIGINT（systemd stop / Ctrl-C）
# =========================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAR="$ROOT/var"; mkdir -p "$VAR"
LOG="$VAR/run.log"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# content_loop を回すか（音声供給）。0 にすると playlist は外部供給前提。
RUN_CONTENT="${RUN_CONTENT:-1}"

pids=()
term() {
  log "run stopping"
  for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
  pkill -P $$ 2>/dev/null || true
  exit 0
}
trap term INT TERM

# 汎用: コマンドを落ちても再起動し続ける監督ループ
supervise() {
  local name="$1"; shift
  local backoff=2
  while true; do
    local start; start=$(date +%s)
    log "[$name] start"
    "$@" >>"$VAR/$name.log" 2>&1 &
    local cpid=$!
    wait "$cpid"; local rc=$?
    local dur=$(( $(date +%s) - start ))
    log "[$name] exited rc=$rc after ${dur}s"
    if (( dur > 60 )); then backoff=2; else backoff=$(( backoff<60 ? backoff*2 : 60 )); fi
    log "[$name] restart in ${backoff}s"; sleep "$backoff"
  done
}

log "=== run start (MODE=${MODE:-record} content=$RUN_CONTENT) ==="

if [[ "$RUN_CONTENT" == "1" ]]; then
  supervise content python3 "$ROOT/scripts/content_loop.py" & pids+=($!)
fi
supervise stream bash "$ROOT/scripts/stream.sh" & pids+=($!)

wait
