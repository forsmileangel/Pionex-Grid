import { state } from "../state.js";

import { app, both, bothSigned, cls, esc, eventChip, eventLabel, get, moneyNum, n, signed, tip, usdt } from "../common.js";

import { go, isCurrent, PAGE_LABELS } from "../router.js";

import { closureCard } from "./settlements.js";

import { effAxis, effFlagText, effFmt, effSeg, HOUR_MS, isoTaipei } from "./efficiency.js";

function fmt(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") {
    if (value.usdt != null) return usdt(value);
    try { return JSON.stringify(value); } catch { return String(value); }
  }
  return String(value);
}

function kvTable(rows) {
  if (!rows || !rows.length) return "<p class='muted'>無</p>";
  return `<table class="kv">${rows.map((r) => `<tr><th>${esc(r.key)}</th><td>${esc(fmt(r.value))}</td></tr>`).join("")}</table>`;
}

export async function renderHistory(seq) {
  const list = await get("/api/v1/grids-index");
  if (!isCurrent(seq, "history")) return;
  if (!list.length) { app.innerHTML = "<p class='muted'>沒有網格</p>"; return; }
  if (!state.historyId || !list.some((g) => g.bu_order_id === state.historyId)) state.historyId = list[0].bu_order_id;
  const data = await get("/api/v1/grids/" + encodeURIComponent(state.historyId) + "/history");
  if (!isCurrent(seq, "history")) return;
  const lifeLabel = { active: "運行中", closed: "已關倉", closed_unresolved: "關倉待補" };
  const options = list.map((g) => `<option value="${esc(g.bu_order_id)}" ${g.bu_order_id===state.historyId?"selected":""}>${esc(g.symbol)} · ${esc(g.created_at)} · ${esc(lifeLabel[g.lifecycle] || g.lifecycle)}</option>`).join("");
  const b = data.board || {};
  const pos = data.position || {};
  const backLabel = PAGE_LABELS[state.historyFrom] || "";
  const current = list.find((g) => g.bu_order_id === state.historyId) || {};
  let closure = null;
  if (current.lifecycle && current.lifecycle !== "active") {
    try { closure = ((await get("/api/v1/settlements")).rows || []).find((r) => r.id === state.historyId) || null; } catch (_) { closure = null; }
    if (!isCurrent(seq, "history")) return;
  }
  app.innerHTML = `
    ${backLabel ? `<div class="live-bar history-back"><button type="button" class="btn ghost" id="historyBack">返回${esc(backLabel)}</button></div>` : ""}
    ${closure ? closureCard(closure) : ""}
    <div class="card">
      <p><label>選擇網格 <select id="gridPick">${options}</select></label></p>
      <div class="stats">
        <div class="stat"><span class="label">標的</span><strong>${esc(pos.symbol || "—")}</strong></div>
        <div class="stat">${tip("倉位大小（派網投入）", "派網畫面上的倉位。調高槓桿、把保證金留在原倉時會變小。")}<strong>${both(b.investment || b.size)}</strong></div>
        <div class="stat">${tip("等效倉位", "投入＋追加保證金，就是你放進這一倉的全部保證金（不含浮動盈虧）。")}<strong>${both(b.equivalent || b.investment || b.size)}${moneyNum(b.extra_margin) ? `<span class="twd">其中追加保證金 ${usdt(b.extra_margin)} U</span>` : ""}</strong></div>
        <div class="stat">${tip("等效槓桿", "投入 × 派網槓桿 ÷（投入＋追加保證金）。派網設定的槓桿會因為調整而變，等效槓桿只有在加倉、補或拿走保證金時才會變。")}<strong>${b.equivalent_leverage == null ? "—" : n(b.equivalent_leverage, 2) + " 倍"}<span class="twd">派網設定 ${esc(pos.leverage || b.leverage || "—")}</span></strong></div>
        <div class="stat"><span class="label">網格利潤</span><strong>${both(b.grid_profit)}</strong></div>
        <div class="stat"><span class="label">過去 24 小時</span><strong class="${cls(b.profit_24h && b.profit_24h.usdt)}">${bothSigned(b.profit_24h)}</strong></div>
        <div class="stat"><span class="label">現價 / Mark</span><strong>${n(b.mark_price, 4)}</strong></div>
        <div class="stat"><span class="label">強平價</span><strong>${n(b.liq_price, 4)}</strong></div>
        <div class="stat"><span class="label">強平距離</span><strong>${b.liq_distance_pct == null ? "—" : n(b.liq_distance_pct, 2) + "%"}</strong></div>
        <div class="stat">${tip("目前總損益", "趨勢盈虧 + 網格利潤。資金費另外顯示，沒有加進去。")}<strong class="${cls(b.total_pnl && b.total_pnl.usdt)}">${bothSigned(b.total_pnl)}</strong></div>
        <div class="stat">${tip("趨勢盈虧", "價格相對開倉價的浮動盈虧：投入 × 槓桿 × 漲跌幅。")}<strong class="${cls(b.trend_profit && b.trend_profit.usdt)}">${bothSigned(b.trend_profit)}</strong></div>
        <div class="stat"><span class="label">資金費</span><strong class="${cls(b.funding && b.funding.usdt)}">${bothSigned(b.funding)}</strong></div>
        <div class="stat">${tip("網格年化", "用網格利潤、等效倉位（投入＋追加保證金）、持有天數推算，API 沒有現成欄位。調槓桿不會讓它跳動。")}<strong class="${cls(b.grid_annualized_pct)}">${b.grid_annualized_pct == null ? "—" : signed(b.grid_annualized_pct) + "%"}</strong></div>
        <div class="stat">${tip("總年化報酬", "用總損益（趨勢+網格）、等效倉位、持有天數推算。")}<strong class="${cls(b.annualized_pct)}">${b.annualized_pct == null ? "—" : signed(b.annualized_pct) + "%"}</strong></div>
      </div>
      <p class="muted">目前總損益 = 趨勢盈虧 + 網格利潤（不含資金費）。網格年化／總年化用等效倉位（投入＋追加保證金）與持有天數推算，API 沒有現成年化欄位。</p>
    </div>
    <div class="card"><div class="card-head"><h3>走勢</h3>
      <span class="note">長條＝每日利潤（左軸 USDT）；線＝這筆倉的歷史累計配對利潤（右軸）</span></div>
      <div class="chart" id="gridTrend"></div></div>
    <div class="card"><div class="card-head"><h3>${tip("盤中效率", "每 30 分鐘擷取一次這筆倉的網格利潤，換算成每個時段賺多少。資料從每小時記錄上線後開始累積。")}</h3>
      <span id="gridIntraCtl"></span></div>
      <div class="chart" id="gridIntra"></div></div>
    <div class="card">
      <h3>${tip("每日差額", "調高槓桿、保證金留在原倉時，投資額會變小，但等效倉位不變，當日差額也不受影響。")}</h3>
      <table class="plain"><thead><tr><th>日期</th><th>狀態</th><th>槓桿</th><th>投資額</th><th>等效倉位</th><th>前次利潤</th><th>當次利潤</th><th>當日差額</th><th>累計</th></tr></thead>
      <tbody>${(data.profits||[]).map((p)=>`<tr>
        <td>${esc(p.capture_date)}</td><td>${esc(eventLabel(p.event || p.status))}</td>
        <td>${esc(p.leverage || "—")}</td>
        <td>${usdt(p.investment)}</td>
        <td>${usdt(p.equivalent)}</td>
        <td>${usdt(p.prev_grid_profit)}</td>
        <td>${usdt(p.grid_profit)}</td>
        <td class="${cls(p.daily_profit && p.daily_profit.usdt)}">${signed(p.daily_profit && p.daily_profit.usdt)}</td>
        <td>${usdt(p.cumulative)}</td>
      </tr>`).join("")}</tbody></table>
    </div>
    <div class="card">
      <h3>本機快照欄位</h3>
      ${kvTable(data.snapshot_fields)}
    </div>
    <div class="card">
      <h3>API 網格詳情 buOrderData</h3>
      <p class="muted">每格下單量在 API 叫 perVolume，是每一格掛單的幣數量，不是 %。</p>
      ${kvTable(data.api_fields)}
    </div>
    <div class="card">
      <details open>
        <summary>API 訂單層 order</summary>
        ${kvTable(data.order_fields)}
      </details>
      <details>
        <summary>API 清單層 list</summary>
        ${kvTable(data.list_fields)}
      </details>
    </div>`;
  document.getElementById("gridPick").onchange = (e) => { state.historyId = e.target.value; go("history", { replace: true }); };
  drawGridTrend(data.profits || []);
  drawGridIntraday(state.historyId, seq);
  const backBtn = document.getElementById("historyBack");
  if (backBtn) backBtn.onclick = () => history.back();
}

function drawGridTrend(profits) {
  const el = document.getElementById("gridTrend");
  if (!el) return;
  const rows = profits.filter((p) => p.capture_date);
  const daily = rows.map((p) => (p.status === "closed_unresolved" ? null : moneyNum(p.daily_profit)));
  const life = rows.map((p) => (p.lifetime_i == null ? null : p.lifetime_i / 1e8));
  const accent = Charts.token("--accent", "#2e6970");
  Charts.plot(el, {
    n: rows.length, labels: rows.map((p) => p.capture_date.slice(5).replace("-", "/")), height: 200, label: "每日利潤與累計",
    empty: "還沒有每日紀錄",
    stacks: [
      { id: "up", color: accent, values: daily.map((v) => (v != null && v > 0 ? v : null)) },
      { id: "down", color: Charts.token("--down", "#b42318"), values: daily.map((v) => (v != null && v < 0 ? v : null)) },
    ],
    overlay: { values: life, color: Charts.token("--ink-2", "#3d4d54"), format: (v) => String(Math.round(v)) },
    yFormat: (v) => String(+v.toFixed(2)),
    markers: rows.map((p, i) => ({ p, i })).filter(({ p }) => eventChip(p.event)).map(({ p, i }) =>
      ({ i, tip: `${p.capture_date}：${eventLabel(p.event)}` })),
    tooltip: (i) => Charts.tipRows(rows[i].capture_date, [
      { color: accent, label: "單日利潤", value: daily[i] == null ? "待補" : signed(daily[i]) + " U", cls: cls(daily[i]) },
      { color: Charts.token("--ink-2", "#3d4d54"), label: "歷史累計配對", value: life[i] == null ? "—" : n(life[i], 2) + " U" },
      { label: "事件", value: eventLabel(rows[i].event || rows[i].status) },
    ]),
  });
}

let gridIntra = { range: "7d", g: "1h", metric: "usdt" };
let gridIntraSeq = 0;

async function drawGridIntraday(id, renderSeq) {
  const el = document.getElementById("gridIntra");
  const ctl = document.getElementById("gridIntraCtl");
  if (!el || !ctl) return;
  const seq = ++gridIntraSeq;
  const st = gridIntra;
  ctl.innerHTML = effSeg("gi-range", [["24h", "24 小時"], ["7d", "7 天"], ["30d", "30 天"]], st.range) + " "
    + effSeg("gi-grain", [["1h", "1 小時"], ["1d", "1 天"]], st.g) + " "
    + effSeg("gi-metric", [["usdt", "USDT"], ["eff", "日化 %"]], st.metric);
  ctl.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      if (btn.dataset.giRange) st.range = btn.dataset.giRange;
      if (btn.dataset.giGrain) st.g = btn.dataset.giGrain;
      if (btn.dataset.giMetric) st.metric = btn.dataset.giMetric;
      drawGridIntraday(id, renderSeq);
    };
  });
  const now = Date.now();
  const days = { "24h": 1, "7d": 7, "30d": 30 }[st.range];
  const qs = new URLSearchParams({ from: isoTaipei(now - days * 24 * HOUR_MS), to: isoTaipei(now), g: st.g, ids: id, extras: "0" });
  let d;
  try {
    d = await get("/api/v1/efficiency?" + qs.toString());
  } catch (err) {
    if (seq === gridIntraSeq && isCurrent(renderSeq, "history") && document.body.contains(el)) {
      el.innerHTML = `<p class="warn">${esc(err.message)}</p>`;
    }
    return;
  }
  if (seq !== gridIntraSeq || !isCurrent(renderSeq, "history") || !document.body.contains(el)) return;
  const s = d.series[id];
  const metric = st.metric;
  const values = s ? d.buckets.map((_, k) => (metric === "usdt" ? s.profit[k] : s.eff[k])) : [];
  const accent = Charts.token("--accent", "#2e6970");
  const label = metric === "usdt" ? "時段利潤" : "日化每U效率";
  const common = {
    n: d.buckets.length, times: d.buckets, g: d.granularity, height: 200, label,
    empty: st.g === "1h" ? "這段期間還沒有每 30 分鐘的資料（從每小時記錄上線後開始累積）" : "這段期間沒有資料",
    yFormat: effAxis(metric),
    markers: (d.events || []).filter((e) => e.id === id).map((e) => {
      let k = -1;
      d.buckets.forEach((t, i) => { if (t <= e.t) k = i; });
      return { i: k, tip: `${eventLabel(e.event)}（${Charts.fmtTime(e.t, "1h")}）` };
    }).filter((m) => m.i >= 0),
    tooltip: (k) => values[k] == null ? "" : Charts.tipRows(Charts.fmtRange(d.buckets[k], d.bucket_hours[k], d.granularity),
      [{ color: accent, label, value: effFmt(values[k], metric) + effFlagText(s.flag[k]), cls: cls(values[k]) }]),
  };
  if (!s) { Charts.plot(el, { ...common, n: 0 }); return; }
  if (metric === "usdt") {
    Charts.plot(el, { ...common, barFlags: s.flag, stacks: [
      { id: "up", color: accent, values: values.map((v) => (v != null && v > 0 ? v : null)) },
      { id: "down", color: Charts.token("--down", "#b42318"), values: values.map((v) => (v != null && v < 0 ? v : null)) },
    ] });
  } else {
    Charts.plot(el, { ...common, lines: [{ id, color: accent, values, flags: s.flag, strong: true }] });
  }
}
