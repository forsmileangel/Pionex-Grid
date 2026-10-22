import { state } from "../state.js";

import { app, bindHistoryClicks, bindSortHeaders, cls, esc, get, positionTable, signed, usdt } from "../common.js";

import { isCurrent } from "../router.js";

let dailySort = { key: "", dir: "desc" };
let dailyOpen = null;

function paintDaily() {
  const days = (state.dailyData && state.dailyData.days) || [];
  if (!days.length) {
    app.innerHTML = "<p class='muted'>還沒有每日紀錄。請先執行 python -m v2.capture</p>";
    return;
  }
  app.innerHTML = `<p class="muted">每一天會往下新增一列。點日期可收合／展開當天每一筆倉位。點欄位標題可排序（各天共用同一排序）。</p>` + days.map((d, i) => {
    const open = dailyOpen == null ? i === 0 : dailyOpen.has(d.capture_date);
    const dayV = d.daily_profit && d.daily_profit.usdt;
    const date = esc(d.capture_date);
    return `
    <section class="day" data-date="${date}">
      <h3><span>${date}</span><span class="meta">${d.active_count} 筆 · 當日 <b class="${cls(dayV)}">${signed(dayV)}</b> USDT · 運行中配對 ${usdt(d.cumulative)} USDT</span></h3>
      <div class="body" ${open ? "" : "hidden"}>${positionTable(d.rows || [], "daily", dailySort)}</div>
    </section>`;
  }).join("");
  app.querySelectorAll(".day h3").forEach((h) => {
    h.onclick = () => {
      const section = h.parentElement;
      const body = section.querySelector(".body");
      body.hidden = !body.hidden;
      if (dailyOpen == null) {
        dailyOpen = new Set();
        days.forEach((d, i) => { if (i === 0) dailyOpen.add(d.capture_date); });
      }
      const date = section.dataset.date;
      if (body.hidden) dailyOpen.delete(date);
      else dailyOpen.add(date);
    };
  });
  bindSortHeaders(dailySort, paintDaily);
  bindHistoryClicks();
}

export async function renderDaily(seq) {
  const data = await get("/api/v1/days");
  if (!isCurrent(seq, "daily")) return;
  state.dailyData = data;
  paintDaily();
}
