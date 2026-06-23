# 白羽リノ 観測スタジオ — 24/7 AITuber

白髪のAI VTuber **白羽リノ** を、**1台のサーバだけ**で 24時間365日
YouTube Live 配信しつづけるための配信スタック。OBS も外部の配信PCも要らない。

> 設計思想は「OBSのシーンを組む」のではなく **配信用Webページを1枚作る**。
> UIはHTML/CSS、映像化はChrome+Xvfb、配信はffmpeg、という分担。

```
                    ┌─ 映像 ─────────────────────────────────┐
HTML/CSS/JS(配信ページ follow mode)              audio_feeder.py
   背景＋透過キャラ＋口パク＋まばたき＋字幕            playlist の wav を
   （口パクは nowplaying.json の env で駆動）          PCM で FIFO に供給
        │                                              │
   Xvfb + chromium --kiosk                             │
        │                                              │
   ffmpeg  ◄── x11grab(映像) ──────────── FIFO(音声) ──┘
        ▼
   YouTube Live(rtmps) / 録画(mp4)
```

**音声は別経路**: ブラウザの音声を一切キャプチャしない（PulseAudio不要）。
`audio_feeder.py` が playlist の wav を ffmpeg に渡し、配信ページは
`nowplaying.json` の envelope で口パクする。映像と音声は壁時計で**完全同期**し、
**Linux でも macOS でも**同じ仕組みで動く。

音声は **VOICEVOX 冥鳴ひまり**、台本は **サーバ内蔵 Codec(gpt-5.5)** が生成（サブスクなので 24/7 でも限界課金ゼロ）。

## 構成

```
web/                  配信用Webページ（静的・これ1枚が「番組画面」）
  index.html
  broadcast.css
  broadcast.js        ← src/broadcast.ts のビルド成果物
  assets/             背景・キャラ動画(透過webm)・口/閉じ目スプライト
  segments/           TTS音声 + playlist.json（再生キュー）
src/broadcast.ts      レンダラ本体（TypeScript）
scripts/
  stream.sh           Xvfb+chromium+ffmpeg の配信パイプライン（映像x11grab＋音声FIFO）
  audio_feeder.py     playlist の wav を PCM で ffmpeg に供給＋nowplaying.json を書く
  run.sh              24/7 マスター起動（content_loop と stream を監督・自動復帰）
  content_loop.py     台本生成(box Codec/gemini/offline)→VOICEVOX→playlist(+env)
  seg_env.py          口パク用エンベロープ計算（共有・純stdlib）
  tts_generate.py     content/content.json から音声を一括生成（seed作成等）
  make_assets.sh      透過webm・閉じ目スプライトの再生成
content/content.json  seed の台本
deploy/               systemd unit 例
docs/ASSETS.md        アセットの作り方（グリーンクランプ等）
```

## 動かす

### 1. 配信ページをビルド

```bash
npm install
npm run build        # src/broadcast.ts → web/broadcast.js
```

### 2. ローカルでプレビュー（ブラウザで確認）

```bash
npm run serve        # http://localhost:8777/
```

ブラウザで開き「▶ 配信を開始」を押す（ブラウザの自動再生制限のため）。
背景＋白羽リノが乗り、seed の音声に合わせて口パク・まばたき・体の揺れが動く。

### 3. サーバ上で配信パイプラインを起動

必要なもの: `ffmpeg`(x11grab/libx264) / `Xvfb` / `chromium`(Playwright同梱可) /
`pulseaudio` / `python3` / VOICEVOX ENGINE。

```bash
# 録画でパイプライン検証（限定確認）
MODE=record DURATION=20 bash scripts/stream.sh
#   → var/record.mp4 が出る

# 24/7 常駐（録画ループ・自動復帰つき）
MODE=record DURATION= bash scripts/run.sh

# 本番: YouTube Live へ配信
MODE=live STREAM_KEY=xxxx-xxxx-xxxx-xxxx bash scripts/run.sh
```

`STREAM_KEY` は YouTube Studio で発行。**鍵はコミットしない**（環境変数で渡す）。

## 設定（主要な環境変数）

| 変数 | 既定 | 説明 |
|---|---|---|
| `MODE` | `record` | `record`=録画 / `live`=YouTube配信 |
| `STREAM_KEY` | — | `live` 時必須 |
| `WIDTH`/`HEIGHT`/`FPS` | `1280`/`720`/`30` | 出力解像度・FPS |
| `CONTENT_BACKEND` | `box` | 台本生成: `box`(gpt-5.5/サブスク) / `gemini` / `offline` |
| `BOX_API_URL` | `http://127.0.0.1:8642` | box の OpenAI互換API |
| `VOICEVOX_URL` | `http://127.0.0.1:50021` | VOICEVOX ENGINE |
| `RINO_SPEAKER` | `14` | VOICEVOX 話者（冥鳴ひまり）|

## コスト設計（24/365 を破産させない）

- **台本**: box 内蔵 Codec(gpt-5.5) はサブスク → 限界課金ゼロ。さらに
  **バッチ生成＋バッファが減ったら補充**で呼び出し回数を最小化。
- **音声**: VOICEVOX はローカル合成 → 無料。
- **映像**: ffmpeg(x264) の CPU のみ。外部課金なし。

## クレジット

- キャラクター: 白羽リノ
- 音声: VOICEVOX:冥鳴ひまり
- ライセンス: MIT（`LICENSE`）。VOICEVOX 音声の利用は VOICEVOX 各話者の規約に従う。
