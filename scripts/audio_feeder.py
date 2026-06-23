#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白羽リノ AITuber — 音声フィーダ（別経路ミックスの音声側）

  playlist.json を順番に再生し、PCM(s16le/44100/stereo) を stdout に
  リアルタイムペースで流す。stream.sh がこれを FIFO 経由で ffmpeg の
  音声入力に渡す。同時に web/segments/nowplaying.json に
  「いま再生中のセグメントと開始時刻(epoch ms)」を書く。

  配信ページは nowplaying.json を見て、そのセグメントの env(エンベロープ)で
  口パクする（ブラウザ側で音声を鳴らさない＝音声キャプチャ不要・OS非依存・完全同期）。

  音声(このフィーダ) と 映像(x11grab) はどちらも壁時計に従うため同期する。

環境変数:
  PLAYLIST     default: ../web/segments/playlist.json
  NOWPLAYING   default: ../web/segments/nowplaying.json
  GAP_MS       セグメント間の無音    default: 280
  SR/CH        出力 PCM 形式         default: 44100 / 2
"""
import os, sys, json, time, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEGDIR = os.path.join(ROOT, "web", "segments")
PLAYLIST = os.environ.get("PLAYLIST", os.path.join(SEGDIR, "playlist.json"))
NOWPLAYING = os.environ.get("NOWPLAYING", os.path.join(SEGDIR, "nowplaying.json"))
GAP_MS = int(os.environ.get("GAP_MS", "280"))
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


def decode_pcm(wav_path):
    """wav -> s16le SR/CH の生PCM bytes"""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", wav_path,
                        "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "-"],
                       capture_output=True)
    return p.stdout


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
    idx = 0
    while True:
        segs = load_segments()
        if not segs:
            write_paced(silence_bytes(500))
            continue
        if idx >= len(segs):
            idx = 0  # 末尾まで来たら先頭から（content_loop が追記し続ける想定）
        seg = segs[idx]; idx += 1
        wav = os.path.join(WEB, seg.get("audio", ""))
        if not os.path.exists(wav):
            continue
        pcm = decode_pcm(wav)
        dur_ms = int(len(pcm) / (SR * CH * BYTES_PER_SAMPLE) * 1000)
        if os.environ.get("FEEDER_T0_FILE") and not getattr(main, "_t0", False):
            open(os.environ["FEEDER_T0_FILE"], "w").write(str(now_ms()))
            main._t0 = True
        set_nowplaying(seg, dur_ms)
        sys.stderr.write(f"[feeder] play {seg.get('id')} ({dur_ms}ms) {seg.get('theme')}\n")
        write_paced(pcm)
        write_paced(silence_bytes(GAP_MS))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
