import crypto from "node:crypto";
import fs from "node:fs";

const API_BASE = "https://api.pionex.com";
const DEFAULT_CREDENTIAL_PATH = "D:\\My-project\\pionex grid record\\PIONEX API.txt";
const SOURCE = "Pionex API (read-only)";

function option(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 && index + 1 < process.argv.length ? process.argv[index + 1] : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
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
  const allowed = new Set(["/api/v1/bot/orders", "/api/v1/bot/orders/futuresGrid/order", "/api/v1/wallet/balancesFull"]);
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

function nowTaipeiIso() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}:${values.second}+08:00`;
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

async function getPerpTickers() {
  const response = await requestPublic("/api/v1/market/tickers", { type: "PERP" });
  const tickers = response.data && Array.isArray(response.data.tickers) ? response.data.tickers : [];
  const map = new Map();
  for (const ticker of tickers) {
    const close = numberOrNull(ticker.close);
    if (ticker.symbol && close !== null && close > 0) map.set(String(ticker.symbol), { close, time: ticker.time || null });
  }
  return map;
}

function markFromTickers(symbol, tickers) {
  const base = String(symbol || "").split("/")[0].replace(/\.PERP$/i, "");
  if (!base) return { price: null, tickerSymbol: "", time: null };
  const tickerSymbol = `${base}_USDT_PERP`;
  const ticker = tickers.get(tickerSymbol);
  if (!ticker) return { price: null, tickerSymbol, time: null };
  return { price: ticker.close, tickerSymbol, time: ticker.time };
}

function effectiveLiq(trend, down, up, liq) {
  if (trend === "short" && down === null && up !== null && up > 0) return up;
  if (trend === "short" && up !== null && up > 0) return up;
  if (trend !== "short" && down !== null && down > 0) return down;
  if (liq !== null && liq > 0) return liq;
  return null;
}

function isInverseCoinMargined(base, quote) {
  return String(quote || "").toUpperCase() !== "USDT" && String(base || "").replace(/\.PERP$/i, "").toUpperCase() === "USDT";
}

function liqDistance(side, mark, liq) {
  return side === "short" ? ((liq - mark) / mark) * 100 : ((mark - liq) / mark) * 100;
}

function inRange(distance) {
  return distance !== null && distance >= 0 && distance <= 100;
}

function magnitudeMismatch(mark, liq) {
  if (!(mark > 0) || !(liq > 0)) return false;
  return Math.abs(Math.log10(mark) - Math.log10(liq)) >= 2;
}

function normalizeLiq(trend, mark, liq, coinMargined) {
  if (mark === null || liq === null || mark <= 0 || liq <= 0) return { liq, distance: null };
  const side = trend === "short" ? "short" : "long";
  const direct = liqDistance(side, mark, liq);
  const inverse = Boolean(coinMargined) || magnitudeMismatch(mark, liq);
  if (!inverse) return inRange(direct) ? { liq, distance: direct } : { liq, distance: 0 };
  const display = magnitudeMismatch(mark, liq) ? 1 / liq : liq;
  const flip = side === "short" ? "long" : "short";
  const options = [
    { liq: display, distance: liqDistance(flip, mark, display) },
    { liq: display, distance: liqDistance(side, mark, display) },
    { liq, distance: liqDistance(flip, mark, liq) },
    { liq, distance: direct },
  ];
  const good = options.find((item) => inRange(item.distance));
  return good || { liq: display, distance: 0 };
}

const STRIP_RAW_KEYS = /^(userId|keyId|token|secret|apiKey|apiSecret|api_key|api_secret)$/i;

function sanitizeRaw(value) {
  if (Array.isArray(value)) return value.map(sanitizeRaw);
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, child] of Object.entries(value)) {
      if (STRIP_RAW_KEYS.test(key) || /secret|token|apikey/i.test(key)) continue;
      out[key] = sanitizeRaw(child);
    }
    return out;
  }
  return value;
}

function firstString(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    return String(value);
  }
  return "";
}

function estimateSpacing(top, bottom, row, gridType) {
  if (!(top > 0) || !(bottom > 0) || !(row > 0)) {
    return { EstimatedStepPct: null, EstimatedStepPrice: null, EstimatedRangePct: null };
  }
  const rangePct = ((top - bottom) / bottom) * 100;
  if (gridType === "geometric") {
    return {
      EstimatedStepPct: (Math.pow(top / bottom, 1 / row) - 1) * 100,
      EstimatedStepPrice: null,
      EstimatedRangePct: rangePct,
    };
  }
  return {
    EstimatedStepPct: null,
    EstimatedStepPrice: (top - bottom) / row,
    EstimatedRangePct: rangePct,
  };
}

async function listFuturesGrids(credentials, status) {
  const records = [];
  const seen = new Set();
  let pageToken = "";
  for (let page = 0; page < 100; page += 1) {
    const params = { status, buOrderTypes: "futures_grid" };
    if (pageToken) params.pageToken = pageToken;
    const response = await requestPrivate("/api/v1/bot/orders", params, credentials);
    const data = response.data || {};
    const pageResults = Array.isArray(data.results) ? data.results : [];
    for (const item of pageResults) {
      const id = String(item.buOrderId || "");
      if (!id) throw new Error(`Pionex returned a ${status} grid without buOrderId.`);
      if (!seen.has(id)) { seen.add(id); records.push(item); }
    }
    const next = data.nextPageToken ? String(data.nextPageToken) : "";
    if (!next || next === pageToken) break;
    pageToken = next;
    if (page === 99) throw new Error("Pionex pagination exceeded the safety limit.");
  }
  return records;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function scaleUsdt(value, conversionPrice) {
  if (value === null) return null;
  if (conversionPrice === null) return value;
  return value * conversionPrice;
}

async function detailToRecord(item, credentials, listStatus, perpTickers) {
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
  if (rawGridProfit === null && listStatus === "running") throw new Error(`Grid profit missing for API order ${orderId}.`);
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
    if (rawQuoteInvestment === null && listStatus === "running") throw new Error(`Coin-margined quote investment missing for API order ${orderId}.`);
    const openQuotePrice = firstNumber(data.openQuotePrice, data.open_quote_price, data.initQuotePrice);
    if ((openQuotePrice === null || openQuotePrice <= 0) && listStatus === "running") throw new Error(`Coin-margined open quote price missing for API order ${orderId}.`);
    if (rawQuoteInvestment !== null && openQuotePrice) investment = rawQuoteInvestment * openQuotePrice;
    if (rawGridProfit !== null) gridProfit = rawGridProfit * conversionPrice;
  } else if (investment === null) {
    investment = rawUsdtInvestment === null ? null : rawUsdtInvestment + extraMargin;
  }
  if (listStatus === "running" && (investment === null || !Number.isFinite(investment) || investment < 0)) {
    throw new Error(`Investment missing for API order ${orderId}.`);
  }

  const leverage = firstNumber(data.leverage);
  const trend = directionLabel(data.trend);
  const product = coinMargined ? "coin_margined_contract_grid" : "contract_grid";
  const top = firstNumber(data.top);
  const bottom = firstNumber(data.bottom);
  const row = firstNumber(data.row);
  const gridType = firstString(data.gridType, data.grid_type);
  const spacing = estimateSpacing(top, bottom, row, gridType);
  const complete = rawGridProfit !== null && investment !== null && Number.isFinite(investment);
  const inverse = isInverseCoinMargined(base, quote);
  const mark = markFromTickers(symbol, perpTickers);
  const liqDown = firstNumber(data.estimateLiquidationPriceDown, data.estimate_liquidation_price_down);
  const liqUp = firstNumber(data.estimateLiquidationPriceUp, data.estimate_liquidation_price_up);
  const liqActual = firstNumber(data.liquidationPrice, data.liquidation_price);
  const liqContract = effectiveLiq(trend, liqDown, liqUp, liqActual);
  const normalizedLiq = normalizeLiq(trend, mark.price, liqContract, inverse || coinMargined);
  const position = firstNumber(data.position);
  const notional = mark.price !== null && position !== null ? Math.abs(position) * mark.price : investment;
  const profit24h = scaleUsdt(firstNumber(data.gridProfit24h, data.grid_profit_24h, data.profit24h), coinMargined ? conversionPrice : null);
  return {
    Key: `${symbol}|${created}`,
    ApiOrderId: orderId,
    Symbol: symbol,
    Created: created,
    Closed: formatTaipeiTime(data.closeTime || order.closeTime || item.closeTime),
    Product: product,
    ListStatus: listStatus,
    BotStatus: firstString(data.status, order.status, item.status),
    ReasonBy: firstString(data.reasonBy, data.reason_by),
    Leverage: leverage === null ? trend : `${leverage}x ${trend}`.trim(),
    LeverageValue: leverage,
    Trend: trend,
    GridType: gridType,
    Top: top,
    Bottom: bottom,
    Row: row,
    PerVolume: firstNumber(data.perVolume, data.per_volume),
    OpenPrice: firstNumber(data.openPrice, data.open_price, data.initPrice),
    MarkPrice: mark.price,
    MarkSymbol: mark.tickerSymbol,
    MarkTime: mark.time,
    Notional: notional,
    LiqPrice: normalizedLiq.liq,
    LiqDistancePct: normalizedLiq.distance,
    InverseCoinMargined: inverse,
    Position: position,
    PositionOpenPrice: firstNumber(data.positionOpenPrice, data.position_open_price),
    BaseAmount: firstNumber(data.baseAmount, data.base_amount, data.closedBaseAmount),
    QuoteAmount: firstNumber(data.quoteAmount, data.quote_amount),
    Investment: investment,
    GridProfit: gridProfit,
    TotalProfit: scaleUsdt(firstNumber(data.totalProfit, data.total_profit), coinMargined ? conversionPrice : null),
    GridProfit24h: profit24h,
    Fee: scaleUsdt(firstNumber(data.fee, data.feeQuote, data.quoteFee), coinMargined ? conversionPrice : null),
    FeeBase: firstNumber(data.feeBase, data.baseFee),
    FeeQuote: firstNumber(data.feeQuote, data.quoteFee, data.fee),
    FundingFee: scaleUsdt(firstNumber(data.totalFundingFee, data.fundingFeePayment, data.fundingFee, data.funding_fee), coinMargined ? conversionPrice : null),
    ProfitReinvest: scaleUsdt(firstNumber(data.profitReinvest, data.profit_reinvest), coinMargined ? conversionPrice : null),
    ProfitReduce: scaleUsdt(firstNumber(data.profitReduce, data.profit_reduce), coinMargined ? conversionPrice : null),
    ProfitWithdrawn: scaleUsdt(firstNumber(data.profitWithdrawn, data.profitWithdrawnU, data.profit_withdrawn, data.profitExited), coinMargined ? conversionPrice : null),
    ExtraMargin: extraMargin,
    MarginBalance: firstNumber(data.marginBalance, data.margin_balance),
    InitMargin: firstNumber(data.initMargin, data.initialMargin, data.usdtInvestment),
    RiskStatus: firstString(data.riskStatus, data.risk_status),
    MarginStatus: firstString(data.marginStatus, data.margin_status),
    EstimateLiqUp: liqUp,
    EstimateLiqDown: liqDown,
    LiquidationPrice: normalizedLiq.liq,
    LiquidationTriggered: Boolean(data.liquidationTriggered),
    MatchedGrids: firstNumber(data.matched, data.filledGrid, data.gridFilled),
    OrderCount: firstNumber(data.orderCount, data.order_count),
    Volume: firstNumber(data.volume, data.quoteVolume, data.tradeVolume),
    LossStopType: firstString(data.lossStopType),
    LossStop: firstString(data.lossStop),
    ProfitStopType: firstString(data.profitStopType),
    ProfitStop: firstString(data.profitStop),
    PausePrice: firstNumber(data.pausePrice, data.pause_price),
    MovingIndicatorType: firstString(data.movingIndicatorType),
    MovingTop: firstNumber(data.movingTop),
    MovingBottom: firstNumber(data.movingBottom),
    Complete: complete,
    ProfitCurrency: "USDT",
    RawGridProfit: rawGridProfit,
    RawGridProfitCurrency: coinMargined ? String(quote) : "USDT",
    RawQuoteInvestment: rawQuoteInvestment,
    RawQuoteInvestmentCurrency: coinMargined ? String(quote) : "USDT",
    ConversionPrice: conversionPrice,
    ConversionSymbol: conversionSymbol,
    ConversionTime: conversionTime,
    EstimatedStepPct: spacing.EstimatedStepPct,
    EstimatedStepPrice: spacing.EstimatedStepPrice,
    EstimatedRangePct: spacing.EstimatedRangePct,
    EstimateNote: "估算",
    RawJson: sanitizeRaw({ list: item, order }),
    Source: SOURCE,
  };
}

async function getWalletOverview(credentials) {
  const response = await requestPrivate("/api/v1/wallet/balancesFull", {}, credentials);
  const data = response.data || {};
  const categories = [];
  for (const item of ((data.botAccount && data.botAccount.detail) || [])) {
    categories.push({ account: "bot", type: item.type || "", title: item.title || "", totalInUsdt: item.totalInUsdt || null });
  }
  for (const item of ((data.traderAccount && data.traderAccount.detail) || [])) {
    categories.push({ account: "trader", type: item.type || "", title: item.title || "", totalInUsdt: item.totalInUsdt || null });
  }
  return {
    totalInUsdt: data.totalInUsdt || null,
    totalInBtc: data.totalInBtc || null,
    botInUsdt: data.botAccount && data.botAccount.totalInUsdt || null,
    traderInUsdt: data.traderAccount && data.traderAccount.totalInUsdt || null,
    categories,
  };
}

async function collect() {
  const credentialPath = option("--credentials", process.env.PIONEX_CREDENTIAL_PATH || DEFAULT_CREDENTIAL_PATH);
  const includeFinished = hasFlag("--include-finished");
  const credentials = readCredentials(credentialPath);
  const perpTickers = await getPerpTickers();
  const running = await listFuturesGrids(credentials, "running");
  if (!includeFinished && running.length === 0) throw new Error("No running futures-grid records were returned.");
  const listed = running.map((item) => ({ item, listStatus: "running" }));
  if (includeFinished) {
    const finished = await listFuturesGrids(credentials, "finished");
    const runningIds = new Set(running.map((item) => String(item.buOrderId)));
    for (const item of finished) {
      if (!runningIds.has(String(item.buOrderId))) listed.push({ item, listStatus: "finished" });
    }
  }
  const records = [];
  for (let index = 0; index < listed.length; index += 1) {
    records.push(await detailToRecord(listed[index].item, credentials, listed[index].listStatus, perpTickers));
    if (index + 1 < listed.length) await sleep(120);
  }
  const keys = new Set(records.map((record) => record.Key));
  if (keys.size !== records.length) throw new Error("Duplicate normalized grid key detected; no snapshot produced.");
  const ids = new Set(records.map((record) => record.ApiOrderId));
  if (ids.size !== records.length) throw new Error("Duplicate API order id detected; no snapshot produced.");
  if (records.length !== listed.length) throw new Error("API capture is incomplete: detail count does not match list count.");
  records.sort((a, b) => b.Created.localeCompare(a.Created));
  const runningRecords = records.filter((record) => record.ListStatus === "running");
  let wallet = null;
  try { wallet = await getWalletOverview(credentials); } catch (error) { wallet = { error: error.message }; }
  return {
    ok: true,
    CapturedAt: nowTaipeiIso(),
    Source: SOURCE,
    IncludeFinished: includeFinished,
    ExpectedCardCount: runningRecords.length,
    ContractCount: runningRecords.length,
    FinishedCount: records.length - runningRecords.length,
    SavingsCount: 0,
    Wallet: wallet,
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
