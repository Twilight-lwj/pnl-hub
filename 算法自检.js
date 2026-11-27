#!/usr/bin/env node
/* 组合收益回测中枢 v1.0.5 算法自检：从 HTML 抽函数进 vm，验证核心口径与页面结构。 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const V = __dirname;
const htmlPath = path.join(V, '组合收益回测中枢_网页版.html');
const html = fs.readFileSync(htmlPath, 'utf8');
let pass = 0, fail = 0;
function ok(cond, msg) { if (cond) { pass++; console.log('  ✓ ' + msg); } else { fail++; console.log('  ✗ ' + msg); } }

// ---- 1. 页面结构 ----
console.log('\n[1] 页面结构');
ok(html.includes('v1.0.5') || html.includes('组合收益回测中枢'), '版本/产品名称存在');
ok(!html.includes('data-page-node-id'), '无预览器回写属性');
ok(!html.includes('cdn.bootcdn.net'), 'CDN 已内联（无外链依赖）');
ok(html.includes('const EMBED = {"dates":[],"ohlc":[],"cost":[],"holdings":[]'), 'EMBED 为无数据空态');
ok(html.includes('pnl_cash_yield_v4'), '货币收益存储键为 v4（默认 0）');
ok(html.includes('simple: true'), '手动策略页 simple 口径存在');
ok(html.includes('appstock/app/kline/kline'), '日K主接口 kline/kline 已内联');
ok(html.includes('appstock/app/fqkline/get'), '旧接口 fqkline/get 兜底保留');
ok(/for \(const base of bases\)/.test(html), 'fetchKline 双接口轮询存在');
ok(html.includes('setSystemBars'), '状态栏桥 setSystemBars 调用存在');
ok(html.includes("'#1e2a3a'") && html.includes("'#0a0f15'"), '浅/深状态栏配色与顶栏一致');
ok(html.includes("LS.ths = 'pnl_ths_v1'"), '同花顺明细持久化键存在');
ok(/ths:\s*loadThs\(\)/.test(html), '导出备份含 ths 股票明细快照');
ok(html.includes('saveThs()'), '导入/刷新后保存股票明细');
ok(html.includes('function thsLoad('), 'thsLoad 统一装载函数存在');
ok(/const p = thsLoad\(\)/.test(html), 'boot 从快照恢复同花顺页');
ok(/function renderTabs\(/.test(html), '动态页签渲染函数存在');
ok(html.includes('pnl_pages_v1'), '页面配置存储键存在');
ok(!html.includes('持股持息A策略') && html.includes('施洛斯A股指数') && html.includes('大肥猫A股指数'), '持股持息已移除且策略页改名');
ok(html.includes('function stocksOfPage('), '跨页股票提取函数存在');
ok(html.includes('tradeImportStocks'), '手动添加弹层跨页导入入口存在');
ok(!html.includes('setTabNav') && !html.includes('tabNav_'), '页签净值显示已移除');
ok(html.includes('id="importFlow"') && !html.includes('id="tabImport"'), '导入入口已迁至设置页');
ok(/if \(state\.key !== key\) return;/.test(html), '策略异步竞态守卫存在');

// ---- 2. 抽 assetCalc 函数验证口径 ----
console.log('\n[2] 资产口径函数');
const ctx = {
  console,
  localStorage: { getItem: () => null, setItem: () => {} },
  // loadLS 桩：无现金数据时返回全 0（货币收益默认 0 的口径）
  loadLS: () => ({ repo: 0, wealth: 0, money: 0 }),
  LS: { cash: 'pnl_cash_yield_v4' },
};
vm.createContext(ctx);
const m = html.match(/function assetCalc\(s, extra\) \{[\s\S]*?\n\}/);
ok(!!m, '找到 assetCalc 函数');
if (m) {
  vm.runInContext(m[0] + '\nglobalThis._fns = { assetCalc };', ctx);
  const { assetCalc } = ctx._fns;
  // 同花顺页：入金 + 盈亏 + 货币 + 清仓
  const s = { mv: 44164, cost: 46230.04 };
  const extra = { realized: 30.24, deposits: 350000 };
  const a = assetCalc(s, extra);
  ok(Math.abs(a.ta - (350000 - 2066.04 + 30.24)) < 0.01, '同花顺页总资产 = 入金+盈亏+货币+清仓 (' + a.ta.toFixed(2) + ')');
  ok(!a.simple, '同花顺页非 simple');
  // 手动页：入金 + 盈亏（不含货币/清仓）
  const b = assetCalc(s, { ...extra, simple: true });
  ok(Math.abs(b.ta - (350000 - 2066.04)) < 0.01, '手动页总资产 = 入金+盈亏 (' + b.ta.toFixed(2) + ')');
  ok(b.simple, '手动页 simple=true');
}

// ---- 3. FIFO 成本口径 ----
console.log('\n[3] FIFO 成本口径');
const bs = html.match(/function buildSeries\(trades, klines\) \{[\s\S]*?\n\}/);
ok(!!bs, '找到 buildSeries 函数');
if (bs) {
  const body = bs[0];
  ok(body.includes('p.avg * p.qty + t.price * t.qty'), '买入按加权均价');
  ok(body.includes('p.avg - t.amt / p.qty'), '分红冲减成本（摊薄）');
  ok(body.includes('snapDay'), '非交易日流水归到最近交易日');
}

// ---- 4. 交付物存在性（发行版不入库，未构建时跳过而非判失败）----
console.log('\n[4] 交付物');
for (const f of [
  '发行版/组合收益回测中枢-发行版1.0.5.apk',
  '发行版/组合收益回测中枢-发行版1.0.5.AppImage',
]) {
  if (fs.existsSync(path.join(V, f))) ok(true, f + ' 存在');
  else console.log('  - ' + f + ' 未构建，跳过');
}

console.log('\n结果：%d 通过，%d 失败', pass, fail);
process.exit(fail ? 1 : 0);
