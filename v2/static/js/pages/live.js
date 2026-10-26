import { state } from "../state.js";

import { app, ageText, bindHistoryClicks, bindSortHeaders, clearFlash, ensureSparks, fmtTaipei, get, n, positionTable, postJson, publishText, showFlash, summaryCards, taipeiToday } from "../common.js";

import { isCurrent } from "../router.js";

let liveData = null;
let liveBusy = false;

function paintLive() {
  const board = liveData || { available: false, rows: [] };
  const fxRate = board.fx && (board.fx.usdt_twd != null ? board.fx.usdt_twd : board.fx.usd_twd);
  const hasLive = !!(board.available && board.as_of);
  const today = taipeiToday();
  const age = hasLive ? ageText(board.as_of) : null;
  app.innerHTML = `
    <div class="live-bar">
      <button class="btn" id="liveRefresh" ${liveBusy ? "disabled" : ""}>${liveBusy ? "更新中…" : "更新目前倉位"}</button>
      <button class="btn ghost" id="liveCommit" ${hasLive && !liveBusy ? "" : "disabled"}>把現在倉位寫入倉位歷史</button>
      <span class="note">${hasLive
        ? `暫存時間：${fmtTaipei(board.as_of)}${age ? `（<span class="${age.stale ? "warn" : ""}">${age.text}</span>）` : ""}。只留在這一頁，不會改每日快照；排程每 30 分鐘更新一次。`
        : "尚未抓過。按「更新目前倉位」只刷新這一頁暫存，不影響每日 08:15 紀錄。"}</span>
    </div>`
    + summaryCards(board, "即時更新時間", hasLive ? fmtTaipei(board.as_of) : "尚未抓過",
        "現在還開著的倉，把派網畫面上的網格利潤加起來。",
        "目前運行中倉位的配對利潤加總，已包含已提領收益。含已關倉成果及複投調整的歷史累計請見每日倉位紀錄。")
    + `<div class="card">
      <p class="muted">這一頁是暫存屏，跟每日 08:15 快照分開。按「更新目前倉位」只抓此刻並留在這裡。台幣 USDT→TWD ${fxRate != null ? n(fxRate, 3) : "—"}。</p>
      ${hasLive ? positionTable(board.rows || [], "24h", state.boardSort) : "<p class='muted'>按「更新目前倉位」後才會出現表格。</p>"}
    </div>`;
  if (hasLive) {
    bindSortHeaders(state.boardSort, paintLive);
    bindHistoryClicks();
  }
  document.getElementById("liveRefresh").onclick = async () => {
    if (liveBusy) return;
    liveBusy = true;
    paintLive();
    try {
      liveData = await postJson("/api/v1/live/refresh", {});
    } catch (err) {
      liveBusy = false;
      if (state.page === "live") paintLive();
      const network = /fetch failed|terminated|ECONN|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|UND_ERR/i.test(err.message);
      const clock = /INVALID_TIMESTAMP/.test(err.message);
      showFlash("更新目前倉位失敗：" + err.message
        + (network ? "。這是連不上派網（網路一時斷線），稍後再按一次即可；排程每 30 分鐘仍會自動更新。" : "")
        + (clock ? "。電腦時鐘和派網差超過 10 秒，請到 Windows 設定 → 時間與語言 → 日期和時間按「立即同步」後再按一次。" : ""), "error");
      return;
    }
    liveBusy = false;
    clearFlash();
    if (state.page === "live") paintLive();
  };
  document.getElementById("liveCommit").onclick = () => {
    if (!hasLive) return;
    const old = document.getElementById("commitMask");
    if (old) old.remove();
    const mask = document.createElement("div");
    mask.id = "commitMask";
    mask.className = "mask";
    mask.innerHTML = `<div class="dialog">
        <h3>把現在倉位寫入倉位歷史？</h3>
        <p>只有這一顆備援鈕才會動每日紀錄。會先向派網<strong>完整重抓一次</strong>（約 15 秒），再寫成台北時間 <strong>${today}</strong> 的倉位歷史。今天若已有 08:15 快照，會被換成這一份，並重新發布到 Portfolio。Excel 排程本身不會被改。</p>
        <p class="muted">再按「確定寫入」才會存。取消則什麼都不改。</p>
        <div class="actions">
          <button class="btn ghost" type="button" id="commitCancel">取消</button>
          <button class="btn danger" type="button" id="commitOk">確定寫入</button>
        </div>
      </div>`;
    document.body.appendChild(mask);
    document.getElementById("commitCancel").onclick = () => mask.remove();
    document.getElementById("commitOk").onclick = async () => {
      mask.remove();
      showFlash("正在向派網完整重抓並寫入，約 15 秒…");
      try {
        const result = await postJson("/api/v1/live/commit-daily", { confirm_date: today });
        showFlash("已寫入 " + (result.capture_date || today) + " 的倉位歷史。" + publishText(result.publish));
      } catch (err) {
        showFlash("寫入失敗：" + err.message, "error");
      }
    };
  };
}

export async function renderLive(seq) {
  await ensureSparks();
  if (!isCurrent(seq, "live")) return;
  liveData = await get("/api/v1/live");
  if (!isCurrent(seq, "live")) return;
  paintLive();
}

// The 30-minute task rewrites the server's cached snapshot. Re-read that cache (no Pionex call)
// when the tab comes back into view and every 5 minutes, so an open page never sits on an old capture.
async function pollLive() {
  if (state.page !== "live" || liveBusy || document.hidden || document.getElementById("commitMask")) return;
  let next;
  try { next = await get("/api/v1/live"); } catch { return; }
  if (state.page !== "live" || liveBusy || !next || !next.as_of) return;
  if (liveData && liveData.as_of === next.as_of) return;
  liveData = next;
  paintLive();
}

let livePolling = false;
export function startLivePolling() {
  if (livePolling) return;
  livePolling = true;
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollLive(); });
  setInterval(pollLive, 5 * 60 * 1000);
}
