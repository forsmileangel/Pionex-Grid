import crypto from "node:crypto";
import fs from "node:fs";

const API_BASE = "https://api.pionex.com";
const DEFAULT_CREDENTIAL_PATH = "D:\\My-project\\pionex grid record\\PIONEX API.txt";
const SOURCE = "Pionex API (read-only)";

function option(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 && index + 1 < process.argv.length ? process.argv[index + 1] : fallback;
}

function readCredentials(filePath) {
  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length !== 2) throw new Error("Credential file must contain exactly two non-empty lines: API key, API secret.");
  return { apiKey: lines[0], apiSecret: lines[1] };
}

function sortedQuery(params) {
  return new URLSearchParams(Object.entries(params).sort(([a], [b]) => a.localeCompare(b))).toString();
}

function privateGet(path, params, credentials) {
  const query = sortedQuery({ ...params, timestamp: Date.now().toString() });
  const pathUrl = `${path}?${query}`;
  const signature = crypto.createHmac("sha256", credentials.apiSecret).update(`GET${pathUrl}`).digest("hex");
  return fetch(`${API_BASE}${pathUrl}`, {
    method: "GET",
    headers: { "PIONEX-KEY": credentials.apiKey, "PIONEX-SIGNATURE": signature, Accept: "application/json" },
  });
}

function publicGet(path, params) {
  const query = sortedQuery(params);
  return fetch(`${API_BASE}${path}?${query}`, { method: "GET", headers: { Accept: "application/json" } });
}

async function parseResponse(response, label) {
  const body = await response.text();
  let json;
  try { json = JSON.parse(body); } catch { throw new Error(`${label} returned non-JSON HTTP ${response.status}.`); }
  if (!response.ok || json.result === false) {
    throw new Error(`${label} failed: HTTP ${response.status}; code=${json.code || "unknown"}; message=${json.message || "unknown"}`);
  }
  return json;
}

async function requestPrivate(path, params, credentials) {
  const allowed = new Set(["/api/v1/bot/orders", "/api/v1/bot/orders/futuresGrid/order"]);
  if (!allowed.has(path)) throw new Error(`Blocked non-read-only endpoint: ${path}`);
  return parseResponse(await privateGet(path, params, credentials), `Pionex ${path}`);
}

async function requestPublic(path, params) {
  if (path !== "/api/v1/market/tickers") throw new Error(`Blocked public endpoint: ${path}`);
  return parseResponse(await publicGet(path, params), `Pionex ${path}`);
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function firstNumber(...values) {
  for (const value of values) {
    const number = numberOrNull(value);
    if (number !== null) return number;
  }
  return null;
}

function formatTaipeiTime(value) {
  const timestamp = numberOrNull(value);
  if (timestamp === null || timestamp <= 0) return "";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(timestamp));
  const values = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
}

function normalizeSymbol(base, quote) {
  const cleanBase = String(base || "").replace(/\.PERP$/i, "");
  const cleanQuote = String(quote || "").replace(/\.PERP$/i, "");
  if (cleanBase === "USDT" && cleanQuote && cleanQuote !== "USDT") return cleanQuote;
  if (cleanBase && cleanQuote) return `${cleanBase}/${cleanQuote}`;
  return cleanBase || cleanQuote;
}

function directionLabel(trend) {
  if (trend === "long") return "long";
  if (trend === "short") return "short";
  return String(trend || "");
}

async function getSpotClose(coin) {
  const symbol = `${coin}_USDT`;
  const response = await requestPublic("/api/v1/market/tickers", { symbol, type: "SPOT" });
  const tickers = response.data && Array.isArray(response.data.tickers) ? response.data.tickers : [];
  const ticker = tickers.find((item) => item.symbol === symbol) || tickers[0];
  const close = ticker ? numberOrNull(ticker.close) : null;
  if (close === null || close <= 0) throw new Error(`No valid USDT ticker for coin-margined quote ${coin}.`);
  return { symbol, close, time: ticker.time || null };
}

async function listRunningFuturesGrids(credentials) {
  const records = [];
  const seen = new Set();
  let pageToken = "";
  for (let page = 0; page < 100; page += 1) {
    const params = { status: "running", buOrderTypes: "futures_grid" };
    if (pageToken) params.pageToken = pageToken;
    const response = await requestPrivate("/api/v1/bot/orders", params, credentials);
    const data = response.data || {};
    const pageResults = Array.isArray(data.results) ? data.results : [];
    for (const item of pageResults) {
      const id = String(item.buOrderId || "");
      if (!id) throw new Error("Pionex returned a running grid without buOrderId.");
      if (!seen.has(id)) { seen.add(id); records.push(item); }
    }
    const next = data.nextPageToken ? String(data.nextPageToken) : "";
    if (!next || next === pageToken) break;
    pageToken = next;
    if (page === 99) throw new Error("Pionex pagination exceeded the safety limit.");
  }
  if (records.length === 0) throw new Error("No running futures-grid records were returned.");
  return records;
}

async function detailToRecord(item, credentials) {
  const orderId = String(item.buOrderId);
  const response = await requestPrivate("/api/v1/bot/orders/futuresGrid/order", { buOrderId: orderId, lang: "en" }, credentials);
  const order = response.data || {};
  const data = order.buOrderData || {};
  const base = order.base || item.base || "";
  const quote = order.quote || item.quote || "";
  const symbol = normalizeSymbol(base, quote);
  const created = formatTaipeiTime(order.createTime || item.createTime);
  if (!symbol || !created) throw new Error(`Incomplete grid identity for API order ${orderId}.`);

  const rawGridProfit = firstNumber(data.gridProfit, data.grid_profit);
  if (rawGridProfit === null) throw new Error(`Grid profit missing for API order ${orderId}.`);
  const rawQuoteInvestment = firstNumber(data.quoteInvestment, data.quote_investment);
  const rawUsdtInvestment = firstNumber(data.usdtInvestment, data.usdt_investment);
  const extraMargin = firstNumber(data.extraMargin, data.extra_margin) || 0;
  const coinMargined = String(quote).toUpperCase() !== "USDT";
  let investment = rawQuoteInvestment;
  let gridProfit = rawGridProfit;
  let conversionPrice = null;
  let conversionSymbol = "";
  let conversionTime = null;

  if (coinMargined) {
    const ticker = await getSpotClose(String(quote).replace(/\.PERP$/i, ""));
    conversionPrice = ticker.close;
    conversionSymbol = ticker.symbol;
    conversionTime = ticker.time;
    if (rawQuoteInvestment === null) throw new Error(`Coin-margined quote investment missing for API order ${orderId}.`);
    const openQuotePrice = firstNumber(data.openQuotePrice, data.open_quote_price);
    if (openQuotePrice === null || openQuotePrice <= 0) throw new Error(`Coin-margined open quote price missing for API order ${orderId}.`);
    investment = rawQuoteInvestment * openQuotePrice;
    gridProfit = rawGridProfit * conversionPrice;
  } else if (investment === null) {
    investment = rawUsdtInvestment === null ? null : rawUsdtInvestment + extraMargin;
  }
  if (investment === null || !Number.isFinite(investment) || investment < 0) throw new Error(`Investment missing for API order ${orderId}.`);

  const leverage = firstNumber(data.leverage);
  const trend = directionLabel(data.trend);
  const product = coinMargined ? "coin_margined_contract_grid" : "contract_grid";
  const totalProfit = firstNumber(data.totalProfit, data.total_profit);
  return {
    Key: `${symbol}|${created}`,
    ApiOrderId: orderId,
    Symbol: symbol,
    Created: created,
    Product: product,
    Leverage: leverage === null ? trend : `${leverage}x ${trend}`.trim(),
    Investment: investment,
    GridProfit: gridProfit,
    TotalProfit: totalProfit,
    Status: "running",
    ProfitCurrency: "USDT",
    RawGridProfit: rawGridProfit,
    RawGridProfitCurrency: coinMargined ? String(quote) : "USDT",
    RawQuoteInvestment: rawQuoteInvestment,
    RawQuoteInvestmentCurrency: coinMargined ? String(quote) : "USDT",
    ConversionPrice: conversionPrice,
    ConversionSymbol: conversionSymbol,
    ConversionTime: conversionTime,
    Source: SOURCE,
  };
}

async function collect() {
  const credentialPath = option("--credentials", process.env.PIONEX_CREDENTIAL_PATH || DEFAULT_CREDENTIAL_PATH);
  const credentials = readCredentials(credentialPath);
  const listed = await listRunningFuturesGrids(credentials);
  const records = [];
  for (let index = 0; index < listed.length; index += 1) {
    records.push(await detailToRecord(listed[index], credentials));
    if (index + 1 < listed.length) await new Promise((resolve) => setTimeout(resolve, 120));
  }
  const keys = new Set(records.map((record) => record.Key));
  if (keys.size !== records.length) throw new Error("Duplicate normalized grid key detected; no snapshot produced.");
  records.sort((a, b) => b.Created.localeCompare(a.Created));
  return {
    ok: true,
    CapturedAt: new Date().toISOString(),
    Source: SOURCE,
    ExpectedCardCount: records.length,
    ContractCount: records.length,
    SavingsCount: 0,
    Records: records,
  };
}

try {
  const snapshot = await collect();
  const outputPath = option("--output", "");
  const json = JSON.stringify(snapshot, null, 2);
  if (outputPath) fs.writeFileSync(outputPath, `${json}\n`, { encoding: "utf8", flag: "w" });
  console.log(json);
} catch (error) {
  console.error(`Pionex grid API collector failed: ${error.message}`);
  process.exitCode = 1;
}
