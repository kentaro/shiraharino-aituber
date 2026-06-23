#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白羽リノ TTS 生成 — VOICEVOX (冥鳴ひまり / style 14)

content.json の台本を VOICEVOX で音声合成し、
web/segments/ に音声ファイルと playlist.json を出力する。

  VOICEVOX_URL    VOICEVOX ENGINE の URL (default: http://127.0.0.1:50021)
  RINO_SPEAKER    話者 style id   (default: 14 = 冥鳴ひまり ノーマル)
  RINO_FORMAT     wav | mp3        (default: wav。mp3 は ffmpeg 必須)

使い方:
  python3 scripts/tts_generate.py [content.json] [--out web/segments]

content.json 形式:
  { "segments": [ {"theme": "...", "text": "..."}, ... ] }
"""
import os, sys, json, argparse, hashlib, subprocess, urllib.request, urllib.parse

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
SPEAKER = int(os.environ.get("RINO_SPEAKER", "14"))
FMT = os.environ.get("RINO_FORMAT", "wav").lower()


def _post(path: str, data: bytes, ctype: str) -> bytes:
    req = urllib.request.Request(VOICEVOX_URL + path, data=data, method="POST")
    req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def synth_wav(text: str) -> bytes:
    q = urllib.parse.urlencode({"text": text, "speaker": SPEAKER})
    query = _post("/audio_query?" + q, b"", "application/json")
    return _post("/synthesis?" + urllib.parse.urlencode({"speaker": SPEAKER}),
                 query, "application/json")


def to_mp3(wav_path: str, mp3_path: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
                    "-ac", "1", "-ar", "44100", "-b:a", "128k", mp3_path], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("content", nargs="?", default=os.path.join(
        os.path.dirname(__file__), "..", "content", "content.json"))
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(__file__), "..", "web", "segments"))
    args = ap.parse_args()

    with open(args.content, encoding="utf-8") as f:
        content = json.load(f)
    items = content.get("segments", [])
    os.makedirs(args.out, exist_ok=True)

    segs = []
    for i, it in enumerate(items):
        text = (it.get("text") or "").strip()
        theme = it.get("theme") or "フリートーク"
        if not text:
            continue
        # 安定したファイル名（テキストのハッシュでキャッシュ）
        h = hashlib.sha1((str(SPEAKER) + text).encode("utf-8")).hexdigest()[:10]
        wav = os.path.join(args.out, f"seg_{h}.wav")
        out_audio = wav
        if not os.path.exists(wav):
            sys.stderr.write(f"[tts] synth {i}: {theme} / {text[:24]}...\n")
            data = synth_wav(text)
            with open(wav, "wb") as wf:
                wf.write(data)
        if FMT == "mp3":
            mp3 = os.path.join(args.out, f"seg_{h}.mp3")
            if not os.path.exists(mp3):
                to_mp3(wav, mp3)
            out_audio = mp3
        rel = "segments/" + os.path.basename(out_audio)
        segs.append({"id": f"seg_{h}", "audio": rel, "text": text, "theme": theme})

    playlist = {"updated": content.get("updated", "generated"), "speaker": SPEAKER, "segments": segs}
    with open(os.path.join(args.out, "playlist.json"), "w", encoding="utf-8") as f:
        json.dump(playlist, f, ensure_ascii=False, indent=2)
    sys.stderr.write(f"[tts] wrote {len(segs)} segments -> {args.out}/playlist.json\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
