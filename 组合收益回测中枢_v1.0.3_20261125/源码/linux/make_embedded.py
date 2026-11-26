#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把单文件页面与图标编码成 embedded.py，供 PyInstaller --onefile 内嵌。

用法：python3 make_embedded.py <index.html> <hold.png> <out/embedded.py> [VERSION]

刻意接受显式输出路径：构建在 mktemp 临时目录里跑，不在归档目录写中间文件，
避免污染定版后的 SHA256SUMS。
"""
import base64
import pathlib
import sys

if len(sys.argv) < 4:
    raise SystemExit(__doc__)

page = pathlib.Path(sys.argv[1])
icon = pathlib.Path(sys.argv[2])
out = pathlib.Path(sys.argv[3])
ver = sys.argv[4] if len(sys.argv) > 4 else "1.0.3"

html_b64 = base64.b64encode(page.read_bytes()).decode("ascii")
icon_b64 = base64.b64encode(icon.read_bytes()).decode("ascii")


def wrap(var, blob):
    body = "".join('    "%s"\n' % blob[i:i + 76] for i in range(0, len(blob), 76))
    return "%s = (\n%s)\n" % (var, body)


out.write_text(
    "# -*- coding: utf-8 -*-\n"
    "# 由 make_embedded.py 自动生成，勿手改\n"
    'VERSION = "%s"\n\n' % ver
    + wrap("HTML_B64", html_b64) + "\n" + wrap("ICON_B64", icon_b64),
    encoding="utf-8",
)
print("embedded.py  %d B  (页面 %d B / 图标 %d B)"
      % (out.stat().st_size, page.stat().st_size, icon.stat().st_size))
