import assert from "node:assert/strict";
import { test } from "node:test";

import { cachedFinishedRecord } from "../../../pionex-grid-api.mjs";

const HOUR = 60 * 60 * 1000;
const closedMs = 1700000000000;

function listed(extra = {}) {
  return {
    buOrderId: "ord-1",
    closeTime: closedMs,
    base: "BTC",
    quote: "USDT",
    status: "finished",
    ...extra,
  };
}

function cacheAt(fetchedMs, listItem) {
  return {
    "ord-1": {
      fetched_at: new Date(fetchedMs).toISOString(),
      record: {
        ListStatus: "finished",
        ApiOrderId: "ord-1",
        GridProfit: 5,
        RawJson: { list: listItem },
      },
    },
  };
}

test("cachedFinishedRecord skips a bot closed for less than 24 hours", () => {
  const listItem = listed();
  const result = cachedFinishedRecord(cacheAt(closedMs + 24 * HOUR - 1, listItem), listed());
  assert.equal(result, null);
});

test("cachedFinishedRecord skips a bot whose finished list entry changed", () => {
  const listItem = listed();
  const result = cachedFinishedRecord(cacheAt(closedMs + 24 * HOUR, listItem), listed({ base: "ETH" }));
  assert.equal(result, null);
});

test("cachedFinishedRecord reuses a settled bot whose list entry is unchanged", () => {
  const listItem = listed();
  const result = cachedFinishedRecord(cacheAt(closedMs + 24 * HOUR, listItem), listed());
  assert.equal(result.DetailSource, "cache");
  assert.equal(result.ApiOrderId, "ord-1");
  assert.equal(result.GridProfit, 5);
});
