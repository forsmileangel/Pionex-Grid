import assert from "node:assert/strict";
import { test } from "node:test";

import { parseResponse, requestPrivate, requestPublic, withNetworkRetry } from "../../../pionex-grid-api.mjs";

const originalFetch = globalThis.fetch;
const creds = { apiKey: "test-key", apiSecret: "test-secret" };

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() { return body; },
  };
}

function useZeroDelay(t) {
  const previous = process.env.PIONEX_RETRY_DELAY_MS;
  process.env.PIONEX_RETRY_DELAY_MS = "0";
  t.after(() => {
    globalThis.fetch = originalFetch;
    if (previous === undefined) delete process.env.PIONEX_RETRY_DELAY_MS;
    else process.env.PIONEX_RETRY_DELAY_MS = previous;
  });
}

const down = JSON.stringify({ result: false, code: "DOWN", message: "down", timestamp: 1 });

async function failingCalls(status, body) {
  let calls = 0;
  await assert.rejects(() => withNetworkRetry("probe", async () => {
    calls += 1;
    return parseResponse(response(status, body), "probe", Date.now());
  }));
  return calls;
}

test("withNetworkRetry retries an HTTP 5xx response up to 3 times", async (t) => {
  useZeroDelay(t);
  assert.equal(await failingCalls(503, down), 3);
});

test("withNetworkRetry retries an HTTP 429 response up to 3 times", async (t) => {
  useZeroDelay(t);
  assert.equal(await failingCalls(429, down), 3);
});

test("withNetworkRetry retries a non-JSON response up to 3 times", async (t) => {
  useZeroDelay(t);
  assert.equal(await failingCalls(502, "<html>outage</html>"), 3);
});

test("withNetworkRetry stops when a later attempt succeeds", async (t) => {
  useZeroDelay(t);
  let calls = 0;
  const result = await withNetworkRetry("probe", async () => {
    calls += 1;
    if (calls === 1) return parseResponse(response(503, down), "probe", Date.now());
    return parseResponse(response(200, JSON.stringify({ result: true, timestamp: 1, data: { ok: 1 } })), "probe", Date.now());
  });
  assert.equal(calls, 2);
  assert.equal(result.data.ok, 1);
});

test("requestPrivate blocks an endpoint outside the read-only whitelist", async (t) => {
  useZeroDelay(t);
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    throw new Error("should not fetch");
  };
  await assert.rejects(
    () => requestPrivate("/api/v1/trade/order", {}, creds),
    /Blocked non-read-only endpoint: \/api\/v1\/trade\/order/,
  );
  assert.equal(called, false);
});

test("requestPublic blocks anything other than tickers", async (t) => {
  useZeroDelay(t);
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    throw new Error("should not fetch");
  };
  await assert.rejects(
    () => requestPublic("/api/v1/market/klines", {}),
    /Blocked public endpoint: \/api\/v1\/market\/klines/,
  );
  assert.equal(called, false);
});

test("requestPublic allows the tickers endpoint", async (t) => {
  useZeroDelay(t);
  let url = "";
  globalThis.fetch = async (input) => {
    url = String(input);
    return response(200, JSON.stringify({ result: true, timestamp: 1, data: { tickers: [] } }));
  };
  const body = await requestPublic("/api/v1/market/tickers", { type: "SPOT", symbol: "ETH_USDT" });
  assert.match(url, /\/api\/v1\/market\/tickers\?/);
  assert.equal(body.result, true);
});

test("requestPrivate allows a whitelisted read-only endpoint", async (t) => {
  useZeroDelay(t);
  let url = "";
  globalThis.fetch = async (input) => {
    url = String(input);
    return response(200, JSON.stringify({ result: true, timestamp: 1, data: { totalInUsdt: "1" } }));
  };
  const body = await requestPrivate("/api/v1/wallet/balancesFull", {}, creds);
  assert.match(url, /\/api\/v1\/wallet\/balancesFull\?/);
  assert.equal(body.data.totalInUsdt, "1");
});
