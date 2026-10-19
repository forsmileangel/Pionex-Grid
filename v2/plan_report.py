"""Render daily-pairing plans (data/plans-DATE.json + notes/plans-DATE.json) as one HTML file.

    python -m v2.plan_report --date 2026-10-08
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .healthcheck import OUT_DIR
from .healthcheck_report import CSS, esc, num, price, signed

ACTION = {"keep": ("保留", "ok"), "top_up": ("補保證金", "mid"), "close": ("關倉", "bad"), "close_later": ("贈品後關倉", "bad")}
EXTRA_CSS = """
.plan-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:6px 0 4px}
.plan-card{border:1px solid var(--line);border-radius:10px;padding:16px;background:#fcfbf8}
.plan-card.pick{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.plan-card h3{font-size:18px;margin:0 0 2px}
.plan-card .tl{font-size:12.5px;color:var(--accent);margin:0 0 10px}
.plan-card .big{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
.plan-card .big small{font-size:13px;color:var(--muted);font-weight:400;margin-left:6px}
.plan-card ul{margin:10px 0 0;padding-left:18px;font-size:13px;color:var(--ink-2)}
.act{display:inline-block;font-size:11.5px;font-weight:600;border-radius:4px;padding:0 7px}
.act.ok{background:#e6f1ea;color:var(--pos)}.act.mid{background:var(--accent-soft);color:var(--accent)}
.act.bad{background:#f7e4df;color:var(--neg)}
.compare .bar-row{grid-template-columns:150px 1fr 150px}
.band{fill:#efe6cf}.band-label{fill:var(--warn);font-size:11.5px}
.cmp-bar{fill:var(--accent)}.cmp-now{fill:#a7a29a}.cmp-label{fill:var(--ink-2);font-size:12.5px}.cmp-val{fill:var(--ink);font-size:12.5px;font-weight:600}
table.data td.reason{white-space:normal;min-width:220px;color:var(--ink-2);font-size:12.5px;text-align:left}
@media (max-width:760px){.plan-cards{grid-template-columns:1fr}}
"""


def pct(v, d=3) -> str:
    return "—" if v is None else f"{v:.{d}f}%"


def compare_chart(data: dict, capital: float) -> str:
    cur = data["current"]
    now_scaled = cur["daily"] / cur["capital"] * capital if cur["capital"] else 0
    rows = [("現況（放大到 20 萬 U）", now_scaled, "cmp-now")] + [
        (f"{n}方案", p["daily"], "cmp-bar") for n, p in data["plans"].items()]
    vmax = max(650, max(v for _, v, _ in rows) * 1.1)
    W, L, R, rowh, top = 820, 170, 70, 34, 26
    H = top + rowh * len(rows) + 22
    x = lambda v: L + v / vmax * (W - L - R)
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="各方案 20 萬 U 的預估每日配對">',
           f'<rect x="{x(300):.1f}" y="{top - 8}" width="{x(450) - x(300):.1f}" height="{rowh * len(rows) + 4}" class="band"/>',
           f'<text x="{(x(300) + x(450)) / 2:.1f}" y="{top - 12}" text-anchor="middle" class="band-label">目標 300～450 U</text>']
    for i, (label, v, cls) in enumerate(rows):
        y = top + i * rowh
        out.append(f'<text x="{L - 10}" y="{y + 17}" text-anchor="end" class="cmp-label">{esc(label)}</text>'
                   f'<rect x="{L}" y="{y + 5}" width="{max(x(v) - L, 2):.1f}" height="16" rx="3" class="{cls}"><title>{esc(label)}：每天約 {v:,.0f} U</title></rect>'
                   f'<text x="{x(v) + 8:.1f}" y="{y + 17}" class="cmp-val">{v:,.0f} U</text>')
    for t in (0, 200, 400, 600):
        out.append(f'<text x="{x(t):.1f}" y="{H - 4}" text-anchor="middle" class="axis" style="fill:var(--muted);font-size:11px">{t}</text>')
    out.append("</svg>")
    return "".join(out)


def plan_cards(data: dict, notes: dict) -> str:
    cards = []
    for name, p in data["plans"].items():
        n = notes.get("plans", {}).get(name, {})
        acts = {}
        for e in p["existing"]:
            acts[e["action"]] = acts.get(e["action"], 0) + 1
        worst = max(p["stress"], key=lambda s: s["amount"])
        cards.append(
            f'<div class="plan-card{" pick" if name == "平衡" else ""}"><h3>{esc(name)}</h3><p class="tl">{esc(n.get("tagline", ""))}</p>'
            f'<div class="big">{p["daily"]:,.0f} U<small>每天 · {p["rate_pct"]:.3f}%</small></div>'
            f'<ul><li>保留 {acts.get("keep", 0) + acts.get("top_up", 0)} 倉（其中補保證金 {acts.get("top_up", 0)} 倉）、關倉 {acts.get("close", 0) + acts.get("close_later", 0)} 倉</li>'
            f'<li>新開 {len(p["new"])} 倉，共 {p["new_capital"]:,.0f} U</li>'
            f'<li>最大產業組：{esc(p["groups"][0]["name"])} {p["groups"][0]["share"]:.0f}%</li>'
            + (f'<li>最壞情境：{esc(worst["name"])}，強平 {worst["amount"]:,.0f} U</li>' if worst["amount"]
               else '<li>表中五種壓力情境都沒有倉位強平</li>') + '</ul></div>')
    return f'<div class="plan-cards">{"".join(cards)}</div>'


def support_table(data: dict, picked: set[str]) -> str:
    want = {"GOOGLX", "TSLAX", "METAX", "LLYX", "BTC", "ETH", "AMDX", "SPCX", "AVGOX", "ONDSX", "MUX", "SNDKX"}
    rows = [r for r in data["support"][:24]] + [r for r in data["support"][24:] if r["base"] in want]
    body = []
    for r in rows:
        cells = []
        for t in ("0.15", "0.20", "0.25"):
            need = r["need"][t]
            cells.append(f'<td>{need["lev"]} 倍<small>強平 {need["liq_dist"]:.0f}%</small></td>' if need["lev"]
                         else '<td class="muted">6 倍也不到</td>')
        tag = "持有" if r["held"] else ("方案選用" if r["base"] in picked else "")
        body.append(f'<tr><td class="t"><b>{esc(r["base"])}</b><small>{esc(r["sector"])}{" · " + tag if tag else ""}</small></td>'
                    f'<td>{num(r["atr14_pct"], 1)}%</td><td><b>{pct(r["rate_1x"] * 3)}</b></td>{"".join(cells)}'
                    f'<td>{num(r["maxdd180_pct"])}%</td><td>{num(r["corr_book"], 2)}</td></tr>')
    return ('<div class="table-wrap"><table class="data compact"><thead><tr><th class="t">標的</th><th>日波動</th>'
            '<th>3 倍時每天</th><th>要到 0.15%</th><th>要到 0.20%</th><th>要到 0.25%</th><th>180 天最大回撤</th><th>與持倉相關</th>'
            f'</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
            '<p class="note">固定用區間「現價 −50%～+25%」、每格約 0.22% 試算；「每天」是價格在區間內時的配對效率（波動支撐力），'
            '用近 30 天和近 60 天的 15 分鐘 K 線重播網格、再乘上用你自己倉位校正的 1.2 倍。表中前 24 名依效率排序，後面是你持有或上次建議過的標的。</p>')


def existing_table(p: dict) -> str:
    body = "".join(
        f'<tr><td class="t"><b>{esc(e["symbol"])}</b><small>{e["lev"]:.0f} 倍 · {e["days"]} 天紀錄</small></td>'
        f'<td>{num(e["capital"])}</td><td>{pct(e["rate"])}</td><td>{num(e["liq_distance_pct"], 0)}%</td>'
        f'<td class="t"><span class="act {ACTION[e["action"]][1]}">{ACTION[e["action"]][0]}</span>'
        f'{f"<small>+{e["top_up"]:,.0f} U</small>" if e["top_up"] else ""}</td><td class="reason">{esc(e["reason"])}</td></tr>'
        for e in sorted(p["existing"], key=lambda e: ["top_up", "keep", "close_later", "close"].index(e["action"])))
    return ('<div class="table-wrap"><table class="data compact"><thead><tr><th class="t">現有倉位</th><th>投入＋追加 U</th>'
            '<th>近 30 天每天</th><th>強平距離</th><th class="t">處理</th><th class="t">理由</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def new_table(p: dict) -> str:
    body = []
    for a in p["new"]:
        c = a["cfg"]
        idle = (c["outside30"] + c["outside60"]) / 2 * 100
        body.append(
            f'<tr><td class="t"><b>{esc(a["base"])}</b><small>{esc(a["group"])}{" · 加碼" if a["held"] else ""}</small></td>'
            f'<td>{num(a["amount"])}</td><td>{c["lev"]} 倍</td>'
            f'<td>{price(c["bottom"])}～{price(c["top"])}<small>−{c["down"] * 100:.0f}%／+{c["up"] * 100:.0f}%</small></td>'
            f'<td>{c["rows"]}</td><td>{c["liq_dist"]:.0f}%</td>'
            f'<td><b>{a["daily"]:,.1f} U</b><small>{a["rate"]:.3f}%</small></td>'
            f'<td>{c["r30"]:.3f}%<small>60 天 {c["r60"]:.3f}%</small></td><td>{idle:.0f}%</td></tr>')
    return ('<div class="table-wrap"><table class="data compact"><thead><tr><th class="t">新開倉位</th><th>金額 U</th><th>槓桿</th>'
            '<th>區間</th><th>格數</th><th>強平距離</th><th>預估每天</th><th>近 30 天</th><th>閒置</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>'
            '<p class="note">區間以 2026-10-08 收盤價計算；開單時以當下價格重算相同的百分比。預估每天＝近 30 天與近 60 天重播的平均，'
            '已扣掉價格跑出區間的閒置時間；「閒置」是兩段期間價格在區間外的平均比例。</p>')


def stress_table(p: dict) -> str:
    rows = "".join(
        f'<tr><td class="t">{esc(s["name"])}</td><td class="t">{esc("、".join(s["symbols"])) or "沒有倉位強平"}</td>'
        f'<td>{num(s["amount"])}</td><td>{num(s["daily_lost"])}</td></tr>' for s in p["stress"])
    return ('<div class="table-wrap"><table class="data compact"><thead><tr><th class="t">情境</th><th class="t">會強平的倉位</th>'
            f'<th>強平的倉位 U</th><th>少掉的每日 U</th></tr></thead><tbody>{rows}</tbody></table></div>')


def group_bars(p: dict) -> str:
    rows = "".join(
        f'<div class="bar-row"><span class="bar-label">{esc(g["name"])}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{g["share"]:.1f}%"></span></span>'
        f'<span class="bar-val">{g["share"]:.0f}%<small>{num(g["amount"])} U</small></span></div>' for g in p["groups"])
    return f'<div class="bars">{rows}</div>'


def calib_tables(data: dict) -> str:
    c = data["calibration"]
    prof = "".join(f'<tr><td class="t">{esc(r["symbol"])}</td><td>{r["days"]}</td><td>{num(r["real"], 1)}</td>'
                   f'<td>{num(r["sim"] * c["factor"], 1)}</td><td>{(r["real"] / (r["sim"] * c["factor"]) - 1) * 100:+.0f}%</td></tr>'
                   for r in c["rows"])
    liq = "".join(f'<tr><td class="t">{esc(r["symbol"])}</td><td>{price(r["pionex"])}</td><td>{price(r["model"])}</td>'
                  f'<td>{r["diff_pct"]:+.1f}%</td></tr>' for r in c.get("liq_check", []))
    return ('<div class="two-col">'
            '<div><h3 style="margin:4px 0 6px">每日配對：模型 vs 實際（近 30 天合計 U）</h3><div class="table-wrap"><table class="data compact">'
            '<thead><tr><th class="t">倉位</th><th>天數</th><th>實際</th><th>模型</th><th>差</th></tr></thead>'
            f'<tbody>{prof}</tbody></table></div></div>'
            '<div><h3 style="margin:4px 0 6px">強平價：模型 vs 派網預估</h3><div class="table-wrap"><table class="data compact">'
            '<thead><tr><th class="t">倉位</th><th>派網</th><th>模型</th><th>差</th></tr></thead>'
            f'<tbody>{liq}</tbody></table></div></div></div>'
            f'<p class="note">模型重播 15 分鐘 K 線（每根依開→低→高→收或開→高→低→收走一遍），每格成交量用派網的算法（投入 × 槓桿 ÷ 各格價格總和），'
            f'持倉＝每格量 × 現價以上的格數。15 分鐘 K 棒內更細的來回抓不到，所以整組實際比模型多約 {(c["factor"] - 1) * 100:.0f}%，'
            '表中模型欄已乘上這個校正。強平價模型為保證金餘額跌到維持保證金（0.5%）時的價格，並計入價格往下時網格持續買進。</p>')


def render(data: dict, notes: dict) -> str:
    capital = data["capital"]
    cur = data["current"]
    picked = {a["base"] for p in data["plans"].values() for a in p["new"]}
    findings = "".join(f"<li>{esc(f)}</li>" for f in notes.get("findings", []))
    steps = "".join(f"<li>{esc(s)}</li>" for s in notes.get("first_steps", []))
    cautions = "".join(f"<li>{esc(s)}</li>" for s in notes.get("cautions", []))
    earnings = "".join(f'<tr><td>{esc(e["date"][5:].replace("-", "/"))}</td><td class="t"><b>{esc(e["symbol"])}</b></td>'
                       f'<td class="t">{esc(e["note"])}</td></tr>' for e in notes.get("earnings", []))
    sources = "".join(f'<li><a href="{esc(s["url"])}">{esc(s["label"])}</a></li>' for s in notes.get("sources", []))
    plan_sections = []
    for i, (name, p) in enumerate(data["plans"].items()):
        n = notes.get("plans", {}).get(name, {})
        plan_sections.append(
            f'<section id="plan-{i}"><h2>{esc(name)}方案：每天約 {p["daily"]:,.0f} U（{p["rate_pct"]:.3f}%）</h2>'
            f'<p class="callout">{esc(n.get("body", ""))}<br><small>規則：{esc(p["desc"])}</small></p>'
            f'{existing_table(p)}<h3 style="margin:20px 0 6px">新開倉位（依預估效率排序）</h3>{new_table(p)}'
            f'<h3 style="margin:20px 0 8px">產業組占比</h3>{group_bars(p)}'
            f'<h3 style="margin:20px 0 6px">壓力測試</h3>{stress_table(p)}</section>')
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(notes.get("title", "每日配對方案"))} {esc(data["date"])}</title>
<style>{CSS}{EXTRA_CSS}.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}@media (max-width:760px){{.two-col{{grid-template-columns:1fr}}}}</style></head>
<body><main>
<p class="meta">{esc(data["date"])} · 用近 60 天 15 分鐘 K 線試算</p>
<h1>{esc(notes.get("title", "每日配對方案"))}</h1>
<p class="lede">{esc(notes.get("subtitle", ""))}</p>

<section id="s-goal"><h2>目標換算</h2>
<div class="tiles">
<div class="tile"><span>你的目標</span><b>0.15%～0.225%</b><small>10 萬 U → 150 U；20 萬 U → 300～450 U（每天、每 U 投入）</small></div>
<div class="tile"><span>現況（近 30 天）</span><b>{cur["daily"]:,.0f} U／天</b><small>{cur["capital"]:,.0f} U 倉位 · {cur["daily"] / cur["capital"] * 100:.3f}%</small></div>
<div class="tile"><span>現況放大到 20 萬 U</span><b>{cur["daily"] / cur["capital"] * capital:,.0f} U／天</b><small>離 300 U 還差 {300 - cur["daily"] / cur["capital"] * capital:,.0f} U</small></div>
</div>
{compare_chart(data, capital)}
{plan_cards(data, notes)}
<p class="note">三個方案都以 20 萬 U 倉位為準，包含要保留的現有倉位與補的保證金；不包含建議另外保留的 15%～20% 補保證金預備金。</p></section>

<section id="s-find"><h2>主要發現</h2><ul class="summary">{findings}</ul>
<p class="callout" style="margin-top:14px"><b>建議：</b>{esc(notes.get("recommendation", ""))}</p></section>

<section id="s-steps"><h2>現在可以先做的事</h2><ol class="plan-list">{steps}</ol>
<h3 style="margin:18px 0 6px">方案標的的財報日</h3><div class="table-wrap"><table class="data compact narrow"><thead><tr><th>日期</th><th class="t">標的</th><th class="t">說明</th></tr></thead><tbody>{earnings}</tbody></table></div></section>

<section id="s-support"><h2>波動撐得起多少每日配對？</h2>{support_table(data, picked)}</section>

{"".join(plan_sections)}

<section id="s-model"><h2>模型怎麼算、準不準</h2>{calib_tables(data)}</section>

<section id="s-caution"><h2>注意事項</h2><ul class="summary">{cautions}</ul>
<ul class="sources">{sources}</ul>
<p class="disclaimer">本報告用派網公開行情與你的本機帳本計算，只讀、不改帳本、不發布到 Gist。過去 30～60 天的波動不代表未來；
加密貨幣與美股代幣都可能出現單日大幅跳空。所有數字僅供規劃參考，不是投資建議。</p></section>
</main></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render daily-pairing plans")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    data = json.loads((OUT_DIR / "data" / f"plans-{args.date}.json").read_text(encoding="utf-8"))
    notes_path = OUT_DIR / "notes" / f"plans-{args.date}.json"
    notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else {}
    out = OUT_DIR / f"{args.date}-每日配對方案.html"
    out.write_text(render(data, notes), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
