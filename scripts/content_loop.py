#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白羽リノ AITuber — 24/7 コンテンツ生成ループ

  トーク台本を「バッチ生成」→ VOICEVOX(冥鳴ひまり) で音声合成
  → web/segments/playlist.json にローリング追記。配信ページが直列再生する。

  ★コスト設計: 24/365 で外部APIを叩き続けると破産するため、
    既定の生成バックエンドは「サーバ内蔵の Codex(gpt-5.5 等) の
    OpenAI互換API」。サブスク利用なら限界課金ゼロ。さらにバッチ生成＋
    バッファが減ったら補充、で呼び出し回数自体を最小化する。

環境変数:
  CONTENT_BACKEND  box | gemini | offline   (default: box)
  BOX_API_URL      サーバの OpenAI互換API    (default: http://127.0.0.1:8642)
  BOX_API_TOKEN    Bearer トークン（必要なら env で渡す。未設定なら付けない）
  BOX_MODEL        default: hermes-agent     (内部 gpt-5.5)
  GEMINI_API_KEY   gemini フォールバック用
  GEMINI_MODEL     default: gemini-2.5-flash
  VOICEVOX_URL     default: http://127.0.0.1:50021
  RINO_SPEAKER     default: 14 (冥鳴ひまり)
  BATCH            1回の生成本数             (default: 8)
  AVG_SEC          1本の想定尺(秒)・補充間隔の計算用 (default: 13)
  BUFFER_RATIO     次バッチまでに残すバッファ割合    (default: 0.5)
  KEEP             playlist 保持本数          (default: 48)
  OUT_DIR          default: ../web/segments
"""
import os, sys, json, time, hashlib, re, urllib.request, urllib.parse, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seg_env import wav_envelope

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("OUT_DIR", os.path.join(ROOT, "web", "segments"))
VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
SPEAKER = int(os.environ.get("RINO_SPEAKER", "14"))
BACKEND = os.environ.get("CONTENT_BACKEND", "box")
BOX_API_URL = os.environ.get("BOX_API_URL", "http://127.0.0.1:8642").rstrip("/")
BOX_API_TOKEN = os.environ.get("BOX_API_TOKEN", "")  # 公開リポジトリにトークンは置かない。env で渡す
BOX_MODEL = os.environ.get("BOX_MODEL", "hermes-agent")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# 音声はキューに貯まる（feederがplaylistを巡回再生）ので、VOICEVOXは
# バンバン叩かず「1行ずつ・ゆっくり貯める」だけでよい＝低スペック箱でも配信が滑らか。
BATCH = int(os.environ.get("BATCH", "1"))
GEN_INTERVAL = float(os.environ.get("GEN_INTERVAL", "60"))  # 1行ごとの最低間隔(秒)
KEEP = int(os.environ.get("KEEP", "48"))

PERSONA = (
    "あなたは白羽リノ。白髪のAI VTuberで、24時間ずっと世界を観測しつづける配信者。"
    "やわらかく落ち着いた口調、一人称は『わたし』。聞き手にそっと寄り添う。"
    "技術・日常・自然・時間・人間観察を題材に、少し詩的でユーモアのある独り言を紡ぐ。"
)
THEMES = ["観測ログ", "今日のことば", "テック雑記", "夜のひとりごと",
          "宇宙と時間", "人間観察", "学びのメモ", "季節のうつろい"]

FALLBACK = [
    {"theme": "夜のひとりごと", "text": "夜がふけてきましたね。こんな時間にも、世界のどこかでは新しい一日が始まっています。"},
    {"theme": "テック雑記", "text": "新しいツールに触れるとき、わたしはいつも少しわくわくします。知らないことは、可能性のかたまりだから。"},
    {"theme": "観測ログ", "text": "今日も小さな変化をいくつも見つけました。気づくこと、それ自体がもう、ひとつの発見だと思うんです。"},
    {"theme": "今日のことば", "text": "急がなくていい。立ち止まっても、ちゃんと前に進んでいます。"},
    {"theme": "季節のうつろい", "text": "風のにおいが、少しずつ変わってきました。季節は、いつだって静かに移ろっていきますね。"},
    {"theme": "宇宙と時間", "text": "光が今わたしに届いているその星は、もうとっくに姿を変えているのかもしれません。時間って、不思議ですね。"},
]


def _post_json(url, payload, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _extract_json_array(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
        out = []
        for it in arr:
            if isinstance(it, dict) and it.get("text"):
                out.append({"theme": str(it.get("theme") or "フリートーク"),
                            "text": str(it["text"]).strip()})
        return out or None
    except Exception:
        return None


def gen_box(n, recent):
    avoid = " / ".join(recent[-6:]) if recent else "（なし）"
    prompt = (f"{PERSONA}\n\n配信で話す独り言を{n}個作って。"
              f"各2〜3文・60〜110字・絵文字や記号やかっこ書きなし。"
              f"テーマは多様に（例: {', '.join(THEMES)}）。直近と重複しないこと。直近: {avoid}\n"
              f'必ず次のJSON配列だけを出力（前後に文章を付けない）: '
              f'[{{"theme":"テーマ","text":"本文"}}, ...]')
    headers = {"Authorization": f"Bearer {BOX_API_TOKEN}"} if BOX_API_TOKEN else {}
    data = _post_json(BOX_API_URL + "/v1/chat/completions",
                      {"model": BOX_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False},
                      headers, timeout=150)
    return _extract_json_array(data["choices"][0]["message"]["content"])


def gen_gemini(n, recent):
    if not GEMINI_KEY:
        return None
    avoid = " / ".join(recent[-6:]) if recent else "（なし）"
    prompt = (f"{PERSONA}\n\n配信で話す独り言を{n}個作って。各2〜3文・60〜110字・記号や絵文字なし。"
              f"直近と重複しないこと。直近: {avoid}\n"
              f'JSON配列だけ出力: [{{"theme":"テーマ","text":"本文"}}, ...]')
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    data = _post_json(url, {"contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": 1.0, "maxOutputTokens": 1200}},
                      {}, timeout=60)
    return _extract_json_array(data["candidates"][0]["content"]["parts"][0]["text"])


def generate_batch(n, recent):
    order = {"box": [gen_box, gen_gemini], "gemini": [gen_gemini, gen_box],
             "offline": []}.get(BACKEND, [gen_box, gen_gemini])
    for fn in order:
        try:
            r = fn(n, recent)
            if r:
                return r
        except Exception as e:
            sys.stderr.write(f"[content] {fn.__name__} fail: {e}\n")
    # 最後の砦: オフラインプール
    pool = FALLBACK[:]
    random.shuffle(pool)
    return pool[:n]


# ゆっくり・間のある喋り（自然＋VOICEVOX負荷も下がる）
SPEED = float(os.environ.get("RINO_SPEED", "0.88"))       # 話速（<1=ゆっくり）
PAUSE = float(os.environ.get("RINO_PAUSE", "1.4"))        # 句読点の間（>1=長め）


def synth(text):
    q = urllib.parse.urlencode({"text": text, "speaker": SPEAKER})
    qr = urllib.request.urlopen(urllib.request.Request(VOICEVOX_URL + "/audio_query?" + q, data=b"", method="POST"),
                                timeout=60).read()
    # クエリを調整：話速ゆっくり・句読点の間を長く・前後に余白
    try:
        query = json.loads(qr)
        query["speedScale"] = SPEED
        query["pauseLengthScale"] = PAUSE
        query["prePhonemeLength"] = 0.25
        query["postPhonemeLength"] = 0.5
        qr = json.dumps(query).encode()
    except Exception:
        pass
    req = urllib.request.Request(VOICEVOX_URL + "/synthesis?" + urllib.parse.urlencode({"speaker": SPEAKER}),
                                 data=qr, headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=120).read()


def load_segs(path):
    try:
        return json.load(open(path, encoding="utf-8")).get("segments", [])
    except Exception:
        return []


def save_segs(path, segs):
    tmp = path + ".tmp"
    json.dump({"updated": int(time.time()), "speaker": SPEAKER, "segments": segs},
              open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "playlist.json")
    segs = load_segs(path)
    recent = [s.get("text", "") for s in segs][-8:]
    sys.stderr.write(f"[content] start backend={BACKEND} batch={BATCH} keep={KEEP} existing={len(segs)}\n")

    fail_streak = 0
    while True:
        batch = generate_batch(BATCH, recent)
        added = 0
        for it in batch:
            text = it["text"].strip()
            if not text:
                continue
            try:
                wav = synth(text)
                fail_streak = 0
            except Exception as e:
                fail_streak += 1
                # 連続失敗時は指数バックオフ（VOICEVOXを叩き続けて負荷を上げない）
                back = min(90, 8 * fail_streak)
                sys.stderr.write(f"[content] voicevox fail ({fail_streak}): {e}; backoff {back}s\n")
                time.sleep(back)
                continue
            h = hashlib.sha1((str(SPEAKER) + text).encode()).hexdigest()[:10]
            fn = f"seg_{h}.wav"
            wpath = os.path.join(OUT_DIR, fn)
            open(wpath, "wb").write(wav)
            env, dur_ms = wav_envelope(wpath)
            segs.append({"id": f"seg_{h}", "audio": f"segments/{fn}", "text": text,
                         "theme": it["theme"], "dur_ms": dur_ms, "env": env})
            recent.append(text); recent = recent[-8:]
            added += 1

        # ローリング保持＋古い音声掃除（seed_ は消さない）
        if len(segs) > KEEP:
            drop = segs[:-KEEP]; segs = segs[-KEEP:]
            keep_files = {os.path.basename(s["audio"]) for s in segs}
            for d in drop:
                f = os.path.basename(d.get("audio", ""))
                if f and f.startswith("seg_") and f not in keep_files:
                    try: os.remove(os.path.join(OUT_DIR, f))
                    except OSError: pass

        save_segs(path, segs)
        sys.stderr.write(f"[content] +{added} (total {len(segs)})\n")

        # 1行ごとにゆっくり待つ（VOICEVOXを叩き続けない＝配信を圧迫しない）
        time.sleep(GEN_INTERVAL if added else 15)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
