import { state } from "../state.js";

import { app, cls, esc, fmtTaipei, get, n, postJson, publishText, showFlash, signed, tip } from "../common.js";

import { isCurrent } from "../router.js";

let settlementData = null;
let settlementEditId = "";
const CLOSE_REASON = { profit_stop: "止盈", loss_stop: "止損", user_cancel: "手動關倉", liquidation: "強平", liquidate: "強平", expired: "到期" };
const AUDIT_LABELS = { closed_at: "關倉時間", grid_profit: "最終配對", lifetime_grid_profit: "完整配對", net_profit: "淨損益",
  own_cash_in: "自己投入", funding_fee: "資金費", fee: "手續費", settlement_usdt_rate: "換算價", note: "備註" };
const reasonLabel = (r) => r ? (CLOSE_REASON[r] || r) : "—";
const curDigits = (cur) => (cur || "USDT") === "USDT" ? 2 : 6;
const amtS = (v, cur) => v == null || v === "" ? "—" : signed(v, curDigits(cur));
const amtP = (v, cur) => v == null || v === "" ? "—" : n(v, curDigits(cur));

function toUsdt(v, r) {
  if (v == null || v === "") return null;
  if ((r.currency || "USDT") === "USDT") return Number(v);
  return r.settlement_usdt_rate ? Number(v) * Number(r.settlement_usdt_rate) : null;
}

function settlementState(r) {
  if (r.status !== "settled") return `<span class="badge warn">淨損益待確認</span>`;
  if (r.net_method === "manual") return `<span class="badge">人工補登</span>`;
  if (r.needs_review) return `<span class="badge warn">需核對 · 兩法差 ${amtP(Math.abs(Number(r.net_check_diff)), r.currency)}</span>`;
  if (r.own_cash_in_source === "manual") return `<span class="badge ok">投入已人工確認</span>`;
  if (r.net_check_ok) return `<span class="badge ok">自動確認 · 兩法一致</span>`;
  return `<span class="badge ok">自動確認</span>`;
}

export function closureCard(r) {
  const cur = r.currency || "USDT";
  return `<div class="card"><h3>關倉結算　${settlementState(r)}</h3>
    <div class="stats">
      <div class="stat"><span class="label">最終淨損益</span><strong class="${cls(r.net_profit)}">${amtS(r.net_profit, cur)}<span class="unit">${esc(cur)}</span>${r.roi_pct != null ? `<span class="pct ${cls(r.roi_pct)}">${signed(r.roi_pct)}%</span>` : ""}</strong></div>
      <div class="stat"><span class="label">自己投入</span><strong>${amtP(r.own_cash_in, cur)}<span class="unit">${esc(cur)}</span></strong></div>
      <div class="stat"><span class="label">實際拿回</span><strong>${amtP(r.received, cur)}<span class="unit">${esc(cur)}</span></strong></div>
      <div class="stat meta"><span class="label">關倉原因／持有</span><strong>${esc(reasonLabel(r.close_reason))} · ${r.hold_days ?? "—"} 天</strong></div>
    </div>
    <p class="muted"><a href="#settlements">到關倉紀錄查看計算明細或修正</a></p>
  </div>`;
}

function settlementTotals(rows) {
  const t = { net: 0, grid: 0, nonGrid: 0, win: 0, loss: 0, settled: 0, coins: {} };
  rows.forEach((r) => {
    const cur = r.currency || "USDT";
    if (r.status === "settled" && r.net_profit != null) {
      const v = Number(r.net_profit);
      t.settled += 1;
      if (v > 0) t.win += 1; else if (v < 0) t.loss += 1;
      const u = toUsdt(r.net_profit, r);
      if (u != null) t.net += u;
      if (cur !== "USDT") t.coins[cur] = (t.coins[cur] || 0) + v;
    }
    const g = toUsdt(r.lifetime_grid_profit, r);
    if (g != null) t.grid += g;
    const ng = toUsdt(r.non_grid_profit, r);
    if (ng != null) t.nonGrid += ng;
  });
  return t;
}

function paintSettlements() {
  const rows = (settlementData && settlementData.rows) || [];
  const selected = rows.find((r) => r.id === settlementEditId);
  const t = settlementTotals(rows);
  const coinLine = Object.entries(t.coins).map(([c, v]) => `${signed(v, 6)} ${esc(c)}`).join("、");
  const pending = settlementData.pending_count || 0;
  const flagged = rows.filter((r) => r.needs_review).length;
  app.innerHTML = `<div class="live-bar"><button class="btn" id="settlementRefresh">補抓關倉資料</button>
      <span class="note">${rows.length} 筆關倉${pending ? `，<span class="warn">${pending} 筆淨損益待確認</span>` : "，全部已確認"}${flagged ? `，<span class="warn">${flagged} 筆兩法差距需核對</span>` : ""}。淨損益＝實際回到錢包的錢 − 自己投入的錢；與每日配對利潤分開，不會再從錢包扣一次。</span></div>
    <div class="stats">
      <div class="stat">${tip("已確認淨損益合計", "所有已確認關倉的最終淨損益，幣本位依關倉換算價折成 USDT。")}<strong class="${cls(t.net)}">${signed(t.net)}<span class="unit">USDT</span>${coinLine ? `<span class="twd">其中幣本位 ${coinLine}</span>` : ""}</strong></div>
      <div class="stat"><span class="label">獲利／虧損筆數</span><strong><span class="up">${t.win}</span> / <span class="down">${t.loss}</span><span class="unit">共 ${t.settled} 筆</span></strong></div>
      <div class="stat">${tip("完整配對利潤合計", "每筆關倉一生的配對收益，含已提領與複投。")}<strong>${n(t.grid)}<span class="unit">USDT</span></strong></div>
      <div class="stat">${tip("非配對淨損益合計", "淨損益扣掉配對收益：趨勢盈虧、手續費、資金費等。")}<strong class="${cls(t.nonGrid)}">${signed(t.nonGrid)}<span class="unit">USDT</span></strong></div>
    </div>
    ${rows.length ? `<div class="card"><div class="card-head"><h3>每筆關倉損益</h3>
      <span class="note">藍綠＝完整配對利潤；綠／紅＝非配對（趨勢、手續費、資金費）；黑點＝最終淨損益。幣本位依換算價折成 USDT。</span></div>
      <div class="chart" id="settleBars"></div></div>` : ""}
    <div class="card">
      <table class="plain wide" style="min-width:1120px"><thead><tr>
        <th>標的／關倉時間</th><th>原因／持有</th><th>自己投入</th><th>實際拿回</th><th>最終淨損益</th>
        <th>${tip("配對／非配對", "上：完整配對利潤（含提領、複投）；下：淨損益扣掉配對後的部分。")}</th><th>資金費／手續費</th><th>狀態</th><th></th>
      </tr></thead>
      <tbody>${rows.map((r) => {
        const cur = r.currency || "USDT";
        const usdtNet = cur !== "USDT" && r.net_profit_usdt != null ? `≈ ${signed(r.net_profit_usdt)} USDT` : "";
        return `<tr>
        <td>${esc(r.symbol)}${r.bonus ? ' <span class="badge">體驗金</span>' : ""}<small>${esc(r.closed_at ? fmtTaipei(r.closed_at).slice(0, 16) : "關倉時間待補")}</small></td>
        <td>${esc(reasonLabel(r.close_reason))}<small>${r.hold_days != null ? `持有 ${r.hold_days} 天` : ""}</small></td>
        <td>${amtP(r.own_cash_in, cur)}<small>${esc(cur)}${r.own_cash_in_source === "manual" ? " · 人工" : r.bonus ? " · 體驗金不計" : ""}</small></td>
        <td>${amtP(r.received, cur)}<small>${Number(r.withdrawn_total) ? `另已提領 ${amtP(r.withdrawn_total, cur)}` : esc(cur)}</small></td>
        <td class="${cls(r.net_profit)}"><b>${amtS(r.net_profit, cur)}</b><small>${r.roi_pct != null ? signed(r.roi_pct) + "%" : ""}${usdtNet ? " " + usdtNet : ""}</small></td>
        <td>${amtS(r.lifetime_grid_profit, cur)}<small class="${cls(r.non_grid_profit)}">非配對 ${amtS(r.non_grid_profit, cur)}</small></td>
        <td>${amtS(r.funding_fee, cur)}<small>手續費 ${amtS(r.fee, cur)}</small></td>
        <td>${settlementState(r)}<small>${r.daily_reconciled ? "配對日報已對帳" : "配對日報待對帳"}${r.audit_count ? ` · 修改 ${r.audit_count} 次` : ""}</small></td>
        <td><button class="btn ghost tiny" data-settlement-edit="${esc(r.id)}">明細／修正</button></td>
      </tr>`;
      }).join("") || '<tr><td colspan="9" class="muted">尚無關倉紀錄</td></tr>'}</tbody></table>
      <p class="note">自己投入＝派網投入額＋追加保證金−複投（複投的錢已在拿回金額內）。體驗金倉不算自己的錢，只計實際解鎖金額。交叉核對用派網已實現損益＋手續費＋資金費，差距過大時請在明細確認自己投入的金額。</p>
    </div>${selected ? settlementForm(selected) : ""}`;
  app.querySelectorAll("[data-settlement-edit]").forEach((btn) => btn.onclick = () => {
    settlementEditId = btn.dataset.settlementEdit;
    paintSettlements();
    document.getElementById("settlementForm").scrollIntoView({ block: "start", behavior: "smooth" });
  });
  document.getElementById("settlementRefresh").onclick = async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true; btn.textContent = "抓取中…";
    try {
      settlementData = await postJson("/api/v1/settlements/refresh", {});
      showFlash("關倉資料已更新。" + publishText(settlementData.publish));
      if (state.page === "settlements") paintSettlements();
    } catch (error) {
      showFlash("補抓失敗：" + error.message, "error");
      btn.disabled = false; btn.textContent = "補抓關倉資料";
    }
  };
  if (selected) bindSettlementForm(selected);
  drawSettlementBars(rows);
}

function drawSettlementBars(rows) {
  const el = document.getElementById("settleBars");
  if (!el) return;
  const accent = Charts.token("--accent", "#2e6970");
  const up = Charts.token("--up", "#1f7a4d"), down = Charts.token("--down", "#b42318");
  const fmtU = (v) => (v == null || !Number.isFinite(v) ? "—" : signed(v) + " U");
  Charts.hbars(el, {
    label: "每筆關倉損益", format: (v) => String(Math.round(v)),
    rows: rows.map((r) => {
      const grid = toUsdt(r.lifetime_grid_profit, r);
      const other = toUsdt(r.non_grid_profit, r);
      const net = r.status === "settled" ? toUsdt(r.net_profit, r) : null;
      const when = r.closed_at ? fmtTaipei(r.closed_at).slice(0, 10) : "關倉時間待補";
      return {
        label: (r.symbol || "—") + (r.bonus ? " · 體驗金" : ""),
        sub: `${when} · ${reasonLabel(r.close_reason)}`,
        parts: [{ value: grid, color: accent }, { value: other, color: other != null && other < 0 ? down : up }],
        marker: net,
        tip: `${r.symbol || "—"}（${when}）配對 ${fmtU(grid)}、非配對 ${fmtU(other)}、淨損益 ${r.status === "settled" ? fmtU(net) : "待確認"}`,
      };
    }),
  });
}

function bindSettlementForm(selected) {
  document.getElementById("settlementCancel").onclick = () => { settlementEditId = ""; paintSettlements(); };
  document.getElementById("settlementForm").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = Object.fromEntries(new FormData(form));
    const btn = form.querySelector("[type=submit]"); btn.disabled = true;
    try {
      settlementData = await postJson("/api/v1/settlements/" + encodeURIComponent(selected.id), body);
      showFlash("已保存並重新計算。" + publishText(settlementData.publish));
      settlementEditId = "";
      if (state.page === "settlements") paintSettlements();
    } catch (error) {
      showFlash("保存失敗：" + error.message, "error");
      btn.disabled = false;
    }
  };
  const box = document.getElementById("auditBox");
  if (box) box.ontoggle = async () => {
    if (!box.open || box.dataset.loaded) return;
    box.dataset.loaded = "1";
    const list = box.querySelector("ul");
    try {
      const data = await get("/api/v1/settlements/" + encodeURIComponent(selected.id) + "/audit");
      if (state.page !== "settlements") return;
      list.innerHTML = (data.rows || []).map((a) => {
        const keys = [...new Set([...Object.keys(a.before || {}), ...Object.keys(a.after || {})])]
          .filter((k) => (a.before || {})[k] !== (a.after || {})[k]);
        const parts = keys.map((k) => `${esc(AUDIT_LABELS[k] || k)}：${esc((a.before || {})[k] ?? "自動")} → ${esc((a.after || {})[k] ?? "自動")}`);
        return `<li><b>${esc(fmtTaipei(a.changed_at).slice(0, 16))}</b>　${parts.join("；") || "無變更"}</li>`;
      }).join("") || '<li class="muted">沒有紀錄</li>';
    } catch (err) {
      if (state.page !== "settlements") return;
      list.innerHTML = `<li class="warn">${esc(err.message)}</li>`;
    }
  };
}

function settlementForm(row) {
  const manual = row.manual || {};
  const cur = row.currency || "USDT";
  const coin = cur !== "USDT";
  const apiOwn = row.bonus ? "0（體驗金）"
    : row.investment != null ? amtP(Number(row.investment) - Number(row.reinvest_total || 0), cur) : "—";
  const input = (key, label, placeholder = "", help = "") => `<label class="field">${label}<input name="${key}" inputmode="decimal" value="${esc(manual[key] ?? "")}" placeholder="${esc(placeholder)}">${help ? `<span class="help">${help}</span>` : ""}</label>`;
  const rateSource = { close_price: "依派網關倉價自動換算", manual: "人工填寫" }[row.settlement_rate_source] || "待補";
  return `<form class="card" id="settlementForm"><h3>${esc(row.symbol)} · 結算明細（${esc(cur)}）　${settlementState(row)}</h3>
    <div class="formula">
      實際拿回 <b>${amtP(row.received, cur)}</b> ＋ 期間已提領 <b>${amtP(row.withdrawn_total, cur)}</b> − 自己投入 <b>${amtP(row.own_cash_in, cur)}</b>
      ＝ 淨損益 <b class="${cls(row.net_profit)}">${amtS(row.net_profit, cur)}</b> ${esc(cur)}${row.net_method === "manual" ? "（目前採用人工填寫的淨損益）" : ""}
      <br><span class="muted">交叉核對：派網已實現損益＋手續費＋資金費${row.bonus ? "−體驗金分潤（虧損由體驗金承擔）" : ""}＝${amtS(row.net_check, cur)}；${row.net_check_diff == null ? "資料不足，無法比對" : row.net_check_ok ? "兩法一致" : row.needs_review ? `<span class="warn">相差 ${amtS(row.net_check_diff, cur)}，請確認自己投入的金額</span>` : `相差 ${amtS(row.net_check_diff, cur)}，已採用人工確認的投入金額`}</span>
      ${Number(row.reinvest_total) ? `<br><span class="muted">期間複投 ${amtP(row.reinvest_total, cur)} 已包含在拿回的金額裡，所以從投入扣除，不重複計算。</span>` : ""}
      ${coin ? `<br><span class="muted">換算價 ${row.settlement_usdt_rate ? n(row.settlement_usdt_rate, 2) + " USDT／" + esc(cur) : "—"}（${rateSource}）${row.net_profit_usdt != null ? `，淨損益約 ${signed(row.net_profit_usdt)} USDT` : ""}</span>` : ""}
    </div>
    <p class="note">一般不需要填。只有兩法差距大、或與派網結算明細不同時才修正；空白代表使用自動值，每次修改都會留下紀錄並重新發布到 Portfolio。</p>
    <div class="form-grid">
      ${input("own_cash_in", "自己投入（含加倉、追加保證金，不含複投）", "自動：" + apiOwn, "只填這格就會重算淨損益")}
      ${input("net_profit", "最終淨損益（直接覆蓋）", row.net_method === "wallet" ? "自動：" + amtS(row.net_profit, cur) : "")}
      <label class="field">實際關倉時間（台北）<input name="closed_at" type="datetime-local" step="1" value="${esc(manual.closed_at ? fmtTaipei(manual.closed_at).slice(0, 19).replace(" ", "T") : "")}"></label>
      ${input("grid_profit", "最終配對利潤（派網最後顯示值）", "目前：" + n(row.grid_profit, 8))}
      ${input("lifetime_grid_profit", "完整累計配對利潤（含已提領／複投）", "目前：" + amtP(row.lifetime_grid_profit, cur))}
      ${input("funding_fee", "資金費（收入正數、支出負數；僅供對帳）", "目前：" + amtS(row.funding_fee, cur))}
      ${input("fee", "手續費（僅供對帳，不重複扣除）", "目前：" + amtS(row.fee, cur))}
      ${coin ? input("settlement_usdt_rate", `換算價（每 1 ${esc(cur)} 等於多少 USDT）`, row.settlement_usdt_rate ? "目前：" + n(row.settlement_usdt_rate, 2) : "") : ""}
    </div>
    <label class="field">結算依據／備註<textarea name="note" maxlength="1000" rows="2">${esc(manual.note || "")}</textarea></label>
    <button type="submit" class="btn">保存並重新計算</button> <button type="button" class="btn ghost" id="settlementCancel">關閉</button>
    ${row.audit_count ? `<details id="auditBox"><summary>修改紀錄（${row.audit_count}）</summary><ul class="audit-list"><li class="muted">載入中…</li></ul></details>` : ""}
  </form>`;
}

export async function renderSettlements(seq) {
  const data = await get("/api/v1/settlements");
  if (!isCurrent(seq, "settlements")) return;
  settlementData = data;
  paintSettlements();
}
