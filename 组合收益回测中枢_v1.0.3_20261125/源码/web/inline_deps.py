#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 CDN script 标签换成 base64 内联脚本，产出完全离线单页 HTML。

库源码里含 `<!--` / `-->`，直接内联会触发 HTML 解析器的 script-data 转义状态，
把后续 JS 当标签切碎；eval() 又不把库暴露到全局。方案：base64 字符串 → atob →
新建 <script> 注入 head，全局作用域执行且不受 HTML 解析影响。
用法: inline_deps.py <输入.html> <输出.html>
"""
import base64
import sys
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
s = src.read_text('utf-8')

deps = [
    ('https://cdn.bootcdn.net/ajax/libs/echarts/5.4.3/echarts.min.js', Path('/tmp/echarts.min.js')),
    ('https://cdn.bootcdn.net/ajax/libs/xlsx/0.18.5/xlsx.full.min.js', Path('/tmp/xlsx.full.min.js')),
]
for url, p in deps:
    tag = '<script src="%s"></script>' % url
    assert tag in s, '找不到 CDN 标签: ' + url
    b64 = base64.b64encode(p.read_bytes()).decode('ascii')
    inline = ("""<script>(function(){var b="%s",d=atob(b),a=new Uint8Array(d.length);
for(var i=0;i<d.length;i++)a[i]=d.charCodeAt(i);
var sc=document.createElement('script');sc.textContent=new TextDecoder('utf-8').decode(a);
document.head.appendChild(sc);})();</script>""" % b64)
    s = s.replace(tag, inline, 1)
    print('内联(base64) %s  %d B' % (p.name, p.stat().st_size))

assert 'cdn.bootcdn.net' not in s, '仍有残留 CDN 引用'
dst.write_text(s, 'utf-8')
print('输出：%s  %d B' % (dst.name, dst.stat().st_size))
