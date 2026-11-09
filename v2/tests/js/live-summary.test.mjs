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

test("daily records retain their grid card", () => {
  const html = render({ kind: "daily" });
  assert.match(html, /過去 24 小時總利潤/);
  assert.doesNotMatch(html, /資金費資料不足/);
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
