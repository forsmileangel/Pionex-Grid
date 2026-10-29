import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { buildRecord, getSpotClose } from "../../../pionex-grid-api.mjs";

const originalFetch = globalThis.fetch;
const originalArgv = process.argv.slice();

function tickerResponse(tickers) {
  return {
    ok: true,
    status: 200,
    async text() {
      return JSON.stringify({ result: true, timestamp: 1700000000000, data: { tickers } });
    },
  };
}

function installFetch(tickers) {
  globalThis.fetch = async () => tickerResponse(tickers);
}

function coinOrder() {
  const created = 1700000000000;
  return {
    item: { buOrderId: "ord-1", base: "USDT", quote: "ETH", createTime: created },
    order: {
      base: "USDT",
      quote: "ETH",
      createTime: created,
      buOrderData: {
        gridProfit: "2",
        quoteInvestment: "4",
        openQuotePrice: "5",
        leverage: "3",
        trend: "long",
      },
    },
  };
}

function writePriceCache(priceByOrder) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pionex-price-"));
  const file = path.join(dir, "finished-details.json");
  const records = {};
  for (const [id, price] of Object.entries(priceByOrder)) {
    records[id] = {
      fetched_at: "2026-10-01T08:15:00+08:00",
      record: { ConversionPrice: price, Symbol: "not-copied" },
    };
  }
  fs.writeFileSync(file, JSON.stringify({ records }));
  return { dir, file };
}

test("getSpotClose uses the ticker whose symbol matches exactly", async (t) => {
  t.after(() => { globalThis.fetch = originalFetch; });
  installFetch([
    { symbol: "BTC_USDT", close: "1", time: 1 },
    { symbol: "ETH_USDT", close: "2500", time: 99 },
  ]);
  const ticker = await getSpotClose("ETH");
  assert.equal(ticker.symbol, "ETH_USDT");
  assert.equal(ticker.close, 2500);
  assert.equal(ticker.time, 99);
});

test("getSpotClose rejects a book that only contains another coin", async (t) => {
  t.after(() => { globalThis.fetch = originalFetch; });
  installFetch([{ symbol: "BTC_USDT", close: "999", time: 1 }]);
  await assert.rejects(
    () => getSpotClose("ETH"),
    /No valid USDT ticker for coin-margined quote ETH/,
  );
});

test("a finished coin-margined bot keeps the cached price and marks it stale", async (t) => {
  const cache = writePriceCache({ "ord-1": 10 });
  t.after(() => {
    globalThis.fetch = originalFetch;
    process.argv = originalArgv.slice();
    fs.rmSync(cache.dir, { recursive: true, force: true });
  });
  installFetch([{ symbol: "BTC_USDT", close: "999", time: 1 }]);
  process.argv = ["node", "pionex-grid-api.mjs", "--price-cache", cache.file];
  const { item, order } = coinOrder();
  const record = await buildRecord(item, order, "finished", new Map());
  assert.equal(record.ConversionPrice, 10);
  assert.equal(record.ConversionStale, true);
  assert.equal(record.ConversionSymbol, "ETH_USDT");
  assert.equal(record.GridProfit, 20);
  assert.equal(record.Investment, 20);
  assert.equal(record.Symbol, "ETH");
  assert.equal(record.ApiOrderId, "ord-1");
});

test("a finished coin-margined bot without a cached price names the order and the coin", async (t) => {
  const cache = writePriceCache({ "other-order": 10 });
  t.after(() => {
    globalThis.fetch = originalFetch;
    process.argv = originalArgv.slice();
    fs.rmSync(cache.dir, { recursive: true, force: true });
  });
  installFetch([{ symbol: "BTC_USDT", close: "999", time: 1 }]);
  process.argv = ["node", "pionex-grid-api.mjs", "--price-cache", cache.file];
  const { item, order } = coinOrder();
  await assert.rejects(
    () => buildRecord(item, order, "finished", new Map()),
    (error) => {
      assert.match(error.message, /ord-1/);
      assert.match(error.message, /ETH/);
      return true;
    },
  );
});

test("a running coin-margined bot still fails when the ticker is missing", async (t) => {
  const cache = writePriceCache({ "ord-1": 10 });
  t.after(() => {
    globalThis.fetch = originalFetch;
    process.argv = originalArgv.slice();
    fs.rmSync(cache.dir, { recursive: true, force: true });
  });
  installFetch([{ symbol: "BTC_USDT", close: "999", time: 1 }]);
  process.argv = ["node", "pionex-grid-api.mjs", "--price-cache", cache.file];
  const { item, order } = coinOrder();
  await assert.rejects(
    () => buildRecord(item, order, "running", new Map()),
    (error) => {
      assert.equal(error.message, "No valid USDT ticker for coin-margined quote ETH.");
      return true;
    },
  );
});

test("a live ticker is used as-is and does not mark the record stale", async (t) => {
  const cache = writePriceCache({ "ord-1": 10 });
  t.after(() => {
    globalThis.fetch = originalFetch;
    process.argv = originalArgv.slice();
    fs.rmSync(cache.dir, { recursive: true, force: true });
  });
  installFetch([
    { symbol: "BTC_USDT", close: "1", time: 1 },
    { symbol: "ETH_USDT", close: "2500", time: 5 },
  ]);
  process.argv = ["node", "pionex-grid-api.mjs", "--price-cache", cache.file];
  const { item, order } = coinOrder();
  const record = await buildRecord(item, order, "finished", new Map());
  assert.equal(record.ConversionPrice, 2500);
  assert.equal(record.GridProfit, 5000);
  assert.equal(Object.hasOwn(record, "ConversionStale"), false);
});
