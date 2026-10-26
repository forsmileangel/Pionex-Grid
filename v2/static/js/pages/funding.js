import { app, bindHistoryClicks, bothSigned, cls, esc, eventLabel, get, moneyNum, n, signed, tip, usdt } from "../common.js";

import { isCurrent } from "../router.js";

import { effSeg } from "./efficiency.js";

// 資金費（試做）：派網每倉累計資金費，和上一次每日快照相減得到當日資金費；淨利＝當日配對＋資金費。只顯示，不進每日利潤。
let fundingData = null;
let fundRange = "30";
let fundSelected = "";
let fundMonth = null;

const pctText = (v) => (v == null ? "—" : signed(v) + "%");
const FUND_TIP = "資金費＝派網這個倉的累計資金費，和上一次每日記錄相減。付出為負、收到為正。幣本位（ETH）是當天新增的幣×抓取價，不含幣價重估。只顯示，不算進每日利潤，Portfolio 不受影響。";
const NET_TIP = "淨利＝當日配對利潤＋當日資金費。每日利潤（Portfolio 用的那個）仍只有配對，這裡另外算給你看。";

function fundCalendar(days) {
  const byDate = {};
  days.forEach((d) => { byDate[d.date] = d; });
  const parts = fundSelected.split("-").map(Number);
  if (!fundMonth) fundMonth = { y: parts[0], m: parts[1] };
  const { y, m } = fundMonth;
  const prefix = `${y}-${String(m).padStart(2, "0")}-`;
  const startPad = new Date(y, m - 1, 1).getDay();
  const dim = new Date(y, m, 0).getDate();
  let heatMax = 0;
  days.forEach((d) => {
    const v = moneyNum(d.net);
    if (d.date.startsWith(prefix) && v != null && !d.unresolved) heatMax = Math.max(heatMax, Math.abs(v));
  });
  const heat = (d) => {
    const v = moneyNum(d.net);
    if (!heatMax || v == null || v === 0 || d.unresolved) return "";
    const a = (0.05 + 0.3 * Math.min(1, Math.abs(v) / heatMax)).toFixed(3);
    return ` style="--cell-bg:rgba(var(${v > 0 ? "--heat-pos" : "--heat-neg"}),${a})"`;
  };
  const line = (label, value, klass = "", opt = false) =>
    `<span class="cal-amt${opt ? " opt" : ""}"><span>${label}</span><b class="${klass}">${value}</b></span>`;
  let cells = ["日", "一", "二", "三", "四", "五", "六"].map((w) => `<div class="cal-dow">${w}</div>`).join("");
  for (let i = 0; i < startPad; i++) cells += `<div class="cal-cell is-pad"></div>`;
  for (let day = 1; day <= dim; day++) {
    const date = prefix + String(day).padStart(2, "0");
    const d = byDate[date];
    const sel = date === fundSelected ? " is-selected" : "";
    if (!d) { cells += `<div class="cal-cell is-empty${sel}"><span class="cal-num">${day}</span></div>`; continue; }
    const grid = moneyNum(d.grid_profit), fund = moneyNum(d.funding), net = moneyNum(d.net);
    cells += `<button type="button" class="cal-cell${sel}" data-funddate="${date}"${heat(d)}><span class="cal-num">${day}${d.unresolved ? " · 待補" : ""}</span>`
      + line("錢包", moneyNum(d.wallet) == null ? "—" : n(moneyNum(d.wallet), 0), "", true)
      + line("倉位", `${n(moneyNum(d.equivalent), 0)} · ${d.active_count ?? "—"}`, "", true)
      + line("配對/單日", `${n(moneyNum(d.running_grid), 0)} / ${signed(grid)}`, "", true)
      + line("資金費", fund == null ? "—" : signed(fund), cls(fund))
      + line("淨利", net == null ? "—" : signed(net), cls(net) + " net")
      + `</button>`;
  }
  const mon = (fundingData.months || {})[prefix.slice(0, 7)];
  const sum = mon
    ? `本月配對 <b class="${cls(moneyNum(mon.grid_profit))}">${signed(moneyNum(mon.grid_profit))}</b> U · 資金費 <b class="${cls(moneyNum(mon.funding))}">${signed(moneyNum(mon.funding))}</b> U（吃掉配對 ${mon.pct_of_grid == null ? "—" : n(Math.abs(mon.pct_of_grid), 1) + "%"}）· 淨利 <b class="${cls(moneyNum(mon.net))}">${signed(moneyNum(mon.net))}</b> U · 日均淨利 ${mon.days ? signed(moneyNum(mon.net) / mon.days) : "—"} U（${mon.days} 天）`
    : "本月尚無紀錄";
  return `<div class="card fund-cal">
      <div class="cal-head">
        <button type="button" id="fundPrev" aria-label="上個月">‹</button>
        <strong>${y} 年 ${m} 月</strong>
        <button type="button" id="fundNext" aria-label="下個月">›</button>
      </div>
      <p class="cal-sum">${sum}</p>
      <div class="cal">${cells}</div>
      <div class="legend">
        <span><b>錢包</b> 08:15 派網錢包總值（USDT）</span>
        <span><b>倉位</b> 等效倉位（投入＋追加保證金）· 倉數</span>
        <span><b>配對/單日</b> 運行中配對利潤 / 當日配對</span>
        <span><b>資金費</b> 當日資金費，付出為負</span>
        <span><b>淨利</b> 單日配對＋資金費，格子顏色依淨利</span>
      </div>
    </div>`;
}

function paintFunding() {
  const data = fundingData || {};
  const days = data.days || [];
  const byDate = data.rows_by_date || {};
  if (!days.length) {
    app.innerHTML = "<p class='muted'>還沒有每日紀錄。</p>";
    return;
  }
  const latest = days.find((d) => moneyNum(d.funding) != null) || days[0];
  if (!fundSelected || !byDate[fundSelected]) fundSelected = latest.date;
  const month = (data.months || {})[latest.date.slice(0, 7)];
  const since = new Date(Date.parse(latest.date + "T00:00:00Z") - 30 * 86400000).toISOString().slice(0, 10);
  const recent = days.filter((d) => d.date > since && moneyNum(d.funding) != null);
  const sum30 = recent.reduce((a, d) => a + moneyNum(d.funding), 0);
  const span30 = recent.reduce((a, d) => a + (d.span_days || 1), 0);
  const eaten = (pct) => (pct == null ? "—" : n(Math.abs(pct), 1) + "%");
  const stat = (label, note, value, sub) => `<div class="stat">${tip(label, note)}`
    + `<strong class="${cls(moneyNum(value))}">${bothSigned(value)}<span class="twd" style="white-space:normal">${sub}</span></strong></div>`;

  const sel = days.find((d) => d.date === fundSelected) || latest;
  const rows = (byDate[fundSelected] || []).slice().sort((a, b) => (moneyNum(a.funding_day) ?? 0) - (moneyNum(b.funding_day) ?? 0));
  const rowHtml = rows.length ? rows.map((r) => {
    const f = moneyNum(r.funding_day), net = moneyNum(r.net);
    return `<tr class="clickable" data-id="${esc(r.bu_order_id || "")}">
      <td>${esc(r.symbol || "—")}</td>
      <td>${esc(eventLabel(r.event || r.status))}</td>
      <td class="n">${usdt(r.investment)}</td>
      <td class="n ${cls(moneyNum(r.daily_profit))}">${signed(moneyNum(r.daily_profit))}</td>
      <td class="n ${cls(f)}">${signed(f)}</td>
      <td class="n ${cls(net)}"><b>${signed(net)}</b></td>
      <td class="n ${cls(r.pct_of_grid)}">${pctText(r.pct_of_grid)}</td>
      <td class="n ${cls(moneyNum(r.funding_cum))}">${signed(moneyNum(r.funding_cum))}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="8" class="muted">當日無倉位明細</td></tr>`;

  const grids = data.grids || [];
  const gridHtml = grids.map((g) => {
    const closed = g.lifecycle === "closed";
    return `<tr class="clickable" data-id="${esc(g.bu_order_id || "")}">
      <td>${esc(g.symbol || "—")}<small>${esc(String(g.created_at || "").slice(0, 10))} 建倉</small></td>
      <td>${closed ? `已關倉<small>${esc(String(g.closed_at || "").slice(0, 10))}</small>` : "運行中"}</td>
      <td class="n ${cls(moneyNum(g.funding_cum))}">${signed(moneyNum(g.funding_cum))}</td>
      <td class="n ${cls(moneyNum(g.funding_30d))}">${closed && !moneyNum(g.funding_30d) ? "—" : signed(moneyNum(g.funding_30d))}</td>
      <td class="n">${usdt(g.lifetime_grid)}</td>
      <td class="n ${cls(g.pct_of_grid)}">${pctText(g.pct_of_grid)}</td>
    </tr>`;
  }).join("");

  app.innerHTML = `
    <p class="muted">試做頁：配對、資金費、淨利放在一起看，確認好用再決定怎麼放進每日詳情或月曆。${tip("怎麼算", FUND_TIP + " " + NET_TIP)}</p>
    <div class="stats">
      ${stat("最近一天淨利", NET_TIP, latest.net,
        `${esc(latest.date)} · 配對 ${signed(moneyNum(latest.grid_profit))} · 資金費 ${signed(moneyNum(latest.funding))}（吃掉 ${eaten(latest.pct_of_grid)}）`)}
      ${stat("本月淨利", "本月每日配對＋資金費加總。和月曆的月合計同一規則，關倉待補的日子不計。", month ? month.net : null,
        month ? `配對 ${signed(moneyNum(month.grid_profit))} · 資金費 ${signed(moneyNum(month.funding))}（吃掉 ${eaten(month.pct_of_grid)}）` : "")}
      ${stat("近 30 天資金費", "最近 30 天的每日資金費加總與日均；年化＝日均 ÷ 最近一天的等效倉位 × 365。", { usdt: String(sum30) },
        span30 ? `日均 ${signed(sum30 / span30)} U · 年化 ${moneyNum(latest.equivalent) ? pctText(sum30 / span30 * 365 / moneyNum(latest.equivalent) * 100) : "—"}` : "")}
      ${stat("歷史累計資金費", "所有出現過的倉（含已關倉）從建倉起的累計資金費加總，USDT 用最近一次的換算。％＝佔歷史累計配對利潤。", latest.total,
        `佔歷史累計配對 ${eaten(latest.total_pct_of_grid)}`)}
    </div>
    ${fundCalendar(days)}
    <div class="card">
      <div class="card-head"><h3>${tip("每日配對、資金費與淨利", FUND_TIP + " " + NET_TIP + " 點長條可切換到那一天。")}</h3>
        ${effSeg("fundrange", [["30", "近 30 天"], ["90", "近 90 天"], ["all", "全部"]], fundRange)}</div>
      <p class="note" style="margin:-4px 0 6px">藍綠長條＝當日配對；紅長條＝資金費（付出）；深色線＝淨利。同一個 USDT 軸。</p>
      <div class="chart" id="fundBars"></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>${esc(fundSelected)} 每倉配對與資金費</h3>
        <label class="note">日期 <select id="fundDate">${days.filter((d) => byDate[d.date]).map((d) =>
          `<option value="${esc(d.date)}"${d.date === fundSelected ? " selected" : ""}>${esc(d.date)}</option>`).join("")}</select></label></div>
      <p class="note">當天配對 <b class="${cls(moneyNum(sel.grid_profit))}">${signed(moneyNum(sel.grid_profit))}</b> U · 資金費 <b class="${cls(moneyNum(sel.funding))}">${signed(moneyNum(sel.funding))}</b> U · 淨利 <b class="${cls(moneyNum(sel.net))}">${signed(moneyNum(sel.net))}</b> U · 資金費年化 ${pctText(sel.annual_pct)}${sel.span_days > 1 ? ` · 資金費含前 ${sel.span_days - 1} 天沒記到的部分` : ""}${moneyNum(sel.funding) == null ? " · 這天沒有可比較的前一筆資金費" : ""}</p>
      <table class="plain"><thead><tr><th>標的</th><th>事件</th><th>倉位</th><th>當日配對</th>
        <th>${tip("當日資金費", FUND_TIP)}</th><th>${tip("淨利", NET_TIP)}</th>
        <th>${tip("資金費／配對", "當日資金費 ÷ 當日配對利潤；配對為 0 或負時不算。")}</th>
        <th>${tip("累計資金費", "這個倉從建倉起的累計資金費（派網數字）。")}</th></tr></thead>
      <tbody>${rowHtml}</tbody></table>
    </div>
    <div class="card">
      <div class="card-head"><h3>${tip("各倉累計資金費", "每個倉從建倉起的累計資金費，和它的累計配對利潤（含已提領、複投）比較。付最多的在上面。")}</h3></div>
      <table class="plain"><thead><tr><th>標的</th><th>狀態</th><th>累計資金費</th><th>近 30 天</th><th>累計配對</th><th>佔配對</th></tr></thead>
      <tbody>${gridHtml || `<tr><td colspan="6" class="muted">沒有資料</td></tr>`}</tbody></table>
    </div>`;

  drawFundingBars(days);
  app.querySelectorAll("button[data-fundrange]").forEach((btn) => {
    btn.onclick = () => { fundRange = btn.dataset.fundrange; paintFunding(); };
  });
  app.querySelectorAll("button[data-funddate]").forEach((btn) => {
    btn.onclick = () => { fundSelected = btn.dataset.funddate; paintFunding(); };
  });
  const shift = (step) => () => {
    fundMonth.m += step;
    if (fundMonth.m < 1) { fundMonth.m = 12; fundMonth.y -= 1; }
    if (fundMonth.m > 12) { fundMonth.m = 1; fundMonth.y += 1; }
    paintFunding();
  };
  document.getElementById("fundPrev").onclick = shift(-1);
  document.getElementById("fundNext").onclick = shift(1);
  const pick = document.getElementById("fundDate");
  if (pick) pick.onchange = () => selectFundDate(pick.value);
  bindHistoryClicks();
}

function selectFundDate(date) {
  fundSelected = date;
  const [y, m] = date.split("-").map(Number);
  fundMonth = { y, m };
  paintFunding();
}

function drawFundingBars(days) {
  const el = document.getElementById("fundBars");
  if (!el) return;
  const chron = days.slice().reverse();
  const shown = fundRange === "all" ? chron : chron.slice(-Number(fundRange));
  const grid = shown.map((d) => moneyNum(d.grid_profit));
  const fund = shown.map((d) => moneyNum(d.funding));
  const net = shown.map((d) => moneyNum(d.net));
  const accent = Charts.token("--accent", "#2e6970");
  const down = Charts.token("--down", "#b42318");
  const ink = Charts.token("--ink-2", "#3d4d54");
  const sel = shown.findIndex((d) => d.date === fundSelected);
  Charts.plot(el, {
    n: shown.length, labels: shown.map((d) => d.date.slice(5).replace("-", "/")), height: 230, label: "每日配對、資金費與淨利",
    stacks: [
      { id: "grid", color: accent, values: grid },
      { id: "fund", color: down, values: fund },
    ],
    lines: [{ id: "net", color: ink, values: net, strong: true }],
    yFormat: (v) => String(+v.toFixed(2)),
    selected: sel >= 0 ? sel : null,
    empty: "這段期間沒有資料",
    tooltip: (i) => {
      const d = shown[i];
      return Charts.tipRows(d.date + (d.span_days > 1 ? `（資金費含前 ${d.span_days - 1} 天）` : ""), [
        { color: accent, label: "當日配對", value: signed(grid[i]) + " U", cls: cls(grid[i]) },
        { color: down, label: "資金費", value: fund[i] == null ? "—" : signed(fund[i]) + " U", cls: cls(fund[i]) },
        { color: ink, label: "淨利", value: net[i] == null ? "—" : signed(net[i]) + " U", cls: cls(net[i]) },
        { label: "資金費吃掉", value: d.pct_of_grid == null ? "—" : n(Math.abs(d.pct_of_grid), 1) + "%" },
      ], "點長條看那一天");
    },
    onClick: (i) => selectFundDate(shown[i].date),
  });
}

export async function renderFunding(seq) {
  const data = await get("/api/v1/funding");
  if (!isCurrent(seq, "funding")) return;
  fundingData = data;
  paintFunding();
}
