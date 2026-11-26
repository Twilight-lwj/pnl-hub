#!/usr/bin/env bash
# 组合收益回测中枢 v1.0.3 · Linux AppImage 构建
# 离线单页 HTML + 图标 → embedded.py → PyInstaller --onefile → AppDir → squashfs → type2 runtime
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APP=pnlhub
NAME=组合收益回测中枢
VER=1.0.3
# 解释器：环境变量 PY 优先，否则取 PATH 里的 python3（需已装 PyInstaller）
if [ -z "${PY:-}" ]; then
    PY="$(command -v python3 || true)"
fi
[ -n "${PY:-}" ] || { echo "找不到 python3：请用 PY=/path/to/python3 指定"; exit 1; }
DIST="${DIST:-$ROOT/发行版}"
PAGE="$ROOT/${NAME}_网页版.html"

for f in "$PAGE" "$HERE/pnlhub.png" "$HERE/runtime-x86_64" "$HERE/app.desktop" "$HERE/app.py"; do
  [ -s "$f" ] || { echo "缺少构建资产：$f"; exit 1; }
done

mkdir -p "$DIST"
WORK="$(mktemp -d /tmp/pnlhub-appimage.XXXXXX)"
SRC="$WORK/src"; AD="$WORK/AppDir"
mkdir -p "$SRC" "$WORK/dist" "$WORK/build" "$AD/usr/bin"
cp "$PAGE" "$SRC/index.html"
cp "$HERE/pnlhub.png" "$SRC/pnlhub.png"
cp "$HERE/app.py" "$SRC/app.py"
cp "$HERE/runtime-x86_64" "$WORK/runtime"

echo "[1/4] 生成内嵌模块 ..."
"$PY" "$HERE/make_embedded.py" "$SRC/index.html" "$SRC/pnlhub.png" "$SRC/embedded.py" "$VER"

echo "[2/4] PyInstaller --onefile ..."
export PYINSTALLER_CONFIG_DIR="$WORK/pyicfg"
: "${HOME:?HOME 未设置，无法定位用户目录}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
cd "$SRC"
"$PY" -m PyInstaller --onefile --noconfirm --strip --name "$APP" \
  --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK" \
  --add-data "$SRC/pnlhub.png:." \
  --exclude-module tkinter --exclude-module unittest --exclude-module pydoc \
  --exclude-module sqlite3 --exclude-module distutils --exclude-module lib2to3 \
  app.py > "$WORK/pyi.log" 2>&1 || { tail -40 "$WORK/pyi.log"; exit 1; }
[ -s "$WORK/dist/$APP" ] || { echo "PyInstaller 未产出可执行文件"; exit 1; }

echo "[3/4] AppDir + runtime 拼接 ..."
cp "$WORK/dist/$APP" "$AD/usr/bin/$APP"; chmod +x "$AD/usr/bin/$APP"
cp "$HERE/pnlhub.png" "$AD/$APP.png"
cp "$HERE/pnlhub.png" "$AD/.DirIcon"
cp "$HERE/app.desktop" "$AD/$APP.desktop"
cat > "$AD/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/pnlhub" "$@"
EOF
chmod +x "$AD/AppRun"
mksquashfs "$AD" "$WORK/app.squashfs" -root-owned -noappend -mkfs-time 0 \
  -comp zstd -Xcompression-level 19 -b 262144 >/dev/null
cat "$WORK/runtime" "$WORK/app.squashfs" > "$WORK/out.AppImage"
OUT="$DIST/${NAME}-发行版${VER}.AppImage"
mv -f "$WORK/out.AppImage" "$OUT"
chmod +x "$OUT"

echo "[4/4] 自检 ..."
V="$("$WORK/dist/$APP" --version)"
[ "$V" = "$APP $VER" ] || { echo "版本号异常：$V"; exit 1; }
echo "      --version → $V"
grep -q '^Name=组合收益回测中枢$' "$AD/$APP.desktop" || { echo "desktop 显示名不对"; exit 1; }
( cd "$WORK" && "$OUT" --appimage-extract >/dev/null )
for f in "usr/bin/$APP" "$APP.desktop" AppRun "$APP.png" .DirIcon; do
  [ -s "$WORK/squashfs-root/$f" ] || { echo "AppImage 解包缺文件：$f"; exit 1; }
done
"$PY" - "$SRC/embedded.py" "$PAGE" <<'PY'
import base64, pathlib, re, sys
s = pathlib.Path(sys.argv[1]).read_text('utf-8')
m = re.search(r'HTML_B64 = \((.*?)\n\)', s, re.S)
assert m, 'embedded.py 里找不到 HTML_B64'
blob = ''.join(re.findall(r'"([A-Za-z0-9+/=]+)"', m.group(1)))
page = pathlib.Path(sys.argv[2]).read_bytes()
assert base64.b64decode(blob) == page, '内嵌页面与成品页不一致'
print('      内嵌页面逐字节 == 成品页  %d B' % len(page))
PY
echo "      -> $OUT  $(du -h "$OUT" | cut -f1)"
echo "完成。"
