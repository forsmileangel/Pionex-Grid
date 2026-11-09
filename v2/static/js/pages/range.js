import { state } from "../state.js";

import { FUND_SIGN_NOTE, app, cls, esc, fundShare, fundWord, moneyNum, n, showFlash, signed, tip, twd } from "../common.js";

import { isCurrent } from "../router.js";

// 區間統計：自選時段的每倉配對、資金費、淨利（v2/range_stats.py）。配對用每 30 分鐘的紀錄；
// 資金費依 00:00／08:00／16:00 的結算歸屬，1.0.4 之前只有每天 08:15 的紀錄，所以舊時段的邊界要落在 08:00 才算得出來。
let rangeData = null;
let rangeFrom = "";
let rangeTo = "";
const excluded = new Set();

const RANGE_TIP = "配對＝區間內盤中紀錄的配對增量（已扣掉加倉、複投的跳動）；區間內新開的倉算全部，關倉的補到結算為止。資金費＝區間內各次結算（台北 00:00、08:00、16:00）的合計，剛好在結束時間結算的算進來、剛好在開始時間的不算。淨利＝配對＋資金費，不含關倉賠掉的本金。" + FUND_SIGN_NOTE;
const FUND_NOTE = "1.0.4 起每 30 分鐘都會記資金費，任何時段都算得出來；在那之前只有每天 08:15 的紀錄，開始和結束要落在 08:00 才算得出資金費（配對不受影響）。";

const pad = (v) => String(v).padStart(2, "0");
const local = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;

// 最近一次結算（00/08/16 點）；快捷按鈕用它往回推。
function lastSettle(now = new Date()) {
  const d = new Date(now);
  d.setMinutes(0, 0, 0);
  while (d.getHours() % 8 !== 0 || d > now) d.setHours(d.getHours() - 1);
  return d;
}

function presets() {
  const now = new Date();
  const s = lastSettle(now);
  const hours = (d, h) => new Date(d.getTime() + h * 3600e3);
  const at8 = new Date(); at8.setHours(8, 0, 0, 0);
  if (at8 > new Date()) at8.setDate(at8.getDate() - 1);
  const night = new Date(at8); night.setDate(night.getDate() - 1); night.setHours(22, 30, 0, 0);
  return [
    ["最近 24 小時（到現在）", hours(now, -24), now],
    ["最近一個完整日（08:00–08:00）", hours(at8, -24), at8],
    ["昨晚 22:30–08:00", night, at8],
    ["最近一個結算段（8 小時）", hours(s, -8), s],
    ["前一個結算段", hours(s, -16), hours(s, -8)],
    ["今天 08:00 到現在", at8, new Date()],
  ];
}

async function load() {
  const qs = new URLSearchParams({ from: rangeFrom, to: rangeTo, exclude: [...excluded].join(",") });
  const res = await fetch("/api/v1/range?" + qs, { signal: AbortSignal.timeout(30000) });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || "讀取失敗 " + res.status);
  return body;
}

const money = (m) => (m && m.usdt != null ? signed(moneyNum(m)) : "—");
// 資金費 ÷ 配對，帶正負號（正＝收到）；配對為 0 或負時不算。
const pctNum = (grid, fund) => {
  const g = moneyNum(grid), f = moneyNum(fund);
  return g == null || f == null || g <= 0 ? null : f / g * 100;
};
const pctText = (v) => (v == null ? "—" : signed(v, 1) + "%");

function paintRange() {
  const d = rangeData;
  const t = d && d.totals;
  const rows = (d && d.rows) || [];
  const presetHtml = presets().map(([label, a, b], i) => `<button type="button" data-preset="${i}">${esc(label)}</button>`).join("");
  const rowHtml = rows.map((r) => `<tr class="${r.excluded ? "muted" : ""}">
      <td><label class="check"><input type="checkbox" data-keep="${esc(r.id)}"${r.excluded ? "" : " checked"}> ${esc(r.symbol || "—")}</label></td>
      <td>${esc(r.leverage || "")}</td>
      <td>${esc(r.status)}</td>
      <td class="n ${cls(moneyNum(r.grid))}">${money(r.grid)}</td>
      <td class="n ${cls(moneyNum(r.funding))}">${r.funding_missing ? `<span class="warn" title="${esc(r.funding_missing)}">缺資料</span>` : money(r.funding)}</td>
      <td class="n ${cls(moneyNum(r.net))}"><b>${money(r.net)}</b></td>
      <td class="n ${cls(pctNum(r.grid, r.funding))}">${pctText(pctNum(r.grid, r.funding))}</td>
      <td class="note">${esc(r.notes.join("；"))}</td>
    </tr>`).join("");
  const x = d && d.excluded_totals;
  app.innerHTML = `
    <p class="muted">選一段時間，看每個倉位的配對、資金費、淨利。${tip("怎麼算", RANGE_TIP + " " + FUND_NOTE)}</p>
    <div class="card">
      <div class="range-pick">
        <label class="field">開始<input type="datetime-local" id="rangeFrom" step="1800" value="${esc(rangeFrom)}"></label>
        <label class="field">結束<input type="datetime-local" id="rangeTo" step="1800" value="${esc(rangeTo)}"></label>
        <button type="button" class="btn" id="rangeGo">計算</button>
      </div>
      <div class="seg range-presets" role="group">${presetHtml}</div>
      ${d ? `<p class="note">區間 ${esc(d.from.slice(0, 16).replace("T", " "))} → ${esc(d.to.slice(0, 16).replace("T", " "))}；配對用 ${esc(d.grid_from.slice(5, 16).replace("T", " "))} → ${esc(d.grid_to.slice(5, 16).replace("T", " "))} 的盤中紀錄（從 ${esc(d.first_point.slice(0, 10))} 開始）。</p>` : ""}
    </div>
    ${t ? `<div class="stats">
      <div class="stat">${tip("配對", RANGE_TIP)}<strong class="${cls(moneyNum(t.grid))}">${money(t.grid)}<span class="unit">USDT</span><span class="twd">${twd(t.grid)} · ${t.count} 倉</span></strong></div>
      <div class="stat">${tip("資金費", FUND_NOTE)}<strong class="${cls(moneyNum(t.funding))}">${money(t.funding)}<span class="unit">USDT</span><span class="twd">${t.funding_missing ? `${t.funding_missing} 倉缺資料，未計` : twd(t.funding)}${t.funding_pending ? ` · ${t.funding_pending} 倉最新一次結算還沒入帳，暫不計` : ""}</span></strong></div>
      <div class="stat">${tip("淨利", "配對＋資金費。不含關倉賠掉的本金（看關倉紀錄）。")}<strong class="${cls(moneyNum(t.net))}">${money(t.net)}<span class="unit">USDT</span><span class="twd">${twd(t.net)}</span></strong></div>
      <div class="stat">${tip("資金費" + fundWord(t.pct_of_grid) + "配對", "資金費 ÷ 配對。" + FUND_SIGN_NOTE)}<strong class="${cls(t.pct_of_grid)}">${t.pct_of_grid == null ? "—" : n(Math.abs(t.pct_of_grid), 1) + "%"}</strong></div>
    </div>` : ""}
    ${d ? `<div class="card">
      <div class="card-head"><h3>每倉明細</h3>
        <div class="seg" role="group"><button type="button" id="rangeDropClosed">排除區間內關倉的倉</button><button type="button" id="rangeAll">全部算進來</button><button type="button" id="rangeCopy">複製表格</button></div></div>
      <p class="note">取消勾選＝不算進合計。${x && x.count ? `已排除 ${x.count} 倉：配對 ${money(x.grid)} · 資金費 ${money(x.funding)} U` : ""}</p>
      <div class="range-table"><table class="plain"><thead><tr><th>標的</th><th>槓桿</th><th>狀態</th><th>配對</th><th>資金費</th><th>淨利</th><th>${tip("資金費／配對", "正＝收到（加成）、負＝付出（吃掉）")}</th><th>備註</th></tr></thead>
      <tbody>${rowHtml || `<tr><td colspan="8" class="muted">這段時間沒有倉位紀錄</td></tr>`}</tbody></table></div>
    </div>` : ""}`;

  const run = async () => {
    rangeFrom = document.getElementById("rangeFrom").value;
    rangeTo = document.getElementById("rangeTo").value;
    try {
      rangeData = await load();
    } catch (err) {
      showFlash(err.message, "error");
      return;
    }
    if (state.page === "range") paintRange();
  };
  document.getElementById("rangeGo").onclick = run;
  app.querySelectorAll("button[data-preset]").forEach((btn) => {
    btn.onclick = () => {
      const [, a, b] = presets()[Number(btn.dataset.preset)];
      document.getElementById("rangeFrom").value = local(a);
      document.getElementById("rangeTo").value = local(b);
      run();
    };
  });
  app.querySelectorAll("input[data-keep]").forEach((box) => {
    box.onchange = () => {
      if (box.checked) excluded.delete(box.dataset.keep); else excluded.add(box.dataset.keep);
      run();
    };
  });
  const dropClosed = document.getElementById("rangeDropClosed");
  if (dropClosed) dropClosed.onclick = () => {
    rows.filter((r) => r.status !== "運行中" && r.status !== "新開").forEach((r) => excluded.add(r.id));
    run();
  };
  const all = document.getElementById("rangeAll");
  if (all) all.onclick = () => { excluded.clear(); run(); };
  const copy = document.getElementById("rangeCopy");
  if (copy) copy.onclick = async () => {
    const lines = [["標的", "槓桿", "狀態", "配對", "資金費", "淨利", "排除"].join("\t")]
      .concat(rows.map((r) => [r.symbol, r.leverage, r.status, moneyNum(r.grid), r.funding_missing ? "" : moneyNum(r.funding),
        r.funding_missing ? "" : moneyNum(r.net), r.excluded ? "排除" : ""].join("\t")));
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      showFlash("已複製，可以貼到 Excel。");
    } catch (_) {
      showFlash("瀏覽器不允許複製。", "error");
    }
  };
}

export async function renderRange(seq) {
  if (!rangeFrom || !rangeTo) {
    const [, a, b] = presets()[0];
    rangeFrom = local(a);
    rangeTo = local(b);
  }
  try {
    rangeData = await load();
  } catch (err) {
    rangeData = null;
    showFlash(err.message, "error");
  }
  if (!isCurrent(seq, "range")) return;
  paintRange();
}
