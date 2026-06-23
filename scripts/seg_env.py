#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
セグメント音声のエンベロープ計算（口パク用・共有モジュール）

  WAV(PCM 16bit) を読み、ENV_DT ごとの RMS 配列を返す。
  配信ページは「いま再生中のセグメント」の env を t_start からの経過で引いて
  口形状を決める（ブラウザ側で音声を解析しない＝音声キャプチャ不要・OS非依存）。

  audioop は Python 3.13 で削除されたため、純 stdlib(array) で実装する。
"""
import wave, array, math

ENV_DT_MS = 50  # エンベロープの時間刻み


def wav_envelope(path):
    """WAVファイル(16bit PCM) -> (env: list[float 0..~1], dur_ms: int)"""
    with wave.open(path, "rb") as w:
        sw = w.getsampwidth()
        ch = w.getnchannels()
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    dur_ms = int(n / sr * 1000) if sr else 0
    if sw != 2:
        # 16bit 以外は未対応 -> 平坦なenvを返す（口は閉じ気味）
        steps = max(1, int(dur_ms / ENV_DT_MS))
        return [0.0] * steps, dur_ms

    samples = array.array("h")
    samples.frombytes(raw)
    if ch > 1:
        # 先頭チャンネルだけ使う（モノ化）
        samples = samples[0::ch]
    win = max(1, int(sr * ENV_DT_MS / 1000))
    maxval = 32768.0
    env = []
    for i in range(0, len(samples) - win + 1, win):
        s = 0
        seg = samples[i:i + win]
        for v in seg:
            s += v * v
        env.append(round(math.sqrt(s / len(seg)) / maxval, 4))
    return env, dur_ms


if __name__ == "__main__":
    import sys, json
    e, d = wav_envelope(sys.argv[1])
    print(json.dumps({"dur_ms": d, "n": len(e), "max": max(e) if e else 0,
                      "env_head": e[:10]}, ensure_ascii=False))
