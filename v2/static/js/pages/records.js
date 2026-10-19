import { state } from "../state.js";

import { app, bindHistoryClicks, bindSortHeaders, ensureSparks, fmtTaipei, get, n, positionTable, summaryCards } from "../common.js";

let boardData = null;

function paintBoard() {
  const board = boardData;
  const fxRate = board.fx && (board.fx.usdt_twd != null ? board.fx.usdt_twd : board.fx.usd_twd);
  app.innerHTML = summaryCards(board, "每日快照時間", board.as_of ? fmtTaipei(board.as_of) : "尚無",
      "目前運行中倉位的配對利潤加總，已包含已提領收益。關倉後該倉位移出加總。",
      "基準日帶入加上之後的每日配對增量，保留已關倉成果並調整複投。提領已含在派網回報值，不另加一次；加倉本金不算利潤。")
    + `<div class="card">
      <p class="muted">這頁是上一次每日快照（約 08:05 或手動抓取），不是即時。台幣 USDT→TWD ${fxRate != null ? n(fxRate, 3) : "—"}。虛線名稱可點說明；表頭可排序。</p>
      ${positionTable(board.rows || [], "24h", state.boardSort)}
    </div>`;
  bindSortHeaders(state.boardSort, paintBoard);
  bindHistoryClicks();
}

export async function renderBoard() {
  await ensureSparks();
  boardData = await get("/api/v1/board");
  paintBoard();
}
