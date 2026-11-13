import { state } from "../state.js";

import { FUND_SIGN_NOTE, app, avgStat, bindHistoryClicks, both, bothSigned, closureCell, closureLine, closureNote, cls, dayRange, esc, eventCell, fundShare, fundWord, get, moneyNum, n, postJson, publishText, showFlash, signed, skippedNote, tip, usdt } from "../common.js";

import { isCurrent } from "../router.js";

// 月曆：每天的配對、資金費、淨利（原本的資金費頁已併進來）。每一格＝當天 08:15 到隔天 08:15 的利潤，
// 由隔天早上 08:15 的那筆每日紀錄記下，所以記在前一天那格（最新一格是昨天，今天那格要等明天 08:15）。
// 格子用 date（利潤日），向伺服器要明細、判定事件一律用 capture_date（那筆紀錄的擷取日）。
// 資料來自 /api/v1/funding?rows=0；每一天的倉位明細點到那天才抓（/api/v1/funding/<擷取日>），null＝讀取中。
let calendarData = null;
const dayRows = new Map();
let calMonth = null;
let calSelected = "";

const FUND_TIP = "資金費＝派網這個倉的累計資金費，和上一次每日紀錄相減。" + FUND_SIGN_NOTE + "派網在台北 00:00、08:00、16:00 結算，所以一格含當天 16:00、隔天 00:00 與 08:00 三次。幣本位是當天新增的幣×抓取價，不含幣價重估。";
const NET_TIP = "淨利＝當日配對＋當日資金費。Portfolio 和 Excel 的每日利潤仍只有配對。不含關倉賠掉的本金（看關倉損益）。";
const MONTH_TIP = "所選月份的每日淨利（配對＋資金費）加總 ÷ 天數。一格＝當天 08:15 到隔天 08:15 的利潤，記在當天那一格（每月 1 號早上的那筆算上個月）；漏抓的日子會併進下一筆，天數照實際算；資金費沒記到的那天，會併進下一天。帳本第一天（基準日）和關倉配對待補的日子不算。切換月份會跟著變。";
const RECENT_TIP = "最新一筆每日紀錄往回 30 天（含當天）的每日淨利加總 ÷ 天數，規則和當月效率一樣。固定看最近 30 天，不跟著月份切換。";

// 目前點選的那一格對應的擷取日（沒有就空字串）。
function selectedCapture() {
  const d = ((calendarData || {}).days || []).find((x) => x.date === calSelected);
  return d ? d.capture_date : "";
}

async function loadDayRows(capture) {
  if (!capture || dayRows.has(capture)) return;
  dayRows.set(capture, null);
  try {
    const data = await get("/api/v1/funding/" + encodeURIComponent(capture));
    dayRows.set(capture, data.rows || []);
  } catch (err) {
    dayRows.delete(capture);
    if (state.page === "calendar") showFlash(`讀取 ${calSelected || capture} 明細失敗：${err.message}`, "error");
    return;
  }
  if (state.page === "calendar" && selectedCapture() === capture) paintCalendar();
}

const pctText = (v) => (v == null ? "—" : signed(v) + "%");

// 平均卡：大字是淨利日均，小字拆成配對和資金費（v2/averages.py）。
function calAverages(mon, recent) {
  const parts = (b) => `${dayRange(b)} · ${b.days} 天 · 配對 ${signed(moneyNum(b.grid_profit.avg))} · 資金費 ${signed(moneyNum(b.funding.avg))}${skippedNote(b)}`;
  return `<div class="stats cal-avg">`
    + avgStat("當月效率", MONTH_TIP, mon && mon.net.avg, mon && mon.days ? parts(mon) : `這個月還沒有完整紀錄${skippedNote(mon)}`)
    + avgStat("近 30 天日均", RECENT_TIP, recent && recent.net.avg, recent && recent.days ? parts(recent) : "還沒有完整紀錄")
    + `</div>`;
}

function paintCalendar() {
  const data = calendarData || {};
  const days = data.days || [];
  if (!days.length) {
    app.innerHTML = "<p class='muted'>還沒有每日紀錄。請先執行 python -m v2.capture</p>";
    return;
  }
  const byDate = {};
  days.forEach((d) => { byDate[d.date] = d; });
  const latest = days.find((d) => moneyNum(d.funding) != null) || days[0];
  if (!calSelected || !byDate[calSelected]) calSelected = days[0].date;
  if (!calMonth) {
    const [y0, m0] = calSelected.split("-").map(Number);
    calMonth = { y: y0, m: m0 };
  }
  const { y, m } = calMonth;
  const prefix = `${y}-${String(m).padStart(2, "0")}-`;
  const dim = new Date(y, m, 0).getDate();
  const startPad = new Date(y, m - 1, 1).getDay();
  const averages = data.averages || {};
  const mon = (averages.months || {})[prefix.slice(0, 7)];
  const recent = averages.recent || {};
  const closures = data.closures || {};
  const closedOn = closures.days || {};

  // 格子深淺依當月淨利的最大絕對值；有關倉待補的日子不上色。
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
  const line = (label, value, klass = "") => `<span class="cal-amt"><span>${label}</span><b class="${klass}">${value}</b></span>`;
  // 手機只顯示淨利：小數依大小省略，免得格子擠。
  const compact = (v) => (v == null ? "—" : signed(v, Math.abs(v) >= 100 ? 0 : 1));
  let cells = ["日", "一", "二", "三", "四", "五", "六"].map((w) => `<div class="cal-dow">${w}</div>`).join("");
  for (let i = 0; i < startPad; i++) cells += `<div class="cal-cell is-pad"></div>`;
  for (let day = 1; day <= dim; day++) {
    const date = prefix + String(day).padStart(2, "0");
    const d = byDate[date];
    const sel = date === calSelected ? " is-selected" : "";
    if (!d) { cells += `<div class="cal-cell is-empty${sel}"><span class="cal-num">${day}</span></div>`; continue; }
    const grid = moneyNum(d.grid_profit), fund = moneyNum(d.funding), net = moneyNum(d.net);
    const closed = closedOn[date];
    cells += `<button type="button" class="cal-cell${sel}" data-date="${date}"${heat(d)}>`
      + `<span class="cal-num">${day}${d.unresolved ? " · 待補" : ""}${closed ? `<i class="cal-flag" title="這天有關倉">關</i>` : ""}</span>`
      + line("配對", grid == null ? "—" : signed(grid), cls(grid))
      + line("資金費", fund == null ? "—" : signed(fund), cls(fund))
      + line("淨利", net == null ? "—" : signed(net), cls(net) + " net")
      + (closed ? (([t, c]) => line("關倉", t, c))(closureCell(closed)) : "")
      + `<b class="cal-mobile ${cls(net ?? grid)}">${compact(net ?? grid)}</b>`
      + `</button>`;
  }

  const total = (key) => (mon && mon.days ? moneyNum(mon[key].total) : null);
  const monthSum = mon && mon.days
    ? `本月配對 <b class="${cls(total("grid_profit"))}">${signed(total("grid_profit"))}</b> U · 資金費 <b class="${cls(total("funding"))}">${signed(total("funding"))}</b> U（${fundShare(mon.pct_of_grid)}）· 淨利 <b class="${cls(total("net"))}">${signed(total("net"))}</b> U`
    : "本月尚無紀錄";

  const sel = byDate[calSelected] || {};
  const selCapture = sel.capture_date || "";
  const loaded = dayRows.get(selCapture);
  if (loaded === undefined) loadDayRows(selCapture);
  const rows = (loaded || []).slice().sort((a, b) => (moneyNum(a.funding_day) ?? 0) - (moneyNum(b.funding_day) ?? 0));
  const rowHtml = rows.length ? rows.map((r) => {
    const grid = moneyNum(r.daily_profit), f = moneyNum(r.funding_day), net = moneyNum(r.net);
    return `<tr class="clickable" data-id="${esc(r.bu_order_id || "")}">
      <td>${esc(r.symbol || "—")}</td>
      <td>${eventCell(r, selCapture, calSelected)}</td>
      <td class="n">${usdt(r.investment || r.size)}</td>
      <td class="n ${cls(grid)}">${signed(grid)}</td>
      <td class="n ${cls(f)}">${signed(f)}</td>
      <td class="n ${cls(net)}"><b>${signed(net)}</b></td>
      <td class="n">${pctText(r.pct_of_grid)}</td>
      <td class="n ${cls(moneyNum(r.funding_cum))}">${signed(moneyNum(r.funding_cum))}</td>
      <td class="n">${usdt(r.grid_profit)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="9" class="muted">${loaded ? "當日無倉位明細" : "載入中…"}</td></tr>`;

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

  const fund30 = recent.days ? moneyNum(recent.funding.avg) : null;
  const monthNow = (averages.months || {})[latest.date.slice(0, 7)];
  const monthNowLabel = `${Number(latest.date.slice(5, 7))} 月淨利`;
  const stat = (label, note, value, sub) => `<div class="stat">${tip(label, note)}`
    + `<strong class="${cls(moneyNum(value))}">${bothSigned(value)}<span class="twd" style="white-space:normal">${sub}</span></strong></div>`;

  app.innerHTML = `
    <div class="stats">
      ${stat("最近一天淨利", NET_TIP, latest.net,
        `${esc(latest.date)} · 配對 ${signed(moneyNum(latest.grid_profit))} · 資金費 ${signed(moneyNum(latest.funding))}（${fundShare(latest.pct_of_grid, true)}）`)}
      ${stat(monthNowLabel, "最近一天所在月份的每日配對＋資金費加總（每月 1 號早上的那筆算上個月）。基準日和關倉待補的日子不計。", monthNow && monthNow.days ? monthNow.net.total : null,
        monthNow && monthNow.days ? `配對 ${signed(moneyNum(monthNow.grid_profit.total))} · 資金費 ${signed(moneyNum(monthNow.funding.total))}（${fundShare(monthNow.pct_of_grid, true)}）` : "")}
      ${stat("近 30 天資金費", "最近 30 天的每日資金費加總與日均；年化＝日均 ÷ 最近一天的等效倉位 × 365。", recent.days ? recent.funding.total : null,
        fund30 != null ? `日均 ${signed(fund30)} U · 年化 ${moneyNum(latest.equivalent) ? pctText(fund30 * 365 / moneyNum(latest.equivalent) * 100) : "—"}` : "")}
      ${stat("歷史累計資金費", "所有出現過的倉（含已關倉）從建倉起的累計資金費加總，USDT 用最近一次的換算。％＝佔歷史累計配對利潤。", latest.total,
        `${fundShare(latest.total_pct_of_grid)}（歷史累計）`)}
    </div>
    <div class="card">
      <div class="cal-head">
        <button type="button" id="calPrev" aria-label="上個月">‹</button>
        <strong>${y} 年 ${m} 月</strong>
        <button type="button" id="calNext" aria-label="下個月">›</button>
      </div>
      ${calAverages(mon, recent)}
      <p class="cal-sum">${monthSum}</p>
      ${closureLine((closures.months || {})[prefix.slice(0, 7)], closures.recent)}
      <div class="card-head" style="margin-top:14px"><h3>${tip("每日配對、資金費與淨利", FUND_TIP + " " + NET_TIP + " 點長條可切換到那一天。")}</h3></div>
      <p class="note" style="margin:-4px 0 6px">藍綠長條＝當日配對；紅長條＝資金費（付出）；深色線＝淨利。同一個 USDT 軸。</p>
      <div class="chart" id="calBars"></div>
      <div class="cal">${cells}</div>
      <div class="legend">
        <span><b>配對</b> 當日網格利潤，基準日為 0；幣本位是當天新增的幣×抓取現貨價</span>
        <span><b>資金費</b> 當日資金費，付出為負</span>
        <span><b>淨利</b> 配對＋資金費，格子顏色依淨利；手機只顯示淨利</span>
        <span><b>關倉</b> 那天記到的關倉淨損益</span>
        <span>每格是當天 08:15 到隔天 08:15 的利潤，隔天早上記錄，所以最新一格是昨天。</span>
        <span>有紀錄的日子可點。</span>
      </div>
    </div>
    <div class="stats">
      <div class="stat"><span class="label">點選日</span><strong>${esc(calSelected)}</strong></div>
      <div class="stat">${tip("淨利", NET_TIP)}<strong class="${cls(moneyNum(sel.net))}">${bothSigned(sel.net)}<span class="twd">配對 ${signed(moneyNum(sel.grid_profit))} · 資金費 ${signed(moneyNum(sel.funding))}</span></strong></div>
      <div class="stat">${tip("運行中配對利潤", "那天還開著的倉，派網畫面上的網格利潤加總（USDT，幣本位含幣價）。關倉後這筆會掉。")}<strong>${both(sel.running_grid)}</strong></div>
      <div class="stat">${tip("歷史累計配對利潤（含已關倉）", "截至當日快照，含基準帶入、每日配對增量、已關倉成果與複投調整。派網配對值已含已提領收益，不再加回。")}<strong>${both(sel.true_profit)}</strong></div>
    </div>
    <div class="card">
      <h3>${esc(calSelected)} 每倉配對與資金費</h3>
      <p class="note">資金費年化 ${pctText(sel.annual_pct)}${sel.span_days > 1 ? ` · 資金費含前 ${sel.span_days - 1} 天沒記到的部分` : ""}${moneyNum(sel.funding) == null ? " · 這天沒有可比較的前一筆資金費" : ""}</p>
      ${sel.unresolved ? '<p class="warn">有關倉配對資料待補，單日與歷史合計為暫計，補齊後重算。</p>' : ""}
      ${closureNote(closedOn[calSelected])}
      ${rows.some((r) => String(r.event || "").split("+").includes("review"))
        ? `<p class="warn">有倉位變多、網格利潤也掉了一截，對不上是加倉還是複投。請在列上選一次。</p>` : ""}
      <div class="range-table"><table class="plain"><thead><tr><th>標的</th><th>${tip("事件", "續存＝照常；加倉＝只是本金變多、網格利潤沒被重置；複投＝網格利潤打進倉位後從接近 0 再算；調槓桿＝槓桿改了、投資額跟著反向變，投入×槓桿不變（只是標籤，金額照算）。對不上才會出現待確認。")}</th><th>倉位</th><th>當日配對</th>
        <th>${tip("當日資金費", FUND_TIP)}</th><th>${tip("淨利", NET_TIP)}</th>
        <th>${tip("資金費／配對", "當日資金費 ÷ 當日配對利潤；正＝收到（加成）、負＝付出（吃掉）；配對為 0 或負時不算。")}</th>
        <th>${tip("累計資金費", "這個倉從建倉起的累計資金費（派網數字）。")}</th><th>當時網格利潤</th></tr></thead>
      <tbody>${rowHtml}</tbody></table></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>${tip("各倉累計資金費", "每個倉從建倉起的累計資金費，和它的累計配對利潤（含已提領、複投）比較。付最多的在上面。")}</h3></div>
      <div class="range-table"><table class="plain"><thead><tr><th>標的</th><th>狀態</th><th>累計資金費</th><th>近 30 天</th><th>累計配對</th><th>佔配對</th></tr></thead>
      <tbody>${gridHtml || `<tr><td colspan="6" class="muted">沒有資料</td></tr>`}</tbody></table></div>
    </div>`;

  const shift = (step) => () => {
    calMonth.m += step;
    if (calMonth.m < 1) { calMonth.m = 12; calMonth.y -= 1; }
    if (calMonth.m > 12) { calMonth.m = 1; calMonth.y += 1; }
    paintCalendar();
  };
  document.getElementById("calPrev").onclick = shift(-1);
  document.getElementById("calNext").onclick = shift(1);
  app.querySelectorAll("button.cal-cell[data-date]").forEach((btn) => {
    btn.onclick = () => { calSelected = btn.dataset.date; paintCalendar(); };
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
  const grid = recs.map((r) => (r ? moneyNum(r.grid_profit) : null));
  const fund = recs.map((r) => (r ? moneyNum(r.funding) : null));
  const net = recs.map((r) => (r ? moneyNum(r.net) : null));
  const accent = Charts.token("--accent", "#2e6970");
  const down = Charts.token("--down", "#b42318");
  const ink = Charts.token("--ink-2", "#3d4d54");
  const sel = dates.indexOf(calSelected);
  Charts.plot(el, {
    n: dim, labels: dates.map((_, i) => String(i + 1)), height: 200, label: "當月每日配對、資金費與淨利",
    stacks: [
      { id: "grid", color: accent, values: grid },
      { id: "fund", color: down, values: fund },
    ],
    lines: [{ id: "net", color: ink, values: net, strong: true }],
    barFlags: recs.map((r) => (r && r.unresolved ? 2 : 0)),
    yFormat: (v) => String(+v.toFixed(2)),
    selected: sel >= 0 ? sel : null,
    tooltip: (i) => {
      const r = recs[i];
      if (!r) return "";
      return Charts.tipRows(`${dates[i]}${r.unresolved ? "（待補）" : ""}${r.span_days > 1 ? `（資金費含前 ${r.span_days - 1} 天）` : ""}`, [
        { color: accent, label: "當日配對", value: signed(grid[i]) + " U", cls: cls(grid[i]) },
        { color: down, label: "資金費", value: fund[i] == null ? "—" : signed(fund[i]) + " U", cls: cls(fund[i]) },
        { color: ink, label: "淨利", value: net[i] == null ? "—" : signed(net[i]) + " U", cls: cls(net[i]) },
        { label: "資金費" + fundWord(r.pct_of_grid), value: r.pct_of_grid == null ? "—" : n(Math.abs(r.pct_of_grid), 1) + "%" },
        { label: "倉位數", value: String(r.active_count ?? "—") },
      ], "點長條切換到那一天");
    },
    onClick: (i) => { if (recs[i]) { calSelected = dates[i]; paintCalendar(); } },
  });
}

async function fetchCalendar() {
  return get("/api/v1/funding?rows=0");
}

export async function renderCalendar(seq) {
  const data = await fetchCalendar();
  if (!isCurrent(seq, "calendar")) return;
  calendarData = data;
  dayRows.clear();
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
          date: btn.dataset.profitDate,
          bu_order_id: btn.dataset.id,
          decision: btn.dataset.decide,
        });
        calendarData = await fetchCalendar();
        dayRows.clear();
        state.dailyData = null;
        if (state.page === "calendar") paintCalendar();
        showFlash("已記錄判定並重算歷史。" + publishText(result.publish));
      } catch (err) {
        showFlash("判定失敗：" + err.message, "error");
        if (state.page === "calendar") btn.disabled = false;
      }
    };
  });
}
