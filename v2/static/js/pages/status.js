import { state } from "../state.js";

import { app, esc, fmtTaipei, get, postJson, publishText, showFlash, signed, tip } from "../common.js";

import { isCurrent } from "../router.js";

export async function renderStatus(seq) {
  const s = await get("/api/v1/status");
  if (!isCurrent(seq, "status")) return;
  const missing = (s.missing_dates || []);
  const gap = s.coin_fx_gap_month && s.coin_fx_gap_month.usdt != null ? signed(s.coin_fx_gap_month.usdt) + " USDT" : "—";
  const pub = s.publish || {};
  const ledgerPub = pub.ledger || {};
  const livePub = pub.live || {};
  const inSync = pub.ledger_in_sync;
  const syncWarn = pub.configured && inSync === false;
  const pubLine = (st) => !st || !st.last_attempt_at ? "尚無紀錄"
    : st.ok ? `成功 · ${fmtTaipei(st.last_success_at)}`
    : `失敗 · ${fmtTaipei(st.last_attempt_at)}：${esc(st.error || "")}${st.last_success_at ? `<span class="hintline">上次成功 ${fmtTaipei(st.last_success_at)}</span>` : ""}`;
  const fieldLabel = { daily_profit_i: "每日利潤", cumulative_i: "運行中配對", true_profit_i: "歷史累計", unresolved_count: "待補數" };
  const corrections = s.corrections || [];
  const flags = s.settlement_check_flags || 0;
  const intra = s.intraday || {};
  const intraMissing = intra.missing_24h || [];
  const intraAge = intra.last_at ? (Date.now() - Date.parse(intra.last_at)) / 60000 : null;
  const intraWarn = !intra.available || intraMissing.length > 0 || (intraAge != null && intraAge > 65);
  const fc = s.finished_cache || {};
  const fcCheck = fc.last_full_check || {};
  const fcReuse = fc.last_reuse || {};
  const fcWarn = !!fcCheck.changed;
  const lc = fc.last_list_check || {};
  const ck = s.clock || {};
  const clockSec = ck.offset_ms == null ? null : Number(ck.offset_ms) / 1000;
  const clockWarn = clockSec != null && Math.abs(clockSec) >= 3;
  app.innerHTML = `<div class="card narrow">
    <h3>執行狀態</h3>
    <dl class="statlist">
      <dt>上次成功擷取</dt>
      <dd>${s.last_success ? fmtTaipei(s.last_success.captured_at) : "尚無"}</dd>
      <dt>${tip("關倉配對待補", "關倉時還沒拿到最後配對值的倉位。待補期間該日單日與歷史合計為暫計，Portfolio 月平均也先不計入那天。")}</dt>
      <dd><span class="dot${s.unresolved ? " is-warn" : ""}"></span><span class="${s.unresolved ? "warn" : ""}">${s.unresolved}</span></dd>
      <dt>${tip("關倉淨損益", "依實際回到錢包的錢 − 自己投入的錢自動確認；兩種算法差距過大時標示需核對。")}</dt>
      <dd><span class="dot${s.settlement_unconfirmed || flags ? " is-warn" : ""}"></span>${s.settlement_unconfirmed ? `<span class="warn">${s.settlement_unconfirmed} 筆待確認</span>` : "全部已確認"}${flags ? ` · <span class="warn">${flags} 筆兩法差距需核對</span>` : ""}
        <span class="hintline"><a href="#settlements">前往關倉紀錄</a></span></dd>
      <dt>${tip("盤中資料", "每 30 分鐘排程（:00、:30）把當下倉位記一筆到 intraday.sqlite，效率走勢頁的每小時資料都從這裡來。派網 API 沒有歷史成交，漏掉的時段無法補抓，只能在圖上用前後兩筆估算。")}</dt>
      <dd><span class="dot${intraWarn ? " is-warn" : ""}"></span>${intra.available
        ? `最後一筆 ${fmtTaipei(intra.last_at).slice(5, 16)} · 今天 ${intra.today_count} 筆`
          + `<span class="hintline">${intraMissing.length ? `<span class="warn">近 24 小時漏抓 ${intraMissing.length} 個時段：${esc(intraMissing.slice(-8).join("、"))}${intraMissing.length > 8 ? "…" : ""}</span>` : "近 24 小時沒有漏抓"}</span>`
        : `<span class="warn">尚未開始累積</span><span class="hintline">等下一次 30 分鐘排程，或在目前倉位按「更新目前倉位」。</span>`}</dd>
      <dt>${tip("已關倉明細", "已關倉的網格結算後數字不會再變，所以記在本機 v2-data/finished-details.json。「更新目前倉位」和 30 分鐘排程直接沿用，不再逐筆向派網查；剛關倉 24 小時內、或派網清單有變動的仍會重抓。每日 08:15 照舊全部重抓一次，順便核對本機紀錄。")}</dt>
      <dd><span class="dot${fcWarn ? " is-warn" : ""}"></span>${fc.available
        ? `本機記住 ${fc.count} 筆`
          + (fcReuse.at ? ` · 最近更新沿用 ${fcReuse.reused} 筆、重抓 ${fcReuse.fetched} 筆` : "")
          + `<span class="hintline">${fcCheck.at
            ? `每日全部核對 ${fmtTaipei(fcCheck.at).slice(5, 16)}：${fcCheck.changed ? `<span class="warn">${fcCheck.changed} 筆和本機紀錄不同，已改用派網最新資料</span>` : fcCheck.checked ? `${fcCheck.checked} 筆和本機紀錄一致` : `重抓 ${fcCheck.fetched} 筆並記下`}`
            : "等下一次每日 08:15 擷取做第一次全部核對。"}</span>`
        : `尚未建立<span class="hintline">下一次更新目前倉位時會自動建立。</span>`}</dd>
      <dt>${tip("運行中清單", "派網的運行中清單本身就附上每個網格的完整明細，所以「更新目前倉位」和 30 分鐘排程直接用清單，不再逐筆查。每日 08:15 照舊逐筆查明細，並核對清單有沒有漏資料；有漏就自動改回逐筆查。")}</dt>
      <dd><span class="dot${lc.missing ? " is-warn" : ""}"></span>${!lc.at
        ? `等下一次每日 08:15 擷取做第一次核對。`
        : lc.missing
          ? `<span class="warn">清單缺 ${lc.missing} 個欄位，已自動改回逐筆查明細</span><span class="hintline">${esc((lc.fields || []).join("、"))}</span>`
          : `每日核對 ${lc.checked} 筆：清單資料完整<span class="hintline">${fmtTaipei(lc.at).slice(5, 16)}</span>`}</dd>
      <dt>${tip("電腦時鐘", "派網只接受和它的時間相差 10 秒以內的請求。抓資料時程式會跟著派網的時間走，所以電腦時鐘偏了也能照常抓；這裡只是提醒該校時了。")}</dt>
      <dd><span class="dot${clockWarn ? " is-warn" : ""}"></span>${clockSec == null
        ? "等下一次更新目前倉位後顯示"
        : clockWarn
          ? `<span class="warn">電腦時鐘比派網${clockSec > 0 ? "慢" : "快"} ${Math.abs(clockSec).toFixed(1)} 秒</span>，抓資料時已自動校正<span class="hintline">建議到 Windows 設定 → 時間與語言 → 日期和時間 → 立即同步。${fmtTaipei(ck.at).slice(5, 16)}</span>`
          : `和派網差 ${Math.abs(clockSec).toFixed(1)} 秒，正常<span class="hintline">${fmtTaipei(ck.at).slice(5, 16)}</span>`}</dd>
      <dt>漏抓日期</dt>
      <dd><span class="dot${missing.length ? " is-warn" : ""}"></span>${esc(missing.join("、")) || "無"}</dd>
      <dt>本月幣本位重估差</dt>
      <dd>${gap}<span class="hintline">${esc(s.coin_fx_gap_month_label || "—")}　月底再看要不要把幣價損益拆開。</span></dd>
    </dl>
  </div>
  <div class="card narrow">
    <h3>Portfolio 同步</h3>
    ${pub.configured ? "" : `<p class="warn">找不到 GIST PUBLISH.txt，帳本不會發布到 Portfolio。</p>`}
    <dl class="statlist">
      <dt>${tip("每日帳本", "Gist 第二檔 pionex-grid-ledger.json。Portfolio 月曆、每日利潤、關倉紀錄都讀這一檔。每次都是整份覆蓋，所以歷史修正也會一起傳過去。")}</dt>
      <dd><span class="dot${syncWarn || ledgerPub.ok === false ? " is-warn" : ""}"></span>${inSync == null ? "—" : inSync ? "內容已同步" : `<span class="warn">本機有更新尚未同步</span>`}
        <span class="hintline">${pubLine(ledgerPub)}</span></dd>
      <dt>${tip("目前倉位", "Gist 第三檔 pionex-grid-live.json，每 30 分鐘排程或按「更新目前倉位」發布。")}</dt>
      <dd><span class="dot${livePub.ok === false ? " is-warn" : ""}"></span>${pubLine(livePub)}</dd>
    </dl>
    <p class="live-bar" style="margin-top:14px"><button class="btn${syncWarn ? "" : " ghost"}" id="republish" ${pub.configured ? "" : "disabled"}>重新發布帳本</button>
      <span class="note">只更新帳本那一檔；失敗時每 30 分鐘排程也會自動重試。</span></p>
  </div>
  <div class="card">
    <h3>${tip("最近的歷史修正", "已記錄的日子因關倉補抓、人工補登、加倉／複投判定而重算時，列出前後差異。修正後的整份帳本會重新發布，Portfolio 拉取後自動更新。")}</h3>
    ${corrections.length ? `<table class="plain"><thead><tr><th>日期</th><th>項目</th><th>原本</th><th>修正後</th><th>原因</th><th>時間</th></tr></thead><tbody>
      ${corrections.map((c) => `<tr><td>${esc(c.capture_date)}</td><td>${esc(fieldLabel[c.field] || c.field)}</td>
        <td>${c.field.endsWith("_i") ? (c.old_value == null ? "—" : signed(c.old_value)) : esc(c.old_value ?? "—")}</td>
        <td>${c.field.endsWith("_i") ? (c.new_value == null ? "—" : signed(c.new_value)) : esc(c.new_value ?? "—")}</td>
        <td style="text-align:left">${esc(c.reason)}</td><td>${esc(fmtTaipei(c.corrected_at).slice(5, 16))}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">目前沒有歷史修正。</p>`}
  </div>`;
  const btn = document.getElementById("republish");
  if (btn) btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "發布中…";
    try {
      const result = await postJson("/api/v1/publish/ledger", {});
      showFlash(result.ok ? "帳本已重新發布到 Portfolio。" : publishText(result), result.ok ? "info" : "error");
    } catch (err) {
      showFlash("重新發布失敗：" + err.message, "error");
    }
    if (state.page === "status") renderStatus(state.renderSeq);
  };
}
