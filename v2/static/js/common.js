import { state } from "./state.js";

export const app = document.getElementById("app");

export function fmtTaipei(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    const s = String(iso).replace("T", " ").slice(0, 19);
    return s ? s + " +08:00" : "";
  }
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const g = (t) => (parts.find((p) => p.type === t) || {}).value || "";
  return `${g("year")}-${g("month")}-${g("day")} ${g("hour")}:${g("minute")}:${g("second")} +08:00`;
}
export const n = (v, d=2) => v == null || v === "" || Number.isNaN(Number(v)) ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits:d, maximumFractionDigits:d });
export const usdt = (m) => m && m.usdt != null ? n(m.usdt, 2) : "—";
export const twd = (m) => m && m.twd != null ? "約 " + n(m.twd, 0) + " TWD" : "";
export const both = (m) => {
  if (!m || m.usdt == null) return "—";
  const extra = twd(m);
  return n(m.usdt, 2) + `<span class="unit">USDT</span>` + (extra ? `<span class="twd">${extra}</span>` : "");
};
export const bothSigned = (m) => {
  if (!m || m.usdt == null) return "—";
  const extra = twd(m);
  return signed(m.usdt) + `<span class="unit">USDT</span>` + (extra ? `<span class="twd">${extra}</span>` : "");
};
export const cls = (v) => { const x = Number(v); if (!Number.isFinite(x) || x === 0) return ""; return x > 0 ? "up" : "down"; };
export function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
export const tip = (label, text) => `<span class="hint" tabindex="0" data-tip="${esc(text)}">${label}</span>`;
export const cell = (main, sub) => `<span class="v">${main}</span><span class="sub">${sub || ""}</span>`;

let tipHost = null;

function hideTip() {
  const pop = document.getElementById("hint-pop");
  if (pop) pop.remove();
  tipHost = null;
}

function showTip(el) {
  const text = el.getAttribute("data-tip") || "";
  if (!text) return;
  hideTip();
  tipHost = el;
  const box = document.createElement("div");
  box.id = "hint-pop";
  box.className = "hint-pop";
  box.textContent = text;
  document.body.appendChild(box);
  const r = el.getBoundingClientRect();
  const left = Math.min(r.left + window.scrollX, window.scrollX + window.innerWidth - 320);
  box.style.left = Math.max(8, left) + "px";
  box.style.top = (r.bottom + window.scrollY + 8) + "px";
}

let tipsBound = false;

export function initTips() {
  if (tipsBound) return;
  tipsBound = true;
  document.addEventListener("mouseover", (e) => {
    const host = e.target.closest("[data-tip]");
    if (host) { if (host !== tipHost) showTip(host); return; }
    if (tipHost) hideTip();
  });
  document.addEventListener("focusin", (e) => {
    const host = e.target.closest("[data-tip]");
    if (host) showTip(host);
    else if (tipHost) hideTip();
  });
  window.addEventListener("scroll", () => { if (tipHost) hideTip(); }, true);
}

const flashBox = document.getElementById("flash");

export function showFlash(text, kind = "info") {
  flashBox.innerHTML = `<div class="notice${kind === "error" ? " is-error" : ""}"><span>${esc(text)}</span>`
    + `<button type="button" aria-label="關閉提示">×</button></div>`;
  flashBox.querySelector("button").onclick = clearFlash;
}

export function clearFlash() { flashBox.innerHTML = ""; }

export async function get(url) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(30000) });
    if (!res.ok) throw new Error(url + " " + res.status);
    // await here so a timeout while reading the body is also caught below.
    return await res.json();
  } catch (err) {
    if (err && err.name === "TimeoutError") throw new Error("連線逾時（30 秒），請稍後再試");
    throw err;
  }
}

export function signed(v, digits=2) {
  if (v == null || v === "") return "—";
  const x = Number(v);
  if (!Number.isFinite(x)) return "—";
  const t = n(Math.abs(x), digits);
  return (x > 0 ? "+" : x < 0 ? "-" : "") + t;
}

export function ageText(iso) {
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return null;
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  const text = mins < 1 ? "剛剛" : mins < 60 ? `${mins} 分鐘前` : mins < 1440 ? `${Math.floor(mins / 60)} 小時 ${mins % 60} 分前` : `${Math.floor(mins / 1440)} 天前`;
  return { text, stale: mins > 35 };
}

export function publishText(pub) {
  if (!pub) return "";
  if (pub.ok) return "已同步到 Portfolio。";
  if (pub.skipped) return "未設定 Gist，Portfolio 沒有同步。";
  return "Portfolio 同步失敗（" + (pub.error || "未知錯誤") + "），排程會自動重試，也可在執行狀態頁重新發布。";
}

export function moneyNum(m) {
  if (m == null || m.usdt == null || m.usdt === "") return null;
  const x = Number(m.usdt);
  return Number.isFinite(x) ? x : null;
}

function efficiency3x(profit, investment, leverage) {
  const multiple = parseFloat(leverage);
  if (!Number.isFinite(profit) || !Number.isFinite(investment) || investment <= 0
      || !Number.isFinite(multiple) || multiple <= 0) return null;
  return profit / investment * 100 * 3 / multiple;
}

function sortValue(r, key, profitKey) {
  const day = profitKey === "daily" ? r.daily_profit : r.profit_24h;
  const dayV = moneyNum(day);
  const sizeV = moneyNum(r.investment) != null ? moneyNum(r.investment) : moneyNum(r.size);
  const pct = efficiency3x(dayV, sizeV, r.leverage);
  if (key === "opened_at") return r.opened_at || "";
  if (key === "size") return sizeV;
  if (key === "symbol") return (r.symbol || "").toLowerCase();
  if (key === "mark") return Number(r.mark_price);
  if (key === "liq") return Number(r.liq_price);
  if (key === "dist") return Number(r.liq_distance_pct);
  if (key === "grid") return moneyNum(r.grid_profit);
  if (key === "day") return dayV;
  if (key === "pct") return pct;
  return null;
}

function missingSort(v) {
  return v == null || v === "" || (typeof v === "number" && !Number.isFinite(v));
}

function sortedRows(rows, sort, profitKey) {
  if (!sort || !sort.key) return rows.slice();
  const dir = sort.dir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const av = sortValue(a, sort.key, profitKey);
    const bv = sortValue(b, sort.key, profitKey);
    if (missingSort(av) && missingSort(bv)) return 0;
    if (missingSort(av)) return 1;
    if (missingSort(bv)) return -1;
    if (typeof av === "string" && typeof bv === "string") return av.localeCompare(bv, "zh-Hant") * dir;
    if (av < bv) return -dir;
    if (av > bv) return dir;
    return 0;
  });
}

function sortClass(key, sort) {
  if (!sort || sort.key !== key) return "sortable";
  return "sortable " + (sort.dir === "asc" ? "sort-asc" : "sort-desc");
}

// 近 14 天每U效率小走勢（每日帳本），目前倉位與倉位紀錄共用，10 分鐘內不重抓。
let sparkData = null;
let sparkAt = 0;
export async function ensureSparks() {
  if (sparkData && Date.now() - sparkAt < 10 * 60 * 1000) return;
  try {
    sparkData = await get("/api/v1/efficiency/sparklines?days=14");
    sparkAt = Date.now();
  } catch (_) {
    sparkData = null;
  }
}

function sparkCell(id) {
  const vals = (sparkData && sparkData.rows && sparkData.rows[id]) || [];
  const got = vals.filter((v) => v != null && Number.isFinite(v));
  const avg = got.length ? got.reduce((a, b) => a + b, 0) / got.length : null;
  const note = avg == null ? "近 14 天沒有完整的每日紀錄"
    : `近 ${got.length} 天平均每U效率 ${signed(avg, 3)}%／日（3 倍換算）`;
  return `<td class="n c-spark" data-tip="${esc(note)}">${Charts.spark(vals, { width: 72, height: 20 })}`
    + `<span class="sub">${avg == null ? "" : "均 " + signed(avg, 3) + "%"}</span></td>`;
}

function sizeSub(r) {
  const lev = r.equivalent_leverage != null ? `${n(r.equivalent_leverage, 2)}x` : "";
  if (moneyNum(r.extra_margin)) return `等效 ${n(moneyNum(r.equivalent), 0)} · ${lev}`;
  return lev ? `等效槓桿 ${lev}` : "";
}

export function positionTable(rows, profitKey, sort) {
  if (!rows.length) return "<p class='muted'>沒有倉位列</p>";
  const ordered = sortedRows(rows, sort, profitKey);
  const spark = profitKey === "24h" && !!sparkData;
  const dayTip = profitKey === "daily"
    ? "跟上一次記錄比，這筆倉的網格利潤多了或少了多少。第一天是 0。幣本位（ETH）是當天新增的幣×抓取價，不是把畫面 USDT 直接相減。"
    : "派網回傳的過去 24 小時網格利潤，不是日記帳。";
  const h = (key, extra, label, unit, note) =>
    `<th class="${extra} ${sortClass(key, sort)}" data-sort="${key}">`
    + `<span class="hd-top">${note ? tip(label, note) : label}<i class="arrow"></i></span>`
    + `<span class="hd-unit">${unit || ""}</span></th>`;
  return `<table class="board${spark ? " has-spark" : ""}"><colgroup>
    <col class="c-date"><col class="c-size"><col class="c-sym"><col class="c-mark">
    <col class="c-liq"><col class="c-dist"><col class="c-grid"><col class="c-day"><col class="c-pct">${spark ? '<col class="c-spark">' : ""}
  </colgroup><thead><tr>
    ${h("opened_at", "t c-date", "建倉日期", "台北時間")}${h("size", "n c-size", "倉位大小", "等效 · 等效槓桿", "大字是派網顯示的投入（倉位）。小字：等效倉位＝投入＋追加保證金；等效槓桿＝投入 × 派網槓桿 ÷ 等效倉位，也就是把你留在倉裡的保證金算進去後的槓桿。調高槓桿但保證金留在原倉時，這兩個數字都不變。")}${h("symbol", "t c-sym", "標的", "派網槓桿 / 方向")}${h("mark", "n c-mark", "現價", "Mark")}
    ${h("liq", "n c-liq", "強平價", "預估")}${h("dist", "n c-dist", "強平距離", "剩餘 %", "離強平還剩多少％，0 到 100。多數是 U 本位；幣本位會另外換算。")}${h("grid", "n c-grid", "網格利潤", "USDT", "這筆倉目前帳上的累計網格利潤。複投後會從 0 再算。")}${h("day", "n c-day", "單日利潤", "USDT", dayTip)}${h("pct", "n c-pct", "每U效率", "3 倍 · %", "單日利潤 ÷ 倉位大小 × 3 ÷ 原槓桿倍率 × 100%。統一按比例換算為 3 倍槓桿，比較不同倉位的配對收益效率。")}${spark
    ? `<th class="n c-spark"><span class="hd-top">${tip("近 14 天", "每日帳本的每U效率（3 倍換算）走勢，虛線是 0。滑過看平均。")}</span><span class="hd-unit">每U效率</span></th>` : ""}
  </tr></thead><tbody>${ordered.map((r) => {
    const day = profitKey === "daily" ? r.daily_profit : r.profit_24h;
    const dayV = day && day.usdt;
    const size = r.investment && r.investment.usdt != null ? r.investment : r.size;
    const pct = efficiency3x(moneyNum(day), moneyNum(size), r.leverage);
    const opened = String(r.opened_at || "").replace("T", " ");
    const openDate = opened.split(" ")[0] || "";
    const openTime = opened.split(" ")[1] || "";
    const chip = profitKey === "daily" ? eventChip(r.event || r.status) : "";
    return `<tr class="clickable" data-id="${esc(r.bu_order_id || "")}">
      <td class="t c-date">${cell(esc(openDate) || "—", esc(openTime))}</td>
      <td class="n c-size">${cell(usdt(size), sizeSub(r))}</td>
      <td class="t c-sym">${cell(esc(r.symbol || "—"), esc(r.leverage || "") + chip)}</td>
      <td class="n c-mark">${cell(n(r.mark_price, 4), "")}</td>
      <td class="n c-liq">${cell(n(r.liq_price, 4), "")}</td>
      <td class="n c-dist ${cls(r.liq_distance_pct)}">${cell(r.liq_distance_pct == null ? "—" : n(r.liq_distance_pct, 2) + "%", "")}</td>
      <td class="n c-grid">${cell(usdt(r.grid_profit), twd(r.grid_profit))}</td>
      <td class="n c-day ${cls(dayV)}">${cell(signed(dayV), twd(day))}</td>
      <td class="n c-pct ${cls(pct)}">${cell(pct == null ? "—" : signed(pct, 3) + "%", "／U")}</td>${spark ? sparkCell(r.bu_order_id) : ""}
    </tr>`;
  }).join("")}</tbody></table>`;
}

function toggleSort(sort, key) {
  if (sort.key === key) sort.dir = sort.dir === "asc" ? "desc" : "asc";
  else {
    sort.key = key;
    sort.dir = (key === "symbol") ? "asc" : "desc";
  }
}

export function bindSortHeaders(sort, onChange) {
  app.querySelectorAll("th.sortable").forEach((th) => {
    th.onclick = (e) => {
      e.stopPropagation();
      toggleSort(sort, th.dataset.sort);
      onChange();
    };
  });
}

export function summaryCards(board, timeLabel, timeValue, gridTip, trueTip) {
  const total = board.total_profit_24h && board.total_profit_24h.usdt;
  const pct24 = board.profit_24h_pct;
  return `<div class="stats">
      <div class="stat wide"><span class="label">錢包總市值</span><strong>${board.wallet_total && board.wallet_total.usdt != null ? both(board.wallet_total) : "—"}</strong></div>
      <div class="stat wide">${tip("過去 24 小時總利潤", "派網回傳的近 24 小時網格利潤加總。下面的％＝這筆 ÷ 等效倉位，調槓桿不會讓它跳動。")}<strong class="${cls(total)}">${bothSigned(board.total_profit_24h)}${pct24 == null || pct24 === "" ? "" : `<span class="pct ${cls(pct24)}">${signed(pct24)}%</span>`}</strong></div>
      <div class="stat wide">${tip("等效倉位", "投入＋追加保證金。調高槓桿但把保證金留在原倉時，保證金只是從「投入」移到「追加保證金」，這個數字不變；加倉、減倉才會變。")}<strong>${both(board.total_equivalent || board.total_investment)}${board.leverage_equivalent ? `<span class="twd">等效槓桿 ${n(board.leverage_equivalent, 2)} 倍</span>` : ""}</strong></div>
      <div class="stat">${tip("倉位加總（派網顯示）", "各倉「投入」加總，就是派網畫面上的倉位。調高槓桿會讓它變小，和等效倉位的差距就是移到追加保證金的部分。")}<strong>${both(board.total_investment)}<span class="twd">${board.position_count || 0} 倉${board.leverage_nominal ? ` · 名目槓桿 ${n(board.leverage_nominal, 2)} 倍` : ""}</span></strong></div>
      <div class="stat">${tip("運行中配對利潤", gridTip)}<strong>${both(board.total_grid_profit)}</strong></div>
      <div class="stat">${tip(board.kind === "live" ? "歷史累計配對利潤" : "歷史累計配對利潤（含已關倉）", trueTip)}<strong>${board.kind === "live" ? '<span style="font-size:14px">請見每日倉位紀錄</span>' : both(board.true_grid_profit)}</strong></div>
      <div class="stat meta"><span class="label">${timeLabel}</span><strong>${timeValue}</strong></div>
    </div>`;
}

export function taipeiToday() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Taipei" }).format(new Date());
}

export async function postJson(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal: AbortSignal.timeout(360000),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || (url + " " + res.status));
    return data;
  } catch (err) {
    if (err && err.name === "TimeoutError") throw new Error("連線逾時（360 秒），請稍後再試");
    throw err;
  }
}

export function eventLabel(ev) {
  const map = { baseline:"基準", new:"新增", continue:"續存", closed:"關倉", closed_unresolved:"關倉未對上", reinvest:"複投", add:"加倉", review:"待確認", withdraw:"提取", reduce:"減倉" };
  return String(ev || "").split("+").map((p) => map[p] || p).filter(Boolean).join("＋") || "—";
}

export function eventChip(ev) {
  const key = String(ev || "");
  if (!key || key === "continue" || key === "baseline" || key === "new") return "";
  const kind = key.split("+").includes("review") ? "is-review" : key.split("+").includes("add") ? "is-add" : "";
  return ` <span class="event-tag ${kind}">${esc(eventLabel(key))}</span>`;
}

export function eventCell(r, date) {
  const ev = r.event || r.status || "";
  const review = String(ev).split("+").includes("review");
  let html = `<span class="event-tag${review ? " is-review" : String(ev).split("+").includes("add") ? " is-add" : ""}">${esc(eventLabel(ev))}</span>`;
  if (review && r.bu_order_id) {
    html += `<div class="decide">
      <button type="button" class="btn tiny" data-decide="add" data-date="${esc(date)}" data-id="${esc(r.bu_order_id)}">這是加倉</button>
      <button type="button" class="btn tiny" data-decide="reinvest" data-date="${esc(date)}" data-id="${esc(r.bu_order_id)}">這是複投</button>
    </div>`;
  }
  return html;
}

export function bindHistoryClicks() {
  app.querySelectorAll("tr[data-id]").forEach((tr) => {
    if (!tr.dataset.id) return;
    tr.tabIndex = 0;
    const open = () => {
      state.historyFrom = state.page;
      state.historyId = tr.dataset.id;
      goHistory();
    };
    tr.onclick = (ev) => {
      if (ev.target.closest("[data-decide], button, a")) return;
      open();
    };
    tr.onkeydown = (ev) => {
      if (ev.key === "Enter" && ev.target === tr) { ev.preventDefault(); open(); }
    };
  });
}

// Late-bound so common.js does not import the router (that import would cycle).
let goHistory = () => {};
export function registerGoHistory(fn) {
  goHistory = fn;
}

// 共用確認／輸入視窗：回傳 null（取消）、true，或 { value, check }。
export function askDialog({ title, body = "", input = null, okText = "確定", danger = false }) {
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "mask";
    mask.innerHTML = `<div class="dialog" role="dialog" aria-modal="true"><h3>${esc(title)}</h3>${body ? `<p>${body}</p>` : ""}
      ${input ? `<label class="field">${esc(input.label)}<input id="askInput" maxlength="${input.max || 40}" value="${esc(input.value || "")}"></label>
        ${input.check ? `<label class="eff-custom"><input type="checkbox" id="askCheck" ${input.check.value ? "checked" : ""}> ${esc(input.check.label)}</label>` : ""}` : ""}
      <div class="actions"><button class="btn ghost" type="button" data-act="no">取消</button>
        <button class="btn${danger ? " danger" : ""}" type="button" data-act="ok">${esc(okText)}</button></div></div>`;
    document.body.appendChild(mask);
    const field = mask.querySelector("#askInput");
    const done = (ok) => {
      const check = mask.querySelector("#askCheck");
      const out = !ok ? null : input ? { value: field.value.trim(), check: !!(check && check.checked) } : true;
      mask.remove();
      resolve(out);
    };
    mask.querySelector('[data-act="no"]').onclick = () => done(false);
    mask.querySelector('[data-act="ok"]').onclick = () => done(true);
    mask.addEventListener("keydown", (e) => {
      if (e.key === "Escape") done(false);
      else if (e.key === "Enter" && field) done(true);
    });
    (field || mask.querySelector('[data-act="ok"]')).focus();
  });
}
