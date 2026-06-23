#!/usr/bin/env bash
# キャラクター・アセットの再生成（再現可能なビルド）
#  1) 静止透過PNG character_base.png: グリーンスクリーン素体 →
#       クロマキー＋グリーンクランプ(G'=min(G,max(R,B)))で緑だけ中和(白を桃化しない)。
#       元動画はほぼ静止のため動画ではなく静止画にして軽量化(ソフトGLでも滑らか)。
#  2) 閉じ目スプライト eyes_closed.png: 閉じ目ソースから目バンドのみフェザー抽出。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../web/assets/shiraharino" && pwd)"
cd "$DIR"
echo "[assets] 1) 静止透過PNG（クロマキー＋グリーンクランプ）"
ffmpeg -y -loglevel error -i shiraharino_mouthless_h264.mp4 -frames:v 1 \
  -vf "scale=720:720,chromakey=0x11f80e:0.14:0.10,format=rgba,\
geq=r='r(X,Y)':g='min(g(X,Y),max(r(X,Y),b(X,Y)))':b='b(X,Y)':a='alpha(X,Y)'" \
  character_base.png
echo "[assets]   -> character_base.png"
echo "[assets] 2) 閉じ目スプライト（目バンドのみ・フェザー）"
python3 - <<'PY'
from PIL import Image, ImageDraw, ImageFilter
src = Image.open("_closed_source.png").convert("RGB").resize((1254, 1254))
mask = Image.new("L", (1254, 1254), 0)
ImageDraw.Draw(mask).rounded_rectangle([452, 432, 812, 582], radius=70, fill=255)
mask = mask.filter(ImageFilter.GaussianBlur(20))
eyes = src.convert("RGBA"); eyes.putalpha(mask)
eyes.save("eyes_closed.png")
print("   -> eyes_closed.png")
PY
echo "[assets] done."
