import { state } from "../state.js";

import { app, bindHistoryClicks, both, bothSigned, cls, esc, eventCell, get, moneyNum, n, postJson, publishText, showFlash, signed, tip, usdt } from "../common.js";

import { effSeg } from "./efficiency.js";

let calendarData = null;
let calMonth = null;
let calSelected = "";
let calLine = "total";

function paintCalendar() {
  const days = (calendarData && calendarData.days) || [];
  if (!days.length) {
    app.innerHTML = "<p class='muted'>還沒有每日紀錄。請先執行 python -m v2.capture</p>";
    return;
  }
  const byDate = {};
  days.forEach((d) => { if (d.capture_date) byDate[d.capture_date] = d; });
  const latest = days[0].capture_date;
  if (!calSelected || !byDate[calSelected]) calSelected = latest;
  const parts = calSelected.split("-").map(Number);
  if (!calMonth) calMonth = { y: parts[0], m: parts[1] };
  const y = calMonth.y, m = calMonth.m;
  const first = new Date(y, m - 1, 1);
  const startPad = first.getDay();
  const dim = new Date(y, m, 0).getDate();
  const dows = ["日","一","二","三","四","五","六"];
  const monthKey = `${y}-${String(m).padStart(2, "0")}-`;
  // 格子深淺依當月單日利潤的最大絕對值；有關倉待補的日子不上色。
  let heatMax = 0;
  days.forEach((d) => {
    if (!String(d.capture_date || "").startsWith(monthKey) || Number(d.unresolved_count) > 0) return;
    const v = moneyNum(d.daily_profit);
    if (v != null) heatMax = Math.max(heatMax, Math.abs(v));
  });
  const cellHeat = (rec) => {
    const v = moneyNum(rec.daily_profit);
    if (!heatMax || v == null || v === 0 || Number(rec.unresolved_count) > 0) return "";
    const a = (0.05 + 0.3 * Math.min(1, Math.abs(v) / heatMax)).toFixed(3);
    return ` style="--cell-bg:rgba(var(${v > 0 ? "--heat-pos" : "--heat-neg"}),${a})"`;
  };
  let cells = dows.map((d) => `<div class="cal-dow">${d}</div>`).join("");
  for (let i = 0; i < startPad; i++) cells += `<div class="cal-cell is-pad"></div>`;
  for (let d = 1; d <= dim; d++) {
    const dateStr = `${y}-${String(m).padStart(2,"0")}-${String(d).padStart(2,"0")}`;
    const rec = byDate[dateStr];
    const sel = dateStr === calSelected ? " is-selected" : "";
    if (rec) {
      const dayV = rec.daily_profit && rec.daily_profit.usdt;
      const cumV = rec.cumulative && rec.cumulative.usdt;
      cells += `<button type="button" class="cal-cell${sel}" data-date="${dateStr}"${cellHeat(rec)}><span class="cal-num">${d}</span>`
        + `<span class="cal-amt"><span>單日</span><b class="${cls(dayV)}">${dayV == null || dayV === "" ? "—" : signed(dayV) + "U"}</b></span>`
        + `<span class="cal-amt"><span>運行中</span><b>${cumV == null || cumV === "" ? "—" : n(cumV, 2) + "U"}</b></span></button>`;
    } else {
      cells += `<div class="cal-cell is-empty${sel}"><span class="cal-num">${d}</span></div>`;
    }
  }
  // 與 Portfolio 月平均同一規則：有關倉配對待補的日子先不計入。
  const prefix = `${y}-${String(m).padStart(2, "0")}-`;
  let monthTotal = 0, monthDays = 0, monthSkipped = 0;
  days.forEach((d) => {
    if (!String(d.capture_date || "").startsWith(prefix)) return;
    const v = moneyNum(d.daily_profit);
    if (Number(d.unresolved_count) > 0 || v == null) { monthSkipped += 1; return; }
    monthTotal += v;
    monthDays += 1;
  });
  const monthSum = monthDays
    ? `本月合計 <b class="${cls(monthTotal)}">${signed(monthTotal)}</b> U · 日均 <b class="${cls(monthTotal)}">${signed(monthTotal / monthDays)}</b> U（${monthDays} 天${monthSkipped ? `，${monthSkipped} 天待補未計` : ""}）`
    : "本月尚無完整紀錄";
  const selected = byDate[calSelected] || {};
  const rows = selected.rows || [];
  const rowHtml = rows.length ? rows.map((r) => {
    const day = r.daily_profit && r.daily_profit.usdt;
    return `<tr class="clickable" data-id="${esc(r.bu_order_id || "")}">
      <td>${esc(r.symbol || "—")}</td>
      <td>${eventCell(r, calSelected)}</td>
      <td class="n">${usdt(r.investment || r.size)}</td>
      <td class="n ${cls(day)}">${signed(day)}</td>
      <td class="n">${usdt(r.grid_profit)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="muted">當日無倉位明細</td></tr>`;
  app.innerHTML = `
    <div class="card">
      <div class="cal-head">
        <button type="button" id="calPrev" aria-label="上個月">‹</button>
        <strong>${y} 年 ${m} 月</strong>
        <button type="button" id="calNext" aria-label="下個月">›</button>
      </div>
      <p class="cal-sum">${monthSum}</p>
      <div class="card-head" style="margin-top:14px"><h3>${calLine === "running"
          ? tip("每日利潤與運行中配對利潤", "線只算當天還開著的倉（派網畫面上的網格利潤加總）。關倉、複投時會往下掉，不代表虧錢。點長條可切換到那一天。")
          : tip("每日利潤與歷史累計配對利潤", "已扣除關倉、複投、提領造成的跳動，只反映實際賺到的配對收益。點長條可切換到那一天。")}</h3>
        ${effSeg("calline", [["total", "含已關倉"], ["running", "只算運行中"]], calLine)}</div>
      <p class="note" style="margin:-4px 0 6px">長條＝單日利潤（左軸 USDT）；線＝${calLine === "running" ? "運行中配對利潤，不含已關倉" : "歷史累計配對利潤，含已關倉"}（右軸 USDT）</p>
      <div class="chart" id="calBars"></div>
      <div class="cal">${cells}</div>
      <div class="legend">
        <span><b>單日</b> 當日網格利潤，基準日為 0；幣本位是當天新增的幣×抓取現貨價，不含幣價重估</span>
        <span><b>運行中</b> 當日仍開著的倉，畫面 USDT，含幣價</span>
        <span><b>歷史累計</b> 基準帶入加每日配對增量，保留關倉成果並調整複投；提領不重複加回</span>
        <span>有紀錄的日子可點。</span>
      </div>
      ${rows.some((r) => String(r.event || "").split("+").includes("review"))
        ? `<p class="warn">有倉位變多、網格利潤也掉了一截，對不上是加倉還是複投。請在列上選一次。</p>`
        : ""}
    </div>
    <div class="stats">
      <div class="stat"><span class="label">點選日</span><strong>${calSelected}</strong></div>
      <div class="stat">${tip("每日利潤", "U 本位：畫面網格利潤的日差。幣本位（ETH）：當天新增的幣×抓取現貨價，不含舊庫存被幣價重估。第一天基準是 0。")}<strong class="${cls(selected.daily_profit && selected.daily_profit.usdt)}">${bothSigned(selected.daily_profit)}</strong></div>
      <div class="stat">${tip("運行中配對利潤", "那天還開著的倉，派網畫面上的網格利潤加總（USDT，幣本位含幣價）。關倉後這筆會掉。")}<strong>${both(selected.cumulative)}</strong></div>
      <div class="stat">${tip("歷史累計配對利潤（含已關倉）", "截至當日快照，含基準帶入、每日配對增量、已關倉成果與複投調整。派網配對值已含已提領收益，不再加回；記錄開始前未保留的複投收益可能未涵蓋。")}<strong>${both(selected.true_profit)}</strong></div>
    </div>
    <div class="card">
      <h3>${calSelected} 明細</h3>
      ${Number(selected.unresolved_count) > 0 ? '<p class="warn">有關倉配對資料待補，單日與歷史合計為暫計，補齊後重算。</p>' : ''}
      <table class="plain"><thead><tr><th>標的</th><th>${tip("事件", "續存＝照常；加倉＝只是本金變多、網格利潤沒被重置；複投＝網格利潤打進倉位後從接近 0 再算。對不上才會出現待確認。")}</th><th>倉位</th><th>單日利潤</th><th>當時網格利潤</th></tr></thead>
      <tbody>${rowHtml}</tbody></table>
    </div>`;
  document.getElementById("calPrev").onclick = () => {
    calMonth.m -= 1;
    if (calMonth.m < 1) { calMonth.m = 12; calMonth.y -= 1; }
    paintCalendar();
  };
  document.getElementById("calNext").onclick = () => {
    calMonth.m += 1;
    if (calMonth.m > 12) { calMonth.m = 1; calMonth.y += 1; }
    paintCalendar();
  };
  app.querySelectorAll("button.cal-cell[data-date]").forEach((btn) => {
    btn.onclick = () => { calSelected = btn.dataset.date; paintCalendar(); };
  });
  app.querySelectorAll("button[data-calline]").forEach((btn) => {
    btn.onclick = () => { calLine = btn.dataset.calline; paintCalendar(); };
  });
  drawCalendarBars(byDate, y, m, dim);
  bindEventDecisions();
  bindHistoryClicks();
}

function drawCalendarBars(byDate, y, m, dim) {
  const el = document.getElementById("calBars");
  if (!el) return;
  const dates = Array.from({ length: dim }, (_, i) => `${y}-${String(m).padStart(2, "0")}-${String(i + 1).padStart(2, "0")}`);
  const recs = dates.map((d) => byDate[d]);
  const daily = recs.map((r) => (r ? moneyNum(r.daily_profit) : null));
  const total = recs.map((r) => (r ? moneyNum(r.true_profit) : null));
  const running = recs.map((r) => (r ? moneyNum(r.cumulative) : null));
  const line = calLine === "running" ? running : total;
  const accent = Charts.token("--accent", "#2e6970");
  const sel = dates.indexOf(calSelected);
  Charts.plot(el, {
    n: dim, labels: dates.map((_, i) => String(i + 1)), height: 170, label: "當月每日利潤",
    stacks: [
      { id: "up", color: accent, values: daily.map((v) => (v != null && v > 0 ? v : null)) },
      { id: "down", color: Charts.token("--down", "#b42318"), values: daily.map((v) => (v != null && v < 0 ? v : null)) },
    ],
    barFlags: recs.map((r) => (r && Number(r.unresolved_count) > 0 ? 2 : 0)),
    overlay: { values: line, color: Charts.token("--ink-2", "#3d4d54"), format: (v) => String(Math.round(v)) },
    yFormat: (v) => String(+v.toFixed(2)),
    selected: sel >= 0 ? sel : null,
    tooltip: (i) => {
      const r = recs[i];
      if (!r) return "";
      return Charts.tipRows(`${dates[i]}${Number(r.unresolved_count) > 0 ? "（待補）" : ""}`, [
        { color: accent, label: "單日利潤", value: signed(daily[i]) + " U", cls: cls(daily[i]) },
        { color: calLine === "total" ? Charts.token("--ink-2", "#3d4d54") : null, label: "歷史累計（含已關倉）", value: total[i] == null ? "—" : n(total[i], 2) + " U" },
        { color: calLine === "running" ? Charts.token("--ink-2", "#3d4d54") : null, label: "運行中配對", value: running[i] == null ? "—" : n(running[i], 2) + " U" },
        { label: "倉位數", value: String(r.active_count ?? "—") },
      ], "點長條切換到那一天");
    },
    onClick: (i) => { if (recs[i]) { calSelected = dates[i]; paintCalendar(); } },
  });
}

export async function renderCalendar() {
  calendarData = await get("/api/v1/days");
  paintCalendar();
}

function bindEventDecisions() {
  app.querySelectorAll("[data-decide]").forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      btn.disabled = true;
      try {
        const result = await postJson("/api/v1/event-decision", {
          capture_date: btn.dataset.date,
          bu_order_id: btn.dataset.id,
          decision: btn.dataset.decide,
        });
        calendarData = await get("/api/v1/days");
        state.dailyData = calendarData;
        paintCalendar();
        showFlash("已記錄判定並重算歷史。" + publishText(result.publish));
      } catch (err) {
        showFlash("判定失敗：" + err.message, "error");
        btn.disabled = false;
      }
    };
  });
}
