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

ENV_FILE="${RINO_ENV_FILE:-$VAR/live.env}"
if [[ "${RINO_LOAD_LIVE_ENV:-1}" == "1" && -f "$ENV_FILE" ]]; then
  while IFS='=' read -r key value; do
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -z "${!key+x}" ]] || continue
    export "$key=$value"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE")
fi

# 単一インスタンス保証。keepalive 側にも flock はあるが、手動起動や古い
# supervisor が混ざっても run.sh 自身が二重起動を拒否する。
RUN_LOCK="${RUN_LOCK:-/tmp/rino_run.lock}"
exec 8>"$RUN_LOCK"
if ! flock -n 8; then
  log "another run.sh is already active (lock=$RUN_LOCK) -> exit"
  exit 0
fi

# content_loop を回すか（音声供給）。0 にすると playlist は外部供給前提。
RUN_CONTENT="${RUN_CONTENT:-1}"
START_VOICEVOX="${START_VOICEVOX:-1}"
VOICEVOX_START="${VOICEVOX_START:-/opt/data/scripts/voicevox-start.sh}"

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
  # run.sh の単一起動ロックは親プロセスだけが保持する。監督ループや
  # その子に継承させると、親だけ落ちた時に stale lock で復旧できない。
  exec 8>&-

  local name="$1"; shift
  local backoff=2
  local cpid=""
  trap '[[ -n "${cpid:-}" ]] && kill "$cpid" 2>/dev/null || true; exit 0' INT TERM
  while true; do
    local start; start=$(date +%s)
    log "[$name] start"
    "$@" >>"$VAR/$name.log" 2>&1 &
    cpid=$!
    wait "$cpid"; local rc=$?
    local dur=$(( $(date +%s) - start ))
    log "[$name] exited rc=$rc after ${dur}s"
    if (( dur > 60 )); then backoff=2; else backoff=$(( backoff<60 ? backoff*2 : 60 )); fi
    log "[$name] restart in ${backoff}s"; sleep "$backoff"
  done
}

log "=== run start (MODE=${MODE:-record} content=$RUN_CONTENT) ==="

if [[ "$START_VOICEVOX" == "1" && -x "$VOICEVOX_START" ]]; then
  log "[voicevox] ensure supervisor"
  ( exec 8>&-; "$VOICEVOX_START" >>"$VAR/voicevox.log" 2>&1 ) || log "[voicevox] start failed rc=$?"
fi

if [[ "$RUN_CONTENT" == "1" ]]; then
  # content_loop は低優先度で（配信の描画/エンコードを最優先にしてVOICEVOX合成に食わせない）
  supervise content nice -n 19 python3 "$ROOT/scripts/content_loop.py" & pids+=($!)
fi
supervise stream bash "$ROOT/scripts/stream.sh" & pids+=($!)

wait
