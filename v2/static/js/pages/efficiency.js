import { state } from "../state.js";

import { app, askDialog, bindHistoryClicks, cls, esc, eventLabel, fmtTaipei, get, n, postJson, showFlash, signed, tip } from "../common.js";

import { go } from "../router.js";

// ---------------------------------------------------------------- 效率走勢
export const HOUR_MS = 3600 * 1000;
const EFF_DEFAULT = { positions: "all", range: "7d", from: "", to: "", granularity: "1h", metric: "usdt", chart: "line", shared_y: true };
const EFF_RANGES = [["today", "今天"], ["24h", "24 小時"], ["7d", "7 天"], ["30d", "30 天"], ["90d", "90 天"], ["all", "全部"], ["custom", "自訂"]];
const EFF_GRAINS = [["30m", "30 分", 0.5], ["1h", "1 小時", 1], ["4h", "4 小時", 4], ["1d", "1 天", 24], ["1w", "週", 168], ["1M", "月", 744]];
const EFF_METRICS = [["usdt", "區間利潤 USDT"], ["eff", "日化每U效率 %"], ["apr", "年化 %"]];
const EFF_CHARTS = [["line", "折線"], ["stack", "堆疊長條"], ["split", "分圖"]];
const EFF_FINE = new Set(["30m", "1h", "4h"]);
const EFF_FLAG = { 1: "未滿", 2: "估", 3: "日帳" };
let effMeta = null;
let effState = null;
let effData = null;
let effError = "";
let effNote = "";
let effBusy = false;
let effSeq = 0;
let effFocus = "";
let effSolo = "";
let effPickOpen = false;
let effView = "";
let effZoomBack = null;
let effRankSort = { key: "eff", dir: "desc" };

const pad2 = (v) => String(v).padStart(2, "0");
export function isoTaipei(ms) {
  const d = new Date(ms + 8 * HOUR_MS);
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}T${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}+08:00`;
}
const inputTaipei = (ms) => isoTaipei(ms).slice(0, 16);
const parseTaipeiInput = (v) => (v ? Date.parse(v + ":00+08:00") : NaN);
function taipeiMidnight(ms) {
  const p = Charts.parts(ms);
  return Date.UTC(p.y, p.mo - 1, p.d) - 8 * HOUR_MS;
}

const effGrainLabel = (g) => (EFF_GRAINS.find((x) => x[0] === g) || [g, g])[1];
function effPos(id) {
  return (effMeta && effMeta.byId && effMeta.byId[id]) || { id, label: String(id).slice(0, 8), color: 0, lifecycle: "active" };
}
const effColor = (id) => Charts.color(effPos(id).color);
const effSymbol = (p) => String(p.symbol || "?").replace("/USDT", "") + (p.product === "coin_margined_contract_grid" ? " 幣本位" : "");

function effSince() {
  const m = effMeta || {};
  const c = [m.ledger_since, m.intraday_since].map((v) => Date.parse(v || "")).filter(Number.isFinite);
  return c.length ? Math.min(...c) : Date.now() - 7 * 24 * HOUR_MS;
}

function effSpan(st = effState) {
  const now = Date.now();
  if (st.range === "custom") {
    const a = parseTaipeiInput(st.from), b = parseTaipeiInput(st.to);
    if (Number.isFinite(a) && Number.isFinite(b) && b > a) return [a, b];
    return [now - 7 * 24 * HOUR_MS, now];
  }
  if (st.range === "today") return [taipeiMidnight(now), now];
  if (st.range === "24h") return [now - 24 * HOUR_MS, now];
  if (st.range === "all") return [effSince(), now];
  const days = { "30d": 30, "90d": 90 }[st.range] || 7;
  return [now - days * 24 * HOUR_MS, now];
}

function effCount(span, g) {
  const hours = (EFF_GRAINS.find((x) => x[0] === g) || EFF_GRAINS[1])[2];
  return Math.ceil((span[1] - span[0]) / (hours * HOUR_MS)) + 1;
}
const effAllowed = (span, g) => effCount(span, g) <= ((effMeta && effMeta.max_buckets) || 2000);

// 顆粒度超過上限時自動調粗，回傳說明文字。
function effFit() {
  const span = effSpan();
  if (effAllowed(span, effState.granularity)) return "";
  const order = EFF_GRAINS.map((x) => x[0]);
  const old = effState.granularity;
  for (let i = order.indexOf(old) + 1; i < order.length; i++) {
    if (effAllowed(span, order[i])) {
      effState.granularity = order[i];
      return `區間太長，顆粒度已從 ${effGrainLabel(old)} 改成 ${effGrainLabel(order[i])}。`;
    }
  }
  return "";
}

function effAlive(p, span) {
  const start = Date.parse(p.ledger_since || p.intraday_since || "");
  if (!Number.isFinite(start)) return false;
  const last = Math.max(...[p.last_seen, p.ledger_until].map((v) => Date.parse(v || "")).filter(Number.isFinite), -Infinity);
  const end = p.lifecycle === "active" || !Number.isFinite(last) ? Date.now() : last;
  return start <= span[1] && end >= span[0] - 36 * HOUR_MS;
}

const effSelected = () => (effState.positions === "all" ? null : new Set(effState.positions));

function effNorm(s) {
  const out = {};
  Object.keys(EFF_DEFAULT).forEach((k) => { out[k] = s[k] === undefined ? EFF_DEFAULT[k] : s[k]; });
  if (Array.isArray(out.positions)) out.positions = out.positions.slice().sort();
  if (out.range !== "custom") { out.from = ""; out.to = ""; }
  return out;
}
const effSettings = () => effNorm(effState);
function effSavedViews() { return (effMeta && effMeta.views) || { views: [], default: null }; }
function effDirty() {
  const v = effSavedViews().views.find((x) => x.name === effView);
  return JSON.stringify(effSettings()) !== JSON.stringify(effNorm(v ? v.settings : {}));
}

function effApplyView(v, load = true) {
  const s = { ...EFF_DEFAULT, ...(v ? v.settings : {}) };
  if (Array.isArray(s.positions)) s.positions = s.positions.filter((id) => effMeta.byId[id]);
  else s.positions = "all";
  if (!EFF_GRAINS.some((x) => x[0] === s.granularity)) s.granularity = EFF_DEFAULT.granularity;
  if (!EFF_RANGES.some((x) => x[0] === s.range)) s.range = EFF_DEFAULT.range;
  if (!EFF_METRICS.some((x) => x[0] === s.metric)) s.metric = EFF_DEFAULT.metric;
  if (!EFF_CHARTS.some((x) => x[0] === s.chart) || (s.chart === "stack" && s.metric !== "usdt")) s.chart = "line";
  effState = s;
  effView = v ? v.name : "";
  effZoomBack = null;
  effSolo = "";
  effFocus = "";
  effNote = "";
  if (load) loadEfficiency();
}

function effVal(s, k) {
  if (!s) return null;
  if (effState.metric === "usdt") return s.profit[k];
  const e = s.eff[k];
  return e == null ? null : effState.metric === "apr" ? e * 365 : e;
}
function effTotalVal(k) {
  const t = effData.total;
  if (effState.metric === "usdt") return t.profit[k];
  const e = t.eff[k];
  return e == null ? null : effState.metric === "apr" ? e * 365 : e;
}
function effRankVal(r) {
  if (!r) return null;
  if (effState.metric === "usdt") return r.profit;
  return r.eff == null ? null : effState.metric === "apr" ? r.eff * 365 : r.eff;
}
export function effFmt(v, metric = effState.metric) {
  if (v == null || !Number.isFinite(v)) return "—";
  if (metric === "usdt") { const a = Math.abs(v); return signed(v, a >= 100 ? 1 : a >= 1 ? 2 : 3); }
  return signed(v, metric === "apr" ? 1 : 3) + "%";
}
export function effAxis(metric = effState.metric) {
  return (v) => String(+v.toFixed(4)) + (metric === "usdt" ? "" : "%");
}
export const effFlagText = (f) => (EFF_FLAG[f] ? `（${EFF_FLAG[f]}）` : "");

function effIds() {
  return Object.keys((effData && effData.series) || {}).sort((a, b) => effPos(a).color - effPos(b).color);
}

function effBucketIndex(t) {
  const b = effData.buckets;
  let lo = 0, hi = b.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (b[mid] <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

function effMarkers(ids) {
  const want = new Set(ids);
  const out = [];
  (effData.events || []).forEach((e) => {
    if (!want.has(e.id)) return;
    const k = effBucketIndex(e.t);
    if (k < 0) return;
    out.push({ i: k, color: effColor(e.id),
      tip: `${effPos(e.id).label}：${eventLabel(e.event)}（${Charts.fmtTime(e.t, "1h")}${e.source === "ledger" ? "，每日帳本" : ""}）` });
  });
  return out;
}

function effTip(k, lines, withTotal) {
  const d = effData;
  const rows = lines.filter((l) => l.values[k] != null).map((l) => ({
    color: l.color, label: effPos(l.id).label, v: l.values[k], cls: cls(l.values[k]),
    value: effFmt(l.values[k]) + effFlagText(l.flags && l.flags[k]),
  })).sort((a, b) => b.v - a.v);
  if (withTotal) {
    const tv = effTotalVal(k);
    if (tv != null) rows.unshift({ color: Charts.token("--accent", "#2e6970"), label: "合計（已選）", value: effFmt(tv) + effFlagText(d.total.flag[k]), cls: cls(tv) });
  }
  if (!rows.length) return "";
  return Charts.tipRows(Charts.fmtRange(d.buckets[k], d.bucket_hours[k], d.granularity), rows, "", 24);
}

function effZoom(i0, i1) {
  const d = effData;
  const from = d.buckets[i0];
  const to = d.buckets[i1] + d.bucket_hours[i1] * HOUR_MS;
  if (!effZoomBack) effZoomBack = { range: effState.range, from: effState.from, to: effState.to, granularity: effState.granularity };
  effState.range = "custom";
  effState.from = inputTaipei(from);
  effState.to = inputTaipei(to);
  // 放大後時段太少時改用較細的顆粒度；細顆粒度只在有每小時資料的區段才有意義。
  const order = EFF_GRAINS.map((x) => x[0]);
  const since = Date.parse((effMeta && effMeta.intraday_since) || "");
  let k = order.indexOf(effState.granularity);
  while (k > 0 && effCount([from, to], order[k]) < 10) {
    const finer = order[k - 1];
    if (EFF_FINE.has(finer) && !(Number.isFinite(since) && to > since)) break;
    k -= 1;
  }
  effState.granularity = order[k];
  effNote = "";
  loadEfficiency();
}

async function loadEfficiency() {
  const seq = ++effSeq;
  const fitted = effFit();
  if (fitted) effNote = fitted;
  const sel = effSelected();
  if (sel && !sel.size) {
    effData = null;
    effError = "";
    effBusy = false;
    if (state.page === "efficiency") paintEfficiency();
    return;
  }
  const [a, b] = effSpan();
  const qs = new URLSearchParams({ from: isoTaipei(a), to: isoTaipei(b), g: effState.granularity });
  if (sel) qs.set("ids", [...sel].join(","));
  effBusy = true;
  const busy = document.getElementById("effBusy");
  if (busy) busy.textContent = "載入中…";
  else if (state.page === "efficiency") paintEfficiency();
  try {
    const res = await fetch("/api/v1/efficiency?" + qs.toString());
    const data = await res.json().catch(() => ({}));
    if (seq !== effSeq) return;
    if (!res.ok) {
      if (data.suggest && data.suggest !== effState.granularity) {
        effNote = `區間太長，顆粒度已改成 ${effGrainLabel(data.suggest)}。`;
        effState.granularity = data.suggest;
        return loadEfficiency();
      }
      throw new Error(data.error || "HTTP " + res.status);
    }
    effData = data;
    effError = "";
  } catch (err) {
    if (seq !== effSeq) return;
    effError = err.message;
  }
  effBusy = false;
  if (state.page === "efficiency") paintEfficiency();
}

export async function renderEfficiency() {
  const meta = await get("/api/v1/efficiency/meta");
  meta.byId = Object.fromEntries((meta.positions || []).map((p) => [p.id, p]));
  effMeta = meta;
  if (!effState) {
    const saved = effSavedViews();
    effApplyView(saved.views.find((v) => v.name === saved.default), false);
  }
  paintEfficiency();
  await loadEfficiency();
}

export function effSeg(attr, items, cur, disabled) {
  return `<div class="seg" role="group">${items.map(([v, label]) =>
    `<button type="button" data-${attr}="${v}" class="${v === cur ? "on" : ""}"${disabled && disabled(v) ? " disabled" : ""}>${label}</button>`).join("")}</div>`;
}

function effControlsHtml(span) {
  const st = effState;
  const sel = effSelected();
  const isOn = (id) => !sel || sel.has(id);
  const groups = new Map();
  effMeta.positions.forEach((p) => {
    if (!effAlive(p, span) && !(sel && sel.has(p.id))) return;
    const key = effSymbol(p);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  });
  const chips = `<button type="button" class="chip${sel ? "" : " on"}" data-chip="">全部</button>`
    + [...groups].map(([sym, list]) => {
      const on = list.filter((p) => isOn(p.id)).length;
      const state = on === list.length ? " on" : on ? " part" : "";
      return `<button type="button" class="chip${state}" data-chip="${esc(sym)}">${esc(sym)}${list.length > 1 ? `<span class="n">${on}/${list.length}</span>` : ""}</button>`;
    }).join("")
    + `<button type="button" class="chip ghost" id="effPick">${effPickOpen ? "收起細選" : "細選倉位"}</button>`;
  const pick = !effPickOpen ? "" : `<div class="pick">${[...groups.values()].flat().map((p) =>
    `<label class="pick-item"><input type="checkbox" data-pick="${esc(p.id)}" ${isOn(p.id) ? "checked" : ""}>`
    + `<i class="sw" style="background:${Charts.color(p.color)}"></i>${esc(p.label)}`
    + `${p.lifecycle !== "active" ? ' <span class="badge">已關倉</span>' : ""}</label>`).join("")}</div>`;
  const saved = effSavedViews();
  const isDefault = effView && effView === saved.default;
  return `<div class="card eff-controls">
    <div class="eff-row"><span class="lbl">標的</span><div class="chips">${chips}</div></div>
    ${pick}
    <div class="eff-row"><span class="lbl">區間</span>${effSeg("range", EFF_RANGES, st.range)}
      ${st.range === "custom" ? `<span class="eff-custom"><input type="datetime-local" id="effFrom" value="${esc(st.from || inputTaipei(span[0]))}">至
        <input type="datetime-local" id="effTo" value="${esc(st.to || inputTaipei(span[1]))}"><button type="button" class="btn tiny" id="effApply">套用</button></span>` : ""}
      ${effZoomBack ? `<button type="button" class="btn ghost tiny" id="effUnzoom">重設縮放</button>` : ""}</div>
    <div class="eff-row"><span class="lbl">顆粒度</span>${effSeg("grain", EFF_GRAINS, st.granularity, (g) => !effAllowed(span, g))}
      <span class="lbl">指標</span>${effSeg("metric", EFF_METRICS, st.metric)}</div>
    <div class="eff-row"><span class="lbl">圖型</span>${effSeg("chart", EFF_CHARTS, st.chart, (c) => c === "stack" && st.metric !== "usdt")}
      ${st.chart === "split" ? `<label class="eff-custom"><input type="checkbox" id="effSharedY" ${st.shared_y ? "checked" : ""}> 共用 Y 軸</label>` : ""}
      <div class="eff-views"><span class="eff-busy" id="effBusy">${effBusy ? "載入中…" : ""}</span>
        <label class="eff-custom">常用檢視 <select id="effView"><option value="">內建：全部倉位</option>${saved.views.map((v) =>
          `<option value="${esc(v.name)}"${v.name === effView ? " selected" : ""}>${esc(v.name)}${v.name === saved.default ? " ★" : ""}</option>`).join("")}</select></label>
        ${effDirty() ? `<span class="eff-busy">（已修改）</span>` : ""}
        <button type="button" class="btn tiny" id="effSave">儲存目前設定</button>
        <button type="button" class="btn ghost tiny" id="effStar" ${effView ? "" : "disabled"}>${isDefault ? "取消預設" : "設為預設 ★"}</button>
        <button type="button" class="btn ghost tiny" id="effDel" ${effView ? "" : "disabled"}>刪除</button>
      </div></div>
  </div>`;
}

function effNoticesHtml(span) {
  const notes = [];
  if (effNote) notes.push(effNote);
  const since = Date.parse(effMeta.intraday_since || "");
  if (!Number.isFinite(since)) {
    notes.push("每小時資料會從下一次 30 分鐘排程開始累積。1 天以上的顆粒度可以先看每日帳本（08:05 截點，圖上以虛線、空心點標示）。");
  } else if (EFF_FINE.has(effState.granularity) && span[0] < since) {
    notes.push(`每小時資料從 ${fmtTaipei(effMeta.intraday_since).slice(0, 16)} 開始累積，之前只有每日帳本；要看更早的走勢請切到 1 天以上。`);
  }
  return (effError ? `<div class="notice is-error" style="margin:0 0 10px"><span>讀取失敗：${esc(effError)}</span></div>` : "")
    + notes.map((t) => `<div class="notice" style="margin:0 0 10px"><span>${esc(t)}</span></div>`).join("");
}

function effStatsHtml() {
  const sum = effData.summary || {};
  const best = (effData.ranking || []).find((r) => r.eff != null);
  const heatTotal = effData.heatmap && effData.heatmap.total;
  let bestHour = null;
  if (heatTotal) heatTotal.eff.forEach((v, h) => { if (v != null && (bestHour == null || v > heatTotal.eff[bestHour])) bestHour = h; });
  const small = 'style="font-size:17px;white-space:normal;line-height:1.35"';
  return `<div class="stats">
    <div class="stat">${tip("區間總利潤", "已選倉位在這段期間的網格利潤加總。複投、加倉、提領已依每日帳本同一套規則排除；幣本位依當下幣價換算。")}
      <strong class="${cls(sum.profit)}">${signed(sum.profit)}<span class="unit">USDT</span></strong></div>
    <div class="stat">${tip("平均日化每U效率", "區間利潤 ÷ 倉位 × 3 ÷ 槓桿，換算成每天 %，合計＝總利潤 ÷（投入 × 槓桿 ÷ 3）× 時數，調槓桿但保證金留在原倉不會讓它跳動。和其他頁「每U效率」同一口徑。")}
      <strong class="${cls(sum.eff)}">${sum.eff == null ? "—" : signed(sum.eff, 3) + "%"}<span class="twd">年化 ${sum.eff == null ? "—" : signed(sum.eff * 365, 1) + "%"}</span></strong></div>
    <div class="stat">${tip("效率最好的倉位", "這段期間日化每U效率最高的倉位。")}
      <strong ${small}>${best ? esc(effPos(best.id).label) : "—"}${best ? `<span class="pct ${cls(best.eff)}">${signed(best.eff, 3)}%／日</span>` : ""}</strong></div>
    <div class="stat">${tip("效率最好的時段", "依下方「一天中的時段」熱度圖，已選倉位合計效率最高的一小時（只用每 30 分鐘實測的資料，最多看最近 180 天）。")}
      <strong ${small}>${bestHour == null ? "累積中" : `${pad2(bestHour)}:00–${pad2((bestHour + 1) % 24)}:00`}${bestHour == null ? ""
        : `<span class="pct ${cls(heatTotal.eff[bestHour])}">${signed(heatTotal.eff[bestHour], 3)}%／日</span>`}</strong></div>
  </div>`;
}

function effRankingHtml() {
  const rows = (effData.ranking || []).slice();
  const key = effRankSort.key, dir = effRankSort.dir === "asc" ? 1 : -1;
  const val = (r) => key === "label" ? effPos(r.id).label : key === "best" ? (r.best && r.best.eff)
    : key === "worst" ? (r.worst && r.worst.eff) : key === "apr" ? (r.eff == null ? null : r.eff * 365) : r[key];
  rows.sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === "string") return x.localeCompare(y, "zh-Hant") * dir;
    return (x - y) * dir;
  });
  const g = effData.granularity;
  const th = (k, label, note) => `<th class="sortable" data-rank="${k}">${note ? tip(label, note) : label}${effRankSort.key === k ? (effRankSort.dir === "asc" ? " ▲" : " ▼") : ""}</th>`;
  const when = (b) => b ? `<span class="${cls(b.eff)}">${signed(b.eff, 3)}%</span><small>${esc(Charts.fmtRange(b.t, effData.bucket_hours[effBucketIndex(b.t)] || 1, g))}</small>` : "—";
  return `<table class="plain eff-rank" style="min-width:900px"><colgroup><col style="width:25%"><col><col><col><col><col><col style="width:13%"><col></colgroup>
    <thead><tr>${th("label", "倉位")}${th("profit", "區間利潤")}${th("eff", "日化每U效率", "區間利潤 ÷ 倉位 × 3 ÷ 槓桿，換算成每天 %。")}${th("apr", "年化")}
      ${th("positive_ratio", "正報酬時段", "有賺錢（利潤大於 0）的時段比例。")}${th("best", "最佳時段")}${th("worst", "最差時段")}
      ${th("coverage", "實測覆蓋", "有每 30 分鐘實測資料的時數比例；其餘是跨過空檔的估算或每日帳本。")}</tr></thead>
    <tbody>${rows.map((r) => `<tr class="clickable" data-id="${esc(r.id)}">
      <td><i class="sw" style="background:${effColor(r.id)}"></i> ${esc(effPos(r.id).label)}${effPos(r.id).lifecycle !== "active" ? ' <span class="badge">已關倉</span>' : ""}</td>
      <td class="${cls(r.profit)}">${signed(r.profit)}</td>
      <td class="${cls(r.eff)}">${r.eff == null ? "—" : signed(r.eff, 3) + "%"}</td>
      <td class="${cls(r.eff)}">${r.eff == null ? "—" : signed(r.eff * 365, 1) + "%"}</td>
      <td>${n(r.positive_ratio * 100, 0)}%</td>
      <td>${when(r.best)}</td><td>${when(r.worst)}</td>
      <td>${r.coverage == null ? "—" : n(r.coverage * 100, 0) + "%"}</td></tr>`).join("")
      || `<tr><td colspan="8" class="muted">這段期間沒有資料</td></tr>`}</tbody></table>`;
}

function paintEfficiency() {
  if (!effMeta || !effState) return;
  const span = effSpan();
  const sel = effSelected();
  let body;
  if (sel && !sel.size) body = `<div class="card"><p class="muted">請在上方至少選一個倉位。</p></div>`;
  else if (!effData) body = `<div class="card"><p class="muted">${effError ? "沒有資料" : "載入中…"}</p></div>`;
  else {
    const cov = effData.summary && effData.summary.coverage;
    body = effStatsHtml()
      + `<div class="card"><div class="card-head"><h3>${tip("合計（已選倉位）", "已選倉位每個時段的合計。效率＝總利潤 ÷（投入 × 槓桿 ÷ 3）× 時數，不受調槓桿影響。")}</h3>
          <span class="note">${cov == null ? "" : `實測覆蓋 ${n(cov * 100, 0)}%　`}拖曳圖表可放大區間</span></div>
          <div class="chart" id="effTotal"></div></div>
        <div class="card"><div class="card-head"><h3>各倉位</h3><span class="note">滑過圖例突顯、點一下只看那一筆；拖曳可放大</span></div>
          <div class="chart" id="effMain"></div>
          <div class="chart-legend" id="effLegend"></div>
          <p class="note">實線＝每 30 分鐘實測；細虛線、空心點＝跨過空檔的估算或每日帳本（08:05 截點）；粗虛線＝已關倉；三角形＝加倉、複投、提領等事件。標「未滿」的時段還在進行中。</p></div>
        <div class="card"><div class="card-head"><h3>${tip("一天中的時段", "把每一天同一個小時（台北時間）的表現平均起來，看哪些時段效率比較好。只用每 30 分鐘實測的資料，最多看最近 180 天。點倉位名稱可看單一網格。")}</h3>
          <span class="note">${effData.heatmap ? `自 ${esc(fmtTaipei(effData.heatmap.from).slice(0, 10))} 起` : ""}</span></div>
          <div id="effHeat"></div></div>
        <div class="card"><div class="card-head"><h3>倉位排行</h3>
          <button type="button" class="btn ghost tiny" id="effCsv">匯出 CSV</button></div>
          ${effRankingHtml()}</div>`;
  }
  app.innerHTML = effControlsHtml(span) + effNoticesHtml(span) + body;
  if (effData && !(sel && !sel.size)) {
    effDrawTotal();
    effDrawMain();
    effDrawLegend();
    effDrawHeat();
  }
  bindEfficiency(span);
}

function effDrawTotal() {
  const el = document.getElementById("effTotal");
  if (!el) return;
  const d = effData;
  const vals = d.buckets.map((_, k) => effTotalVal(k));
  const accent = Charts.token("--accent", "#2e6970");
  const common = {
    n: d.buckets.length, times: d.buckets, g: d.granularity, height: 190, yFormat: effAxis(), onBrush: effZoom,
    label: "已選倉位合計",
    tooltip: (k) => vals[k] == null ? "" : Charts.tipRows(Charts.fmtRange(d.buckets[k], d.bucket_hours[k], d.granularity),
      [{ color: accent, label: "合計（已選）", value: effFmt(vals[k]) + effFlagText(d.total.flag[k]), cls: cls(vals[k]) }]),
  };
  if (effState.metric === "usdt") {
    Charts.plot(el, { ...common, barFlags: d.total.flag, stacks: [
      { id: "up", color: accent, values: vals.map((v) => (v != null && v > 0 ? v : null)) },
      { id: "down", color: Charts.token("--down", "#b42318"), values: vals.map((v) => (v != null && v < 0 ? v : null)) },
    ] });
  } else {
    Charts.plot(el, { ...common, lines: [{ id: "total", color: accent, values: vals, flags: d.total.flag, strong: true }] });
  }
}

function effLine(id) {
  const s = effData.series[id];
  return {
    id, color: effColor(id), values: effData.buckets.map((_, k) => effVal(s, k)), flags: s.flag,
    faded: !!effFocus && effFocus !== id, strong: effFocus === id, dashed: effPos(id).lifecycle !== "active",
  };
}

function effDrawMain() {
  const el = document.getElementById("effMain");
  if (!el || !effData) return;
  const d = effData;
  const ids = effIds().filter((id) => !effSolo || id === effSolo);
  const base = { n: d.buckets.length, times: d.buckets, g: d.granularity, yFormat: effAxis(), onBrush: effZoom };
  if (effState.chart === "split") {
    el._chartDraw = null;
    const ranks = Object.fromEntries((d.ranking || []).map((r) => [r.id, r]));
    el.innerHTML = `<div class="minis">${ids.map((id) => {
      const v = effRankVal(ranks[id]);
      return `<div class="mini"><h4><span><i class="sw" style="background:${effColor(id)}"></i> ${esc(effPos(id).label)}</span>`
        + `<span class="${cls(v)}">${effFmt(v)}</span></h4><div class="chart" data-mini="${esc(id)}"></div></div>`;
    }).join("") || `<div class="chart-empty">這段期間沒有資料</div>`}</div>`;
    let domain = null;
    if (effState.shared_y) {
      let lo = Infinity, hi = -Infinity;
      ids.forEach((id) => effLine(id).values.forEach((v) => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
      if (Number.isFinite(lo)) domain = [lo, hi];
    }
    el.querySelectorAll("[data-mini]").forEach((node) => {
      const line = { ...effLine(node.dataset.mini), faded: false, strong: false };
      Charts.plot(node, { ...base, height: 150, yDomain: domain, lines: [line], markers: effMarkers([line.id]),
        label: effPos(line.id).label, tooltip: (k) => effTip(k, [line], false) });
    });
    return;
  }
  const lines = ids.map(effLine);
  const common = { ...base, height: 330, markers: effMarkers(ids), label: "各倉位走勢", tooltip: (k) => effTip(k, lines, !effSolo) };
  if (effState.chart === "stack" && effState.metric === "usdt") {
    Charts.plot(el, { ...common, barFlags: d.total.flag,
      stacks: lines.map((l) => ({ id: l.id, color: l.color, values: l.values, faded: l.faded })) });
  } else {
    Charts.plot(el, { ...common, lines });
  }
}

function effDrawLegend() {
  const el = document.getElementById("effLegend");
  if (!el) return;
  const ranks = Object.fromEntries((effData.ranking || []).map((r) => [r.id, r]));
  el.innerHTML = effIds().map((id) => {
    const p = effPos(id);
    const v = effRankVal(ranks[id]);
    return `<button type="button" data-legend="${esc(id)}" class="${effSolo === id ? "solo" : ""}${p.lifecycle !== "active" ? " closed" : ""}">`
      + `<i class="sw" style="background:${effColor(id)}"></i>${esc(p.label)}${v == null ? "" : ` <b class="${cls(v)}">${effFmt(v)}</b>`}</button>`;
  }).join("");
  el.querySelectorAll("[data-legend]").forEach((btn) => {
    const id = btn.dataset.legend;
    btn.onmouseenter = () => { if (effState.chart !== "split") { effFocus = id; effDrawMain(); } };
    btn.onmouseleave = () => { if (effFocus) { effFocus = ""; effDrawMain(); } };
    btn.onclick = () => { effSolo = effSolo === id ? "" : id; effFocus = ""; effDrawMain(); effDrawLegend(); };
  });
}

function effDrawHeat() {
  const el = document.getElementById("effHeat");
  if (!el) return;
  const h = effData.heatmap;
  if (!h) {
    el.innerHTML = `<div class="chart-empty">每 30 分鐘的實測資料累積之後，這裡會顯示一天 24 個時段的平均表現。</div>`;
    return;
  }
  const m = effState.metric;
  const pick = (row) => (m === "usdt" ? row.profit : row.eff.map((v) => (v == null ? null : m === "apr" ? v * 365 : v)));
  const tips = (label, row, vals) => vals.map((v, i) =>
    `${label}｜${pad2(i)}:00–${pad2((i + 1) % 24)}:00 平均 ${effFmt(v)}（${row.n[i] || 0} 個時段）`);
  const rows = [{ label: "合計（已選）", color: Charts.token("--accent", "#2e6970"), strong: true, scale: "total", values: pick(h.total), tips: tips("合計", h.total, pick(h.total)) }];
  effIds().forEach((id) => {
    const row = h.rows[id];
    if (!row) return;
    const vals = pick(row);
    rows.push({ id, label: effPos(id).label, color: effColor(id), values: vals, tips: tips(effPos(id).label, row, vals) });
  });
  Charts.heat(el, { cols: Array.from({ length: 24 }, (_, i) => pad2(i)), rows,
    onRowClick: (id) => { state.historyFrom = "efficiency"; state.historyId = id; go("history"); } });
}

function effCsv() {
  const d = effData;
  if (!d) return;
  const ids = effIds();
  const metric = (EFF_METRICS.find((x) => x[0] === effState.metric) || ["", ""])[1];
  const num = (v) => (v == null || !Number.isFinite(v) ? "" : String(+v.toFixed(6)));
  const rows = [[`時段開始（台北）／${metric}`, ...ids.map((id) => effPos(id).label), "合計"]];
  d.buckets.forEach((t, k) => {
    rows.push([isoTaipei(t).slice(0, 16).replace("T", " "), ...ids.map((id) => num(effVal(d.series[id], k))), num(effTotalVal(k))]);
  });
  const quote = (c) => (/[",\r\n]/.test(c) ? `"${c.replace(/"/g, '""')}"` : c);
  const csv = "﻿" + rows.map((r) => r.map(quote).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = `pionex-efficiency-${effState.metric}-${d.granularity}-${isoTaipei(Date.now()).slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function effStoreViews(doc, message) {
  try {
    effMeta.views = await postJson("/api/v1/efficiency/views", doc);
    if (message) showFlash(message);
  } catch (err) {
    showFlash("常用檢視儲存失敗：" + err.message, "error");
  }
  if (state.page === "efficiency") paintEfficiency();
}

function effSetPositions(ids) {
  const all = effMeta.positions.map((p) => p.id);
  const set = new Set(ids);
  effState.positions = all.every((id) => set.has(id)) ? "all" : all.filter((id) => set.has(id));
  effSolo = "";
  loadEfficiency();
}

function bindEfficiency(span) {
  const st = effState;
  const current = () => new Set(st.positions === "all" ? effMeta.positions.map((p) => p.id) : st.positions);
  app.querySelectorAll("[data-chip]").forEach((btn) => {
    btn.onclick = () => {
      const sym = btn.dataset.chip;
      if (!sym) { st.positions = "all"; effSolo = ""; loadEfficiency(); return; }
      const group = effMeta.positions.filter((p) => effSymbol(p) === sym && (effAlive(p, span) || current().has(p.id))).map((p) => p.id);
      const set = current();
      const allOn = group.every((id) => set.has(id));
      group.forEach((id) => (allOn ? set.delete(id) : set.add(id)));
      effSetPositions([...set]);
    };
  });
  const pickBtn = document.getElementById("effPick");
  if (pickBtn) pickBtn.onclick = () => { effPickOpen = !effPickOpen; paintEfficiency(); };
  app.querySelectorAll("[data-pick]").forEach((box) => {
    box.onchange = () => {
      const set = current();
      if (box.checked) set.add(box.dataset.pick); else set.delete(box.dataset.pick);
      effSetPositions([...set]);
    };
  });
  app.querySelectorAll("[data-range]").forEach((btn) => {
    btn.onclick = () => {
      const v = btn.dataset.range;
      if (v === "custom") {
        const cur = effSpan();
        st.from = st.from || inputTaipei(cur[0]);
        st.to = st.to || inputTaipei(cur[1]);
      }
      st.range = v;
      effZoomBack = null;
      effNote = "";
      if (v === "custom") paintEfficiency(); else loadEfficiency();
    };
  });
  const apply = document.getElementById("effApply");
  if (apply) apply.onclick = () => {
    const from = document.getElementById("effFrom").value, to = document.getElementById("effTo").value;
    if (!(parseTaipeiInput(to) > parseTaipeiInput(from))) { showFlash("結束時間要晚於開始時間", "error"); return; }
    st.from = from;
    st.to = to;
    effNote = "";
    loadEfficiency();
  };
  const unzoom = document.getElementById("effUnzoom");
  if (unzoom) unzoom.onclick = () => {
    Object.assign(st, effZoomBack);
    effZoomBack = null;
    effNote = "";
    loadEfficiency();
  };
  app.querySelectorAll("[data-grain]").forEach((btn) => {
    btn.onclick = () => { st.granularity = btn.dataset.grain; effNote = ""; loadEfficiency(); };
  });
  app.querySelectorAll("[data-metric]").forEach((btn) => {
    btn.onclick = () => {
      st.metric = btn.dataset.metric;
      if (st.metric !== "usdt" && st.chart === "stack") st.chart = "line";
      paintEfficiency();
    };
  });
  app.querySelectorAll("[data-chart]").forEach((btn) => {
    btn.onclick = () => { st.chart = btn.dataset.chart; effFocus = ""; paintEfficiency(); };
  });
  const shared = document.getElementById("effSharedY");
  if (shared) shared.onchange = () => { st.shared_y = shared.checked; effDrawMain(); };
  const viewSel = document.getElementById("effView");
  if (viewSel) viewSel.onchange = () => {
    effApplyView(effSavedViews().views.find((v) => v.name === viewSel.value));
  };
  const save = document.getElementById("effSave");
  if (save) save.onclick = async () => {
    const saved = effSavedViews();
    const got = await askDialog({
      title: "儲存常用檢視",
      body: "會記住目前的標的、區間、顆粒度、指標和圖型。區間按鈕（例如 7 天）會存成相對時間，下次打開會從當下往回算。",
      input: { label: "名稱", value: effView, max: 40, check: { label: "設為預設（打開這一頁時自動套用）", value: saved.default === effView && !!effView } },
      okText: "儲存",
    });
    if (!got) return;
    if (!got.value) { showFlash("請輸入名稱", "error"); return; }
    const views = saved.views.slice();
    const entry = { name: got.value, settings: effSettings() };
    const at = views.findIndex((v) => v.name === got.value);
    if (at >= 0) views[at] = entry; else views.push(entry);
    const def = got.check ? got.value : saved.default === got.value ? null : saved.default;
    effView = got.value;
    await effStoreViews({ views, default: def }, `已儲存常用檢視「${got.value}」。`);
  };
  const star = document.getElementById("effStar");
  if (star) star.onclick = () => {
    const saved = effSavedViews();
    const def = saved.default === effView ? null : effView;
    effStoreViews({ views: saved.views, default: def }, def ? `打開這一頁時會自動套用「${def}」。` : "已取消預設，打開這一頁會顯示全部倉位。");
  };
  const del = document.getElementById("effDel");
  if (del) del.onclick = async () => {
    if (!effView) return;
    const ok = await askDialog({ title: `刪除常用檢視「${effView}」？`, body: "只刪掉這組設定，資料不受影響。", okText: "刪除", danger: true });
    if (!ok) return;
    const saved = effSavedViews();
    const name = effView;
    effView = "";
    await effStoreViews({ views: saved.views.filter((v) => v.name !== name), default: saved.default === name ? null : saved.default }, `已刪除「${name}」。`);
  };
  app.querySelectorAll("[data-rank]").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.rank;
      if (effRankSort.key === k) effRankSort.dir = effRankSort.dir === "asc" ? "desc" : "asc";
      else effRankSort = { key: k, dir: k === "label" ? "asc" : "desc" };
      paintEfficiency();
    };
  });
  const csv = document.getElementById("effCsv");
  if (csv) csv.onclick = effCsv;
  bindHistoryClicks();
}
