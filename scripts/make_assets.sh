#!/usr/bin/env bash
# =========================================================================
# キャラクター・アセットの再生成（再現可能なビルド）
#
#  1) 透過WebM: グリーンスクリーン動画 → クロマキー → グリーンクランプ
#       （G' = min(G, max(R,B)) で緑スピルだけ中和。白を桃色化しない）
#  2) 閉じ目スプライト: 閉じ目ソース画像から目バンドのみをフェザー抽出
#
#  閉じ目ソース(_closed_source.png)は gemini-2.5-flash-image で
#  「目を閉じた版」を生成したもの（生成プロンプトは docs/ASSETS.md 参照）。
# =========================================================================
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../web/assets/shiraharino" && pwd)"
cd "$DIR"

echo "[assets] 1) 透過WebM（クロマキー＋グリーンクランプ）"
ffmpeg -y -loglevel error -i shiraharino_mouthless_h264.mp4 \
  -vf "chromakey=0x11f80e:0.14:0.10,format=rgba,\
geq=r='r(X,Y)':g='min(g(X,Y),max(r(X,Y),b(X,Y)))':b='b(X,Y)':a='alpha(X,Y)',\
format=yuva420p" \
  -c:v libvpx-vp9 -pix_fmt yuva420p -b:v 2.5M -auto-alt-ref 0 \
  -metadata:s:v:0 alpha_mode=1 shiraharino_mouthless.webm
echo "[assets]   -> shiraharino_mouthless.webm"

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
