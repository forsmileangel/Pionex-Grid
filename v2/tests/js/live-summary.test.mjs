import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { test } from "node:test";

// common.js imports state.js, which reads location.search when it loads; give Node the browser globals it touches.
globalThis.document = { getElementById: () => null, addEventListener: () => {} };
globalThis.location = { search: "", hash: "" };
globalThis.window = { addEventListener: () => {}, scrollX: 0, scrollY: 0, innerWidth: 1280 };
const { summaryCards } = await import("../../static/js/common.js");
const board = { kind: "live", total_profit_24h: { usdt: "10" }, profit_24h_pct: 1 };
const render = (values) => summaryCards({ ...board, ...values }, "time", "test", "grid", "total");

test("live card shows net, funding sign/share and the net percentage", () => {
  for (const [funding, net, word] of [[-3, 7, "吃掉"], [3, 13, "加成"]]) {
    const html = render({ total_funding_24h: { usdt: funding }, total_net_24h: { usdt: net }, net_24h_pct: 0.7 });
    assert.match(html, /過去 24 小時淨利（含資金費）/);
    assert.ok(html.includes(`+${net.toFixed(2)}<span class="unit">USDT`));
    assert.match(html, /配對 \+10.00 · 資金費/);
    assert.ok(html.includes(`${word}配對 30.0%`));
    assert.match(html, /\+0.70%/);
  }
});

test("missing funding falls back to grid and hides the net percentage", () => {
  const html = render({ funding_24h_missing: 1 });
  assert.match(html, /\+10.00<span class="unit">USDT/);
  assert.match(html, /資金費資料不足（1 倉），暫顯示配對利潤/);
  assert.doesNotMatch(html, /class="pct /);
});

test("daily records show the snapshot day's net, as the calendar does", () => {
  const html = render({ kind: "daily", as_of: "2026-11-09T08:15:18+08:00", day_grid: { usdt: "62.14" },
    day_funding: { usdt: "61.07" }, day_net: { usdt: "123.21" }, day_net_pct: 0.18, day_span: 1 });
  assert.match(html, /11\/09 08:15 快照前 24 小時淨利（含資金費）/);
  assert.ok(html.includes(`+123.21<span class="unit">USDT`));
  assert.match(html, /配對 \+62.14 · 資金費 \+61.07 · 加成配對 98.3%/);
  assert.match(html, /\+0.18%/);
  assert.match(html, /不是即時/);
  assert.doesNotMatch(html, /從現在往回算的滾動/);
  assert.doesNotMatch(html, /相隔/);
});

test("daily records note a gap between snapshots", () => {
  const html = render({ kind: "daily", as_of: "2026-11-09T08:15:18+08:00", day_grid: { usdt: "20" },
    day_funding: { usdt: "-2" }, day_net: { usdt: "18" }, day_net_pct: 0.1, day_span: 3 });
  assert.match(html, /和前一次快照相隔 3 天，數字涵蓋這 3 天/);
});

test("daily records without funding fall back to Pionex's grid figure and say so", () => {
  const html = render({ kind: "daily", as_of: "2026-11-09T08:15:18+08:00", day_grid: { usdt: "10" } });
  assert.match(html, /11\/09 08:15 快照前 24 小時配對/);
  assert.match(html, /\+10.00<span class="unit">USDT/);
  assert.match(html, /這天沒有資金費資料，暫顯示派網回傳的配對/);
  assert.match(html, /\+1.00%/);
});

test("first range preset is a rolling day and the next is a complete 08:00 day", () => {
  const source = fs.readFileSync(new URL("../../static/js/pages/range.js", import.meta.url), "utf8");
  const functions = source.slice(source.indexOf("function lastSettle("), source.indexOf("async function load("));
  const at = new Date("2026-09-19T12:34:00+08:00").getTime();
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : [at])); }
  }
  const values = vm.runInNewContext(functions + ";presets()", { Date: FixedDate });
  assert.equal(values[0][0], "最近 24 小時（到現在）");
  assert.equal(values[0][1].getTime(), at - 24 * 3600e3);
  assert.equal(values[0][2].getTime(), at);
  assert.equal(values[1][0], "最近一個完整日（08:00–08:00）");
  assert.equal(values[1][2] - values[1][1], 24 * 3600e3);
  assert.equal(values[1][1].getHours(), 8);
  assert.equal(values[1][2].getHours(), 8);
});
