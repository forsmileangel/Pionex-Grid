// Fixture responses for the collector CLI test. This replaces fetch before the script runs.
const DETAILS = {
  "bot-usdt": {
    base: "BTC",
    quote: "USDT",
    createTime: 1700000000000,
    status: "running",
    buOrderData: {
      gridProfit: "12.5",
      quoteInvestment: "1000",
      extraMargin: "0",
      leverage: "5",
      trend: "long",
      top: "120000",
      bottom: "100000",
      row: "20",
      gridType: "geometric",
      status: "running",
      openPrice: "105000",
    },
  },
  "bot-eth": {
    base: "USDT",
    quote: "ETH",
    createTime: 1690000000000,
    closeTime: 1700001500000,
    status: "finished",
    buOrderData: {
      gridProfit: "2",
      quoteInvestment: "4",
      openQuotePrice: "5",
      leverage: "3",
      trend: "long",
      top: "3000",
      bottom: "1500",
      row: "10",
      gridType: "geometric",
      status: "finished",
    },
  },
};

function json(data) {
  const body = JSON.stringify({ result: true, timestamp: 1700003000000, data, sentinel: "pionex-grid-test-fixture" });
  return {
    ok: true,
    status: 200,
    async text() { return body; },
  };
}

function targetOf(input) {
  if (typeof input === "string") return new URL(input);
  if (input instanceof URL) return input;
  return new URL(input.url);
}

globalThis.fetch = async (input) => {
  const url = targetOf(input);
  if (url.hostname !== "api.pionex.com") {
    throw new Error(`blocked non-fixture host ${url.hostname}`);
  }
  if (url.pathname === "/api/v1/market/tickers") {
    if (url.searchParams.get("type") === "PERP") {
      return json({
        tickers: [
          { symbol: "BTC_USDT_PERP", close: "110000", time: 1700001400000 },
          { symbol: "ETH_USDT_PERP", close: "2400", time: 1700001400000 },
        ],
      });
    }
    return json({
      tickers: [
        { symbol: "BTC_USDT", close: "1", time: 1 },
        { symbol: "ETH_USDT", close: "10", time: 1700001600000 },
      ],
    });
  }
  if (url.pathname === "/api/v1/bot/orders") {
    const status = url.searchParams.get("status");
    const results = status === "running"
      ? [{ buOrderId: "bot-usdt", base: "BTC", quote: "USDT", createTime: 1700000000000, status: "running" }]
      : [{ buOrderId: "bot-eth", base: "USDT", quote: "ETH", createTime: 1690000000000, closeTime: 1700001500000, status: "finished" }];
    return json({ results });
  }
  if (url.pathname === "/api/v1/bot/orders/futuresGrid/order") {
    const id = url.searchParams.get("buOrderId");
    if (!DETAILS[id]) throw new Error(`unexpected fixture order ${id}`);
    return json(DETAILS[id]);
  }
  if (url.pathname === "/api/v1/wallet/balancesFull") {
    return json({
      totalInUsdt: "100",
      totalInBtc: "0.01",
      botAccount: { totalInUsdt: "80", detail: [] },
      traderAccount: { totalInUsdt: "20", detail: [] },
    });
  }
  throw new Error(`unexpected fixture request ${url.pathname}`);
};
