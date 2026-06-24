#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白羽リノ AITuber — 音声フィーダ（コンシューマ側）

  ★プロデューサ/コンシューマのキュー方式（24/7ライブ生成のための核）★
    - playlist.json の先頭セグメントを「1回だけ」再生する。
    - 再生し終えたら done.json に id を書く（プロデューサ= content_loop が
      その先頭を捨てる合図）。先頭が捨てられて次のセグメントが現れたら再生。
    - つまり「再生したものは二度と流れない」＝反復しない。常に新鮮な台本だけが流れる。

  再生は PCM(s16le/44100/stereo) を stdout にリアルタイムペースで流し、
  stream.sh が FIFO 経由で ffmpeg の音声入力に渡す。同時に nowplaying.json に
  「いま再生中のセグメントと開始時刻(epoch ms)」を書く。配信ページはそれを見て
  env(エンベロープ) で口パクする（ブラウザ側で音声を鳴らさない＝完全同期）。

環境変数:
  PLAYLIST     default: ../web/segments/playlist.json
  NOWPLAYING   default: ../web/segments/nowplaying.json
  DONE_FILE    default: ../web/segments/done.json   （再生完了idの合図）
  GAP_MS       セグメント間の無音    default: 320
  SR/CH        出力 PCM 形式         default: 44100 / 2
"""
import os, sys, json, time, subprocess, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEGDIR = os.path.join(ROOT, "web", "segments")
PLAYLIST = os.environ.get("PLAYLIST", os.path.join(SEGDIR, "playlist.json"))
NOWPLAYING = os.environ.get("NOWPLAYING", os.path.join(SEGDIR, "nowplaying.json"))
DONE_FILE = os.environ.get("DONE_FILE", os.path.join(SEGDIR, "done.json"))
GAP_MS = int(os.environ.get("GAP_MS", "320"))
SR = int(os.environ.get("SR", "44100"))
CH = int(os.environ.get("CH", "2"))
CHUNK_MS = 100
BYTES_PER_SAMPLE = 2
WEB = os.path.join(ROOT, "web")

out = sys.stdout.buffer


def now_ms():
    return int(time.time() * 1000)


def load_segments():
    try:
        return json.load(open(PLAYLIST, encoding="utf-8")).get("segments", [])
    except Exception:
        return []


def write_done(seg_id):
    tmp = DONE_FILE + ".tmp"
    json.dump({"id": seg_id, "t": now_ms()}, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, DONE_FILE)


def decode_pcm(wav_path):
    """wav -> s16le SR/CH の生PCM bytes"""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", wav_path,
                        "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "-"],
                       capture_output=True)
    return p.stdout


# ---- 先読みデコード（FIFOを途切れさせない） --------------------------------
# 直列に「デコード→再生」すると、デコード(負荷下で~0.5s)の間FIFOが空になり配信ffmpegの
# 音声入力がstall→A/Vで映像も止まる。そこで「次セグメントを再生中に裏スレッドで先読み
# デコード」しておき、切替時は即PCMを渡す＝音声無切れ。
# subprocess.run は ffmpeg 待機中GILを解放、write_paced も sleepでGILを解放するので並行する。
_pf_lock = threading.Lock()
_pf_done = {}          # id -> pcm（デコード完了済み）
_pf_running = set()    # id -> 先読み中


def _decode_into_cache(seg):
    sid = seg.get("id")
    wav = os.path.join(WEB, seg.get("audio", ""))
    pcm = decode_pcm(wav) if os.path.exists(wav) else b""
    with _pf_lock:
        _pf_done[sid] = pcm
        _pf_running.discard(sid)


def ensure_prefetch(seg):
    """seg のPCMを裏で先読み開始（未開始・未完了なら）"""
    sid = seg.get("id")
    wav = os.path.join(WEB, seg.get("audio", ""))
    if not sid or not os.path.exists(wav):
        return
    with _pf_lock:
        if sid in _pf_done or sid in _pf_running:
            return
        _pf_running.add(sid)
    threading.Thread(target=_decode_into_cache, args=(seg,), daemon=True).start()


def take_pcm(seg):
    """seg のPCMを取得。先読み済みなら即時、未完了なら少し待ち、無ければ同期デコード。"""
    sid = seg.get("id")
    wav = os.path.join(WEB, seg.get("audio", ""))
    for _ in range(40):  # 最大~0.8s 先読み完了を待つ
        with _pf_lock:
            if sid in _pf_done:
                return _pf_done.pop(sid)
            running = sid in _pf_running
        if not running:
            break
        time.sleep(0.02)
    return decode_pcm(wav) if os.path.exists(wav) else b""


def take_pcm_continuous(seg):
    """seg のPCMを取得する。デコード待ち中も無音を書き、FIFOを止めない。"""
    sid = seg.get("id")
    wav = os.path.join(WEB, seg.get("audio", ""))
    if not sid or not os.path.exists(wav):
        return b""
    ensure_prefetch(seg)
    while True:
        with _pf_lock:
            if sid in _pf_done:
                return _pf_done.pop(sid)
            running = sid in _pf_running
        if not running:
            # 先読みスレッドが例外等で消えた場合でも、同期デコードでFIFOを止めない。
            ensure_prefetch(seg)
        write_paced(silence_bytes(CHUNK_MS))


def _prune_prefetch(valid_ids):
    """playlistから消えたidの先読みキャッシュを掃除（メモリリーク防止）"""
    with _pf_lock:
        for sid in [k for k in _pf_done if k not in valid_ids]:
            _pf_done.pop(sid, None)


def write_paced(pcm):
    """PCM をリアルタイムペースで stdout に流す"""
    bytes_per_chunk = int(SR * CHUNK_MS / 1000) * CH * BYTES_PER_SAMPLE
    t = time.time()
    for i in range(0, len(pcm), bytes_per_chunk):
        try:
            out.write(pcm[i:i + bytes_per_chunk]); out.flush()
        except (BrokenPipeError, ValueError):
            raise SystemExit(0)
        t += CHUNK_MS / 1000
        dt = t - time.time()
        if dt > 0:
            time.sleep(dt)


def silence_bytes(ms):
    return b"\x00" * (int(SR * ms / 1000) * CH * BYTES_PER_SAMPLE)


def set_nowplaying(seg, dur_ms):
    tmp = NOWPLAYING + ".tmp"
    json.dump({"id": seg.get("id"), "t_start": now_ms(), "dur_ms": dur_ms,
               "theme": seg.get("theme"), "text": seg.get("text"),
               "env": seg.get("env", []), "env_dt_ms": 50},
              open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, NOWPLAYING)


def main():
    sys.stderr.write(f"[feeder] start SR={SR} CH={CH} playlist={PLAYLIST}\n")
    last_played = None
    while True:
        segs = load_segments()
        if not segs:
            write_paced(silence_bytes(300))  # キューが空 → 静かに待つ（生成待ち）
            continue
        seg = segs[0]
        sid = seg.get("id")
        if sid == last_played:
            # 直前に再生した先頭がまだ残っている＝プロデューサがまだ捨ててない。
            # 次のセグメントが現れるまで短い無音で待つ（音声は途切れさせない）。
            write_paced(silence_bytes(150))
            continue
        wav = os.path.join(WEB, seg.get("audio", ""))
        if not os.path.exists(wav):
            # 音声未生成 → このidは飛ばさず合図だけ出してプロデューサに捨てさせる
            last_played = sid
            write_done(sid)
            write_paced(silence_bytes(120))
            continue
        # 先読み済みPCMを取得。未完了でも無音でFIFOを埋めながら待つ。
        pcm = take_pcm_continuous(seg)
        dur_ms = int(len(pcm) / (SR * CH * BYTES_PER_SAMPLE) * 1000)
        set_nowplaying(seg, dur_ms)
        sys.stderr.write(f"[feeder] play {sid} ({dur_ms}ms) {seg.get('theme')} :: {seg.get('text')}\n")
        # 再生する前に「次のセグメント」を裏で先読みデコード開始（再生中に完了させる）
        if len(segs) > 1:
            ensure_prefetch(segs[1])
        _prune_prefetch({s.get("id") for s in segs})
        write_paced(pcm)
        write_paced(silence_bytes(GAP_MS))
        # 再生完了 → 合図。プロデューサがこの先頭を捨て、次の新鮮なセグメントを出す。
        last_played = sid
        write_done(sid)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
