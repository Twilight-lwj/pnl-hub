#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由含数据的调试页生成「无数据发行版」单页 HTML。

1) 清掉预览器回写的 data-page-node-id 属性
2) EMBED 置空（无持仓 / 无走势 / 金额为 0），保留完整字段结构
3) 刷新行情按钮在无数据时给出友好提示，避免访问 ohlc[-1] 崩溃
4) 信息条区间在无数据时显示「等待导入」
"""
import json
import re
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
s = src.read_text('utf-8')

# 1) 清掉预览器回写属性
before = s.count('data-page-node-id')
s = re.sub(r'\s+data-page-node-id="[^"]*"', '', s)
print('清理 data-page-node-id：%d 处' % before)

# 2) EMBED 置空
empty = {
    "dates": [], "ohlc": [], "cost": [], "holdings": [],
    "stats": {
        "mv": 0, "dayPnl": 0, "holdPnl": 0, "cost": 0,
        "realized": 0, "dividend": 0, "repoInterest": 0,
        "cash": 0, "repo": 0, "deposits": 0, "totalAssets": 0, "nav": 1,
    },
    "source": "",
}
payload = json.dumps(empty, ensure_ascii=False, separators=(',', ':'))
new, n = re.subn(r'const EMBED = \{.*?\};\n', 'const EMBED = ' + payload + ';\n', s, count=1, flags=re.S)
assert n == 1, 'EMBED 替换失败'
s = new
print('EMBED 已置空：%d 字符' % len(payload))

# 3) 刷新行情空数据保护（幂等：主源码可能已内置更完善的保护）
old3 = """$('btnRefresh').addEventListener('click', async () => {
  if (state.key !== 'ths') { loadStrategy(state.key); return; }
  try {"""
new3 = """$('btnRefresh').addEventListener('click', async () => {
  if (state.key !== 'ths') { loadStrategy(state.key); return; }
  if (!EMBED.holdings.length) { toast('暂无持仓数据，请先到设置页导入同花顺账本流水'); return; }
  try {"""
builtin = """$('btnRefresh').addEventListener('click', async () => {
  if (state.key !== 'ths') { loadStrategy(state.key); return; }
  const cur = state.data;
  if (!cur || !cur.holdings || !cur.holdings.length) {"""
if old3 in s:
    s = s.replace(old3, new3, 1)
    print('刷新按钮：已插入空数据保护')
elif builtin in s:
    print('刷新按钮：主源码已内置空数据保护，跳过')
else:
    raise AssertionError('刷新按钮锚点未找到')

# 4) 信息条区间空数据保护
old4 = """    `<span>区间 <b>${data.dates[0]} → ${data.dates[data.dates.length - 1]}</b></span>`;"""
new4 = """    (data.dates.length
      ? `<span>区间 <b>${data.dates[0]} → ${data.dates[data.dates.length - 1]}</b></span>`
      : `<span>区间 <b>等待导入</b></span>`);"""
if old4 in s:
    s = s.replace(old4, new4, 1)
    print('信息条区间：已插入空数据保护')
elif '等待导入' in s:
    print('信息条区间：主源码已内置空数据保护，跳过')
else:
    raise AssertionError('信息条区间锚点未找到')

dst.write_text(s, 'utf-8')
print('输出：%s  %d B' % (dst.name, dst.stat().st_size))
