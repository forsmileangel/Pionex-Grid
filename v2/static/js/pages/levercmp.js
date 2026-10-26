import { app, bindHistoryClicks, esc, fmtTaipei, get, n, signed, tip } from "../common.js";

import { isCurrent } from "../router.js";

// 槓桿對照（試做）：另一套「等效倉位＝倉位×倍率」和這裡「投入＋追加保證金」並排比較。只讀最近一次每日快照。
let leverData = null;
const OURS_TIP = "你的等效倉位＝投入＋追加保證金，也就是你放在這個倉裡的保證金。調高槓桿、保證金留在倉內時不變；補追加保證金會變大。";
const THEIRS_TIP = "他的等效倉位＝當前倉位（quoteInvestment 換成 USDT）× 派網倍率，也就是槓桿放大後的規模。調高槓桿、保證金留在倉內時也不變；但補追加保證金不會讓它改變。";

function paintLeverCompare() {
  const data = leverData || {};
  const rows = data.rows || [];
  const t = data.totals;
  if (!rows.length || !t) {
    app.innerHTML = "<p class='muted'>還沒有每日快照。</p>";
    return;
  }
  const u = (v, d = 0) => (v == null ? "—" : n(v, d));
  const x = (v) => (v == null ? "—" : n(v, 2) + "×");
  const pct = (v, d = 1) => (v == null ? "—" : n(v, d) + "%");
  const stat = (label, note, main, sub) => `<div class="stat">${tip(label, note)}`
    + `<strong>${main}<span class="twd" style="white-space:normal">${sub}</span></strong></div>`;

  const coin = rows.find((r) => r.coin_margined && r.their_position != null
    && Math.abs(r.their_position - r.investment) > 0.01);
  const fundGap = rows.filter((r) => r.funding_net != null && r.funding_paid != null)
    .sort((a, b) => Math.abs(b.funding_net - b.funding_paid) - Math.abs(a.funding_net - a.funding_paid))[0];

  const body = rows.map((r) => {
    const diffRatio = r.ratio != null && r.our_leverage != null && Math.abs(r.ratio - r.our_leverage) >= 0.01;
    return `<tr class="clickable" data-id="${esc(r.bu_order_id || "")}">
      <td>${esc(r.symbol || "—")}<small>派網 ${esc(r.leverage || "—")}</small></td>
      <td>${u(r.investment)}${r.coin_margined && r.their_position != null && Math.abs(r.their_position - r.investment) > 0.01
        ? `<small>他 ${u(r.their_position)}（現價）</small>` : ""}</td>
      <td class="n">${u(r.extra_margin)}</td>
      <td class="n"><b>${u(r.ours)}</b><small>${pct(r.our_share)}</small></td>
      <td class="n"><b>${u(r.theirs)}</b><small>${pct(r.their_share)}</small></td>
      <td class="n">${x(r.our_leverage)}${diffRatio ? `<small>他÷你 ${x(r.ratio)}</small>` : ""}</td>
      <td class="n">${pct(r.our_eff)}</td>
      <td class="n">${pct(r.their_eff)}</td>
      <td class="n">${pct(r.liq_distance_pct, 2)}</td>
    </tr>`;
  }).join("");

  const foot = `<tr class="total">
      <td><b>合計</b><small>${t.count} 倉</small></td>
      <td>${u(t.investment)}${Math.abs(t.their_position - t.investment) > 0.01 ? `<small>他：${u(t.their_position)}</small>` : ""}</td>
      <td class="n">${u(t.extra_margin)}</td>
      <td class="n"><b>${u(t.ours)}</b></td>
      <td class="n"><b>${u(t.theirs)}</b></td>
      <td class="n">${x(t.our_leverage)}<small>他÷你 ${x(t.ratio)}</small></td>
      <td class="n">${pct(t.our_eff)}</td>
      <td class="n">${pct(t.their_eff)}</td>
      <td class="n">${pct(t.liq_ours, 2)}<small>他：${pct(t.liq_theirs, 2)}</small></td>
    </tr>`;

  const same = "<span class='up'>一樣</span>";
  const diff = "<span class='down'>不一樣</span>";
  const defs = [
    ["等效倉位", "當前倉位 × 倍率（槓桿後的規模）", "投入＋追加保證金（你放在倉裡的保證金）",
      `${diff}：他的 ＝ 你的 × 等效槓桿。合計 ${u(t.theirs)} 對 ${u(t.ours)} U`],
    ["佔比", "他的等效倉位 ÷ 合計", "你的等效倉位 ÷ 合計", `${diff}：倍率越高的倉，在他的表佔比越大`],
    ["當前倉位／倍率", "quoteInvestment 換成 USDT；派網倍率", "投入（派網顯示的倉位）；派網槓桿",
      `U 本位${same}。${coin ? `幣本位 ${esc(coin.symbol)} 他用現價、你用開倉價：${u(coin.their_position)} 對 ${u(coin.investment)} U` : "幣本位他用現價、你用開倉價"}`],
    ["等效槓桿", "沒有這欄，只看每倉倍率", "投入 × 倍率 ÷ 你的等效倉位", `你這邊才有。整體 ${x(t.our_leverage)}`],
    ["現價／強平價", "永續行情 close；有效的預估強平價", "永續行情；同樣的強平價規則", same],
    ["強平距離", "（現價 − 強平價）÷ 現價", "同一個公式", `每倉${same}；合計他用他的等效倉位加權（${pct(t.liq_theirs, 2)}），你用你的（${pct(t.liq_ours, 2)}）`],
    ["網格上下限距離", "（現價 − 下限）÷ 現價、（上限 − 現價）÷ 現價", "沒有這欄", "你這邊沒有"],
    ["可用網格／累積網格", "gridProfit − 提領 − 複投 − 減倉／gridProfit", "網格利潤＝gridProfit", "累積網格一樣；你這邊沒有「可用網格」"],
    ["單天配對", "本日最新 − 本日基準（08:00 日界線，盤中一直更新）", "08:15 快照和前一天相減，另外處理提領、複投、關倉", "概念一樣，記錄時間點不同"],
    ["資金費", "fundingFeePayment 的差額；沒資料當 0", "totalFundingFee 的差額（淨額，含收到的）；沒資料留空",
      `${diff}：fundingFeePayment 只算付出的。目前合計他 ${signed(t.funding_paid)}、淨額 ${signed(t.funding_net)}${fundGap
        ? `；差最多的是 ${esc(fundGap.symbol)}（${signed(fundGap.funding_paid)} 對 ${signed(fundGap.funding_net)}）` : ""}`],
    ["單天淨利", "單天配對＋資金費", "資金費試做頁的淨利", "算法一樣，資金費來源不同（見上一列）"],
    ["當前利潤／淨利", "保證金權益推算，再加回已轉入本金的獲利", "總損益＝趨勢盈虧＋網格利潤", `${diff}：是另一套算法`],
    ["當前利潤%／淨利%", "÷ 他的等效倉位", "沒有；年化用你的等效倉位", "分母不同"],
    ["配對效率（%/年）", "累積網格 ÷ 他的等效倉位 ÷ 天數 × 365", "網格年化＝網格利潤 ÷ 你的等效倉位 ÷ 天數 × 365",
      `${diff}：他的 ≈ 你的 ÷ 等效槓桿（合計 ${pct(t.their_eff)} 對 ${pct(t.our_eff)}）。你頁面上的「每U效率」分母也是投入×倍率，和他同一個基準`],
    ["運行時間", "現在 − 建立時間", "持有天數", same],
  ].map(([col, theirs, ours, verdict]) =>
    `<tr><td><b>${col}</b></td><td>${theirs}</td><td>${ours}</td><td>${verdict}</td></tr>`).join("");

  app.innerHTML = `
    <p class="muted">試做頁：把另一套「等效倉位＝倉位×倍率」和你的「等效倉位＝投入＋追加保證金」放在同一張表。資料是 ${esc(data.capture_date || "")} 每日快照（${data.as_of ? fmtTaipei(data.as_of) : "—"}），只讀不寫。</p>
    <div class="stats">
      ${stat("你的等效倉位", OURS_TIP, `${u(t.ours)}<span class="unit">USDT</span>`, `投入 ${u(t.investment)} ＋ 追加保證金 ${u(t.extra_margin)}`)}
      ${stat("他的等效倉位", THEIRS_TIP, `${u(t.theirs)}<span class="unit">USDT</span>`, `投入 × 倍率；是你的 ${n(t.ratio, 2)} 倍`)}
      ${stat("等效槓桿", "投入 × 派網倍率 加總 ÷ 你的等效倉位。他的表沒有整體槓桿，但「他的等效倉位 ÷ 你的」幾乎就是這個數；差一點是因為 ETH 幣本位他用現價換算。", `${n(t.our_leverage, 2)} 倍`, `他÷你 ${n(t.ratio, 2)}`)}
      ${stat("配對效率（年化）", "各倉網格利潤 ÷ 持有天數加總，× 365，再除以合計的等效倉位。分子一樣，只差分母。", `${pct(t.our_eff)} <span class="unit">你</span>`, `他的算法 ${pct(t.their_eff)}`)}
    </div>
    <div class="card">
      <div class="card-head"><h3>${tip("每倉對照", "點列可開單一網格頁。投入就是他的「當前倉位」；幣本位他用現價換算，所以會不同。")}</h3></div>
      <p class="note">${tip("你的等效倉位", OURS_TIP)}和${tip("他的等效倉位", THEIRS_TIP)}剛好差一個「等效槓桿」。兩邊在調高槓桿、保證金留在倉內時都不會跳；差別在補追加保證金時，你的會變大、他的不變。</p>
      <table class="plain wide lever-cmp"><thead><tr>
        <th>標的</th><th>投入<small>＝他的「當前倉位」</small></th><th>追加保證金</th>
        <th>${tip("你的等效倉位", OURS_TIP)}<small>佔比</small></th><th>${tip("他的等效倉位", THEIRS_TIP)}<small>佔比</small></th>
        <th>${tip("等效槓桿", "投入 × 派網倍率 ÷ 你的等效倉位。U 本位的倉，這個數字就等於「他的 ÷ 你的」。")}</th>
        <th>${tip("配對效率 你", "網格利潤 ÷ 你的等效倉位 ÷ 持有天數 × 365。")}<small>%/年</small></th>
        <th>${tip("配對效率 他", "網格利潤 ÷ 他的等效倉位 ÷ 持有天數 × 365。")}<small>%/年</small></th>
        <th>${tip("強平距離", "（現價 − 強平價）÷ 現價，兩邊每倉都一樣。合計是加權平均：你的用你的等效倉位加權，他用他的。")}</th>
      </tr></thead><tbody>${body}${foot}</tbody></table>
    </div>
    <div class="card">
      <div class="card-head"><h3>${tip("欄位算法對照", "他的每個欄位，對到你這邊的哪一個、算法一不一樣。")}</h3></div>
      <table class="plain wide lever-defs"><thead><tr><th>他的欄位</th><th>他的算法</th><th>你這邊</th><th>比較</th></tr></thead>
      <tbody>${defs}</tbody></table>
    </div>`;
  bindHistoryClicks();
}

export async function renderLeverCompare(seq) {
  const data = await get("/api/v1/leverage-compare");
  if (!isCurrent(seq, "levercmp")) return;
  leverData = data;
  paintLeverCompare();
}
