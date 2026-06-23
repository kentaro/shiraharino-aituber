# アセットについて

配信ページが使うキャラクター・アセットと、その作り方（再現手順）。

## ファイル一覧（`web/assets/shiraharino/`）

| ファイル | 用途 |
|---|---|
| `shiraharino_mouthless_h264.mp4` | 口を消したグリーンスクリーンの素体動画（呼吸等のアイドル。今回の素体はほぼ静止）|
| `shiraharino_mouthless.webm` | 上を緑抜き透過にした VP9 alpha 動画（**配信ページが再生**）|
| `mouth_track.json` | フレームごとの口の位置（quad）|
| `mouth/{closed,half,open}.png` | 音量に応じて切り替える口スプライト |
| `_closed_source.png` | 「目を閉じた版」の生成元（gemini-2.5-flash-image で作成）|
| `eyes_closed.png` | まばたき用の閉じ目スプライト（目バンドのみをフェザー抽出）|
| `room_bg.png` | 配信ルームの背景 |

## 再生成

```bash
bash scripts/make_assets.sh
```

これで `shiraharino_mouthless.webm` と `eyes_closed.png` を再生成する。

### 透過WebM のキモ: グリーンクランプ

白髪・白衣装のキャラに通常の despill をかけると **白が桃色に転ぶ**。
そこで「緑が R,B を超えるピクセルだけ緑を抑える」`G' = min(G, max(R,B))`
を ffmpeg の `geq` で適用する。白（R=G=B）は不変、緑フチだけ中和される。

```
chromakey=0x11f80e:0.14:0.10, format=rgba,
geq=r='r(X,Y)':g='min(g(X,Y),max(r(X,Y),b(X,Y)))':b='b(X,Y)':a='alpha(X,Y)',
format=yuva420p
→ libvpx-vp9 -pix_fmt yuva420p -auto-alt-ref 0 -metadata:s:v:0 alpha_mode=1
```

> ffmpeg の VP9 デコーダは alpha を読めないため、`ffprobe` は `yuv420p` と表示し
> `alphaextract` も失敗するが、**Chromium は透過再生できる**（偽陰性に注意）。

### 閉じ目スプライト

素体には閉じ目が無いので、`_closed_source.png`（gemini-2.5-flash-image で
「目を閉じた版」を同一画風で生成したもの）から **目のバンドだけ** を
フェザー付きで切り出し、開き目の上に重ねてまばたきさせる。

生成プロンプト（参考）:
> Make her CLOSE her eyes gently, as if blinking — natural relaxed closed eyes
> with downward eyelashes, same anime art style, same face/hair/lighting/pose.
> Change ONLY the eyes. Keep everything else identical, including the green background.

## 声

VOICEVOX **冥鳴ひまり（style id 14）**。配信画面右下にクレジットを常時表示する
（VOICEVOX 利用規約に基づく）。
