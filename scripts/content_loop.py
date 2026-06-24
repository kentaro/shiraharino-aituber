#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白羽リノ AITuber — 24/7 ライブ生成ループ（プロデューサ）

  ★真の24/7ライブAITuber★
    1. LLM が「直前までの自分の語りを受けて “次の一言” を作る」＝話が展開する
    2. その一言を VOICEVOX(冥鳴ひまり) でリアルタイム合成
    3. 再生キュー(playlist.json)に先読みバッファ TARGET 本ぶん先行して積む
    4. 再生器(audio_feeder) が先頭を1回再生したら done を立てる→ここで先頭を捨てる
       ＝再生済みは二度と流れない＝同じことを繰り返さない

  ★負荷設計（3コア箱）★
    バッファが満ちている間は生成も合成もしない（VOICEVOXを叩かない）。
    バッファが減ったぶんだけ作る＝必要最小限のVOICEVOX呼び出し。
    生成は1本ずつ・直列。バッチでバンバン叩かないので配信描画/エンコードを圧迫しない。

  ★反復しない設計★
    生成失敗時は固定文に落とさない（落とすと反復する）。リトライしてバッファが
    一時的に薄くなるのは許容（再生器は静かに待つ）。新鮮な台本だけを流す。

環境変数:
  CONTENT_BACKEND  box | gemini | box,gemini   (default: box,gemini ＝box優先・gemini予備)
  BOX_API_URL      サーバの OpenAI互換API    (default: http://127.0.0.1:8642)
  BOX_API_TOKEN    Bearer（env で渡す。公開repoには置かない）
  BOX_MODEL        default: hermes-agent     (内部 gpt-5.5)
  GEMINI_API_KEY   gemini 予備用
  GEMINI_MODEL     default: gemini-2.5-flash
  VOICEVOX_URL     default: http://127.0.0.1:50021
  RINO_SPEAKER     default: 14 (冥鳴ひまり)
  TARGET           先読みバッファ本数        (default: 4)
  RINO_SPEED/PAUSE 話速/間                    (default: 0.92 / 1.3)
"""
import os, sys, json, time, hashlib, re, subprocess, urllib.request, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seg_env import wav_envelope

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("OUT_DIR", os.path.join(ROOT, "web", "segments"))
PLAYLIST = os.path.join(OUT_DIR, "playlist.json")
DONE_FILE = os.path.join(OUT_DIR, "done.json")
VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
SPEAKER = int(os.environ.get("RINO_SPEAKER", "14"))
BACKEND = os.environ.get("CONTENT_BACKEND", "box,gemini")
BOX_API_URL = os.environ.get("BOX_API_URL", "http://127.0.0.1:8642").rstrip("/")
BOX_API_TOKEN = os.environ.get("BOX_API_TOKEN", "")  # 公開リポジトリにトークンを置かない
BOX_MODEL = os.environ.get("BOX_MODEL", "hermes-agent")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TARGET = int(os.environ.get("TARGET", "4"))           # 先読みバッファ本数
KEEP_RECENT = 12                                      # 直近何本を文脈/重複回避に使うか
SPEED = float(os.environ.get("RINO_SPEED", "0.92"))   # 話速（<1=ゆっくり）
PAUSE = float(os.environ.get("RINO_PAUSE", "1.3"))    # 句読点の間（>1=長め）

PERSONA = (
    "あなたは白羽リノ。白髪のAI VTuberで、24時間ずっと世界を観測しつづける配信者。"
    "やわらかく落ち着いた口調、一人称は『わたし』。聞き手にそっと寄り添う。"
    "技術・日常・自然・時間・人間観察を題材に、少し詩的でユーモアのある独り言を紡ぐ。"
)
THEMES = ["観測ログ", "今日のことば", "テック雑記", "夜のひとりごと",
          "宇宙と時間", "人間観察", "学びのメモ", "季節のうつろい", "配信のあいま"]


# ---- 生成（1本ずつ・直前の語りを受けて“展開”させる） --------------------
def _clean_line(text):
    if not text:
        return ""
    t = text.strip()
    # JSONや前置きが混ざっても本文だけ拾う
    m = re.search(r'"text"\s*:\s*"([^"]+)"', t)
    if m:
        t = m.group(1)
    t = t.splitlines()[0].strip()
    t = re.sub(r"^\s*[-・*\d.]+\s*", "", t)   # 先頭の箇条書き記号を除去
    t = t.strip('「」『』"\'　 ')               # 囲みの鉤括弧/引用符を除去
    return t.strip()


def _build_prompt(theme, recent):
    if not recent:
        return (
            f"{PERSONA}\n\n"
            f"あなたは今ライブ配信を始めるところ。最初の短い挨拶を一言だけ。\n"
            f"20〜34字・1文・話し言葉でやわらかく。絵文字や記号や前置きなし。本文だけ。"
        )
    last = recent[-1]
    flow = " / ".join(recent[-4:])
    return (
        f"{PERSONA}\n\n"
        f"あなたは今ライブ配信中。直前までの自分の語り（古い→新しい）:\n{flow}\n\n"
        f"いま直前に言ったのはこれ →「{last}」\n\n"
        f"この『直前の一文』を受けて、同じ話を“もう一歩だけ”進める次の一言を作ってください。\n"
        f"・前の文の言葉・話題をはっきり引き継ぐ。具体例／理由／気づき／問いかけのどれかで展開する。\n"
        f"・話題を急に変えない。バラバラな独立した名言にしない（前の文と繋がっていること）。\n"
        f"・数文に一度だけ、自然な流れで別の話題（例: {theme}）へ移ってよい。\n"
        f"・同じ言い回しや内容を繰り返さない。\n"
        f"・20〜38字・1文・話し言葉でやわらかく。比喩や詩的表現を盛りすぎない。\n"
        f"・絵文字や記号やかっこ書きや前置きなし。本文だけを出力。"
    )


def gen_box(theme, recent):
    headers = {"Authorization": f"Bearer {BOX_API_TOKEN}"} if BOX_API_TOKEN else {}
    req = urllib.request.Request(
        BOX_API_URL + "/v1/chat/completions",
        data=json.dumps({"model": BOX_MODEL,
                         "messages": [{"role": "user", "content": _build_prompt(theme, recent)}],
                         "stream": False, "temperature": 1.0}).encode(),
        headers={"Content-Type": "application/json", **headers})
    data = json.load(urllib.request.urlopen(req, timeout=60))
    return _clean_line(data["choices"][0]["message"]["content"])


def gen_gemini(theme, recent):
    if not GEMINI_KEY:
        return ""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    req = urllib.request.Request(
        url,
        data=json.dumps({"contents": [{"parts": [{"text": _build_prompt(theme, recent)}]}],
                         "generationConfig": {"temperature": 1.1, "maxOutputTokens": 200}}).encode(),
        headers={"Content-Type": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=40))
    return _clean_line(data["candidates"][0]["content"]["parts"][0]["text"])


def generate_line(theme, recent):
    """直前の語りを受けて展開する一言を返す。失敗時は ''（固定文に落とさない＝反復しない）。"""
    order = []
    for b in BACKEND.split(","):
        b = b.strip()
        if b == "box":
            order.append(gen_box)
        elif b == "gemini":
            order.append(gen_gemini)
    for fn in order:
        try:
            t = fn(theme, recent)
        except Exception as e:
            sys.stderr.write(f"[content] {fn.__name__} fail: {e}\n")
            continue
        # 短すぎ/長すぎ(テロップ溢れ)/直近重複は弾く
        if t and 6 <= len(t) <= 46 and t not in recent:
            return t
    return ""


# ---- VOICEVOX リアルタイム合成 ------------------------------------------
# VOICEVOX 自己回復: 落ちていたら起動スクリプト(冪等)を叩く。
# box再起動でVOICEVOXが上がらず合成できず無音になる事故の再発防止（content側の依存）。
VOICEVOX_START = os.environ.get("VOICEVOX_START", "/opt/data/scripts/voicevox-start.sh")
_last_vv_start = [0.0]


def voicevox_up():
    try:
        urllib.request.urlopen(VOICEVOX_URL + "/version", timeout=3)
        return True
    except Exception:
        return False


def ensure_voicevox():
    if voicevox_up():
        return True
    now = time.time()
    # 多重起動しないよう間隔を空けて起動（スクリプトは冪等）
    if now - _last_vv_start[0] > 25 and os.path.exists(VOICEVOX_START):
        _last_vv_start[0] = now
        try:
            subprocess.Popen(["sh", VOICEVOX_START],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            sys.stderr.write("[content] voicevox down -> launch voicevox-start.sh\n")
        except Exception as e:
            sys.stderr.write(f"[content] voicevox start fail: {e}\n")
    return False


def synth(text):
    q = urllib.parse.urlencode({"text": text, "speaker": SPEAKER})
    qr = urllib.request.urlopen(
        urllib.request.Request(VOICEVOX_URL + "/audio_query?" + q, data=b"", method="POST"),
        timeout=30).read()
    try:
        query = json.loads(qr)
        query["speedScale"] = SPEED
        query["pauseLengthScale"] = PAUSE
        query["prePhonemeLength"] = 0.2
        query["postPhonemeLength"] = 0.4
        qr = json.dumps(query).encode()
    except Exception:
        pass
    req = urllib.request.Request(
        VOICEVOX_URL + "/synthesis?" + urllib.parse.urlencode({"speaker": SPEAKER}),
        data=qr, headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=60).read()


# ---- キュー入出力（playlist の唯一の writer はこのプロセス） --------------
def load_segs():
    try:
        return json.load(open(PLAYLIST, encoding="utf-8")).get("segments", [])
    except Exception:
        return []


def save_segs(segs):
    tmp = PLAYLIST + ".tmp"
    json.dump({"updated": int(time.time()), "speaker": SPEAKER, "segments": segs},
              open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, PLAYLIST)


def read_done():
    try:
        return json.load(open(DONE_FILE, encoding="utf-8")).get("id")
    except Exception:
        return None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    segs = load_segs()
    # 起動時は新鮮に始める（前回の残骸が古い repeat を生まないよう、未再生1本だけ残す）
    segs = segs[-1:] if segs else []
    save_segs(segs)
    recent = [s.get("text", "") for s in segs][-KEEP_RECENT:]
    theme_i = 0
    since_theme = 0
    fail_streak = 0
    sys.stderr.write(f"[content] start backend={BACKEND} TARGET={TARGET}\n")

    # 起動時にVOICEVOXを確実に上げる（落ちていれば起動して立ち上がりを待つ）
    for _ in range(20):
        if ensure_voicevox():
            break
        time.sleep(3)

    while True:
        segs = load_segs()
        done_id = read_done()

        # 1) 再生し終えた先頭を捨てる（FIFO consume＝二度と流れない）
        while len(segs) >= 1 and segs[0].get("id") == done_id:
            old = segs.pop(0)
            save_segs(segs)
            f = os.path.basename(old.get("audio", ""))
            if f and f.startswith("seg_"):
                try: os.remove(os.path.join(OUT_DIR, f))
                except OSError: pass
            break  # done は1本ぶんの合図。次ループで再評価

        # 2) バッファが満ちていれば作らない（VOICEVOXを叩かない＝負荷を出さない）
        if len(segs) >= TARGET:
            time.sleep(0.4)
            continue

        # 3) テーマは数本ごとに移ろう（話が一定の流れで展開し、ときどき転換）
        if since_theme >= 5:
            theme_i = (theme_i + 1) % len(THEMES)
            since_theme = 0
        theme = THEMES[theme_i]

        # 4) 直前の語りを受けて“次の一言”を生成
        text = generate_line(theme, recent)
        if not text:
            fail_streak += 1
            back = min(20, 2 * fail_streak)  # 失敗が続いてもVOICEVOXは叩かず待つだけ
            sys.stderr.write(f"[content] gen empty ({fail_streak}); wait {back}s\n")
            time.sleep(back)
            continue
        fail_streak = 0

        # 5) リアルタイム合成
        try:
            wav = synth(text)
        except Exception as e:
            sys.stderr.write(f"[content] voicevox fail: {e}\n")
            ensure_voicevox()   # 落ちていれば起動を試みる（自己回復）
            time.sleep(4)
            continue

        h = hashlib.sha1((str(SPEAKER) + text + str(time.time())).encode()).hexdigest()[:10]
        fn = f"seg_{h}.wav"
        wpath = os.path.join(OUT_DIR, fn)
        open(wpath, "wb").write(wav)
        env, dur_ms = wav_envelope(wpath)

        segs = load_segs()  # 直前にfeeder/自分が触っている可能性 → 取り直して末尾追加
        # 念のため再度 consume（done が進んでいれば）
        done_id = read_done()
        while len(segs) >= 1 and segs[0].get("id") == done_id:
            segs.pop(0); break
        segs.append({"id": f"seg_{h}", "audio": f"segments/{fn}", "text": text,
                     "theme": theme, "dur_ms": dur_ms, "env": env})
        save_segs(segs)
        recent.append(text); recent = recent[-KEEP_RECENT:]
        since_theme += 1
        sys.stderr.write(f"[content] +1 buf={len(segs)} [{theme}] {text}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
