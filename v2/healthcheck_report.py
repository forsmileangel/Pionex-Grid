"""Render the health-check data + notes into one self-contained, print-friendly HTML file."""

from __future__ import annotations

import html
import statistics
from datetime import datetime, timedelta

LEVEL = {"high": ("優先", "lv-high"), "mid": ("建議", "lv-mid"), "low": ("之後", "lv-low")}
REASON = {"profit_stop": "止盈", "loss_stop": "止損", "user_cancel": "手動關倉"}
RAMP = {"保守": "#8fbcc1", "持平": "#4f8f96", "樂觀": "#1f5459"}


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def num(v, d=0) -> str:
    if v is None:
        return "—"
    return f"{v:,.{d}f}"


def signed(v, d=0) -> str:
    if v is None:
        return "—"
    return ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v):,.{d}f}"


def price(v) -> str:
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:,.2f}"
    if v >= 0.1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def tone(v) -> str:
    return "" if not v else ("pos" if v > 0 else "neg")


def taipei(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(iso)[:16]


# ------------------------------------------------------------------ charts

def range_bar(r: dict) -> str:
    """Grid range as a track: dot = price, red tick = estimated liquidation."""
    lo, hi, mark, liq = r.get("bottom"), r.get("top"), r.get("mark"), r.get("liq")
    if not (lo and hi and mark and hi > lo):
        return "—"
    w, x0, x1 = 120, 6, 114
    x = lambda v: x0 + (min(max(v, lo), hi) - lo) / (hi - lo) * (x1 - x0)
    parts = [f'<svg class="rbar" width="{w}" height="18" viewBox="0 0 {w} 18" role="img" '
             f'aria-label="區間 {price(lo)}～{price(hi)}，現價 {price(mark)}">',
             f'<rect x="{x0}" y="7" width="{x1 - x0}" height="4" rx="2" class="rb-track"/>']
    if liq:
        lx = x(liq)
        outside = liq < lo
        parts.append(f'<g><title>預估強平 {price(liq)}{"（在區間下限之外）" if outside else ""}</title>'
                     f'<rect x="{lx - 1:.1f}" y="2" width="2" height="14" rx="1" class="rb-liq"/></g>')
    parts.append(f'<g><title>現價 {price(mark)}（區間 {price(lo)}～{price(hi)}）</title>'
                 f'<circle cx="{x(mark):.1f}" cy="9" r="4.5" class="rb-dot"/></g></svg>')
    return "".join(parts)


def goal_chart(data: dict) -> str:
    g = data["goal"]
    hist = data["positions"]["ledger"]["wallet_history"]
    start = datetime.fromisoformat(data["positions"]["as_of"]).date() if data["positions"].get("as_of") else None
    if not hist or not start:
        return ""
    d0 = datetime.fromisoformat(hist[0]["date"]).date()
    d_end = start + timedelta(days=360)
    targets = g["targets"]
    ymax = max([t["usdt"] for t in targets] + [max(p) for p in g["paths"].values()]) * 1.06
    ymin = min(h["usdt"] for h in hist) * 0.9
    W, H, L, R, T, B = 820, 280, 58, 120, 18, 30
    sx = lambda d: L + (d - d0).days / max((d_end - d0).days, 1) * (W - L - R)
    sy = lambda v: T + (1 - (v - ymin) / (ymax - ymin)) * (H - T - B)
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="錢包實際走勢與只靠網格利潤的預估">']
    step = 50_000
    tick = (int(ymin // step) + 1) * step
    while tick < ymax:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{sy(tick):.1f}" y2="{sy(tick):.1f}" class="grid"/>'
                   f'<text x="{L - 8}" y="{sy(tick) + 4:.1f}" class="axis" text-anchor="end">{tick // 1000:,}k</text>')
        tick += step
    for m in range(0, 13, 3):
        d = start + timedelta(days=30 * m)
        label = "現在" if m == 0 else f"+{m} 個月"
        out.append(f'<text x="{sx(d):.1f}" y="{H - 8}" class="axis" text-anchor="middle">{label}</text>')
    for t in targets:
        y = sy(t["usdt"])
        td = start + timedelta(days=30 * t["months"])
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" class="target"/>'
                   f'<circle cx="{sx(td):.1f}" cy="{y:.1f}" r="4" class="target-dot"><title>{esc(t["label"])}：{num(t["usdt"])} U</title></circle>'
                   f'<text x="{W - R + 8}" y="{y + 4:.1f}" class="label">{"半年目標" if t["months"] == 6 else "一年目標"} {t["usdt"] / 1000:,.0f}k</text>')
    pts = " ".join(f'{sx(datetime.fromisoformat(h["date"]).date()):.1f},{sy(h["usdt"]):.1f}' for h in hist)
    out.append(f'<polyline points="{pts}" class="actual"><title>實際錢包（每日 08:15 快照）</title></polyline>')
    label_y = []
    for name, path in g["paths"].items():
        coords = [(start, g["wallet"])] + [(start + timedelta(days=30 * (i + 1)), v) for i, v in enumerate(path)]
        pts = " ".join(f"{sx(d):.1f},{sy(v):.1f}" for d, v in coords)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{RAMP[name]}" stroke-width="2" stroke-linejoin="round">'
                   f'<title>只靠網格利潤（{name}）：一年後約 {num(path[-1])} U</title></polyline>')
        y = sy(path[-1])
        while any(abs(y - u) < 13 for u in label_y):
            y -= 13
        label_y.append(y)
        out.append(f'<text x="{W - R + 8}" y="{y + 4:.1f}" class="label">{name} {path[-1] / 1000:,.0f}k</text>')
    out.append("</svg>")
    legend = ('<div class="legend"><span><i class="sw sw-actual"></i>實際錢包</span>'
              + "".join(f'<span><i class="sw" style="background:{c}"></i>只靠利潤・{n}</span>' for n, c in RAMP.items())
              + '<span><i class="sw sw-target"></i>目標</span></div>')
    return legend + "".join(out)


def sector_bars(groups: list[dict]) -> str:
    top = max(g["share_pct"] for g in groups) if groups else 1
    rows = "".join(
        f'<div class="bar-row"><span class="bar-label">{esc(g["name"])}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{g["share_pct"] / top * 100:.1f}%"></span></span>'
        f'<span class="bar-val">{g["share_pct"]:.1f}%<small>{num(g["investment"])} U · {g["bots"]} 倉</small></span></div>'
        for g in groups)
    return f'<div class="bars">{rows}</div>'


# ------------------------------------------------------------------ sections

def flags(r: dict, median_eff: float | None) -> list[str]:
    out = []
    if r.get("liq_distance_pct") is not None and r["liq_distance_pct"] < 35:
        out.append("強平近")
    if r.get("range_pos_pct") is not None and r["range_pos_pct"] > 85:
        out.append("近上緣")
    if r.get("range_pos_pct") is not None and r["range_pos_pct"] < 25:
        out.append("近下緣")
    if r.get("cost_drag_pct") is not None and r["cost_drag_pct"] > 45:
        out.append("成本高")
    if median_eff and r.get("eff30") is not None and r["eff30"] < median_eff * 0.5:
        out.append("效率低")
    return out


def positions_table(data: dict) -> str:
    rows = sorted(data["positions"]["rows"], key=lambda r: -r["investment"])
    effs = [r["eff30"] for r in rows if r.get("eff30") is not None]
    med = statistics.median(effs) if effs else None
    body = []
    for r in rows:
        f = flags(r, med)
        chips = "".join(f'<span class="chip">{esc(x)}</span>' for x in f)
        body.append(
            f'<tr><td class="t"><b>{esc(r["symbol"])}{"（幣本位）" if "/" not in r["symbol"] else ""}</b><small>{esc(r["leverage"])} · {esc(str(r.get("opened_at") or "")[:10])}</small><div class="flags">{chips}</div></td>'
            f'<td>{num(r["investment"])}</td>'
            f'<td class="rb">{range_bar(r)}<small>{num(r.get("range_pos_pct"))}% · 距上限 {num(r.get("to_top_pct"), 1)}%</small></td>'
            f'<td>{num(r.get("liq_distance_pct"), 1)}%</td>'
            f'<td>{num(r.get("eff_leverage"), 2)}×</td>'
            f'<td>{num(r.get("eff30"), 3)}%<small>14 天 {num(r.get("eff14"), 3)}%</small></td>'
            f'<td>{num(r.get("cost_drag_pct"))}%</td>'
            f'<td class="{tone(r.get("trend_profit"))}">{signed(r.get("trend_profit"))}<small>網格 {num(r["grid_profit"])}</small></td></tr>')
    return (
        '<div class="legend"><span><i class="sw-dot"></i>現價</span><span><i class="sw-liq"></i>派網預估強平價</span>'
        '<span>灰條＝網格區間（左下限、右上限）</span></div>'
        '<div class="table-wrap"><table class="data"><thead><tr>'
        '<th class="t">標的</th><th>投入 U</th><th class="rb">區間位置</th><th>強平距離</th><th>實際槓桿</th>'
        '<th>30 天效率</th><th>成本拖累</th><th>趨勢盈虧</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
        f'<p class="note">效率＝每天網格利潤 ÷ 投入 × 3 ÷ 槓桿（換算成 3 倍比較），和儀表板一致；本次中位數 {num(med, 3)}%。'
        '成本拖累＝（資金費支出＋手續費）÷ 網格利潤。實際槓桿＝目前持倉名義價值 ÷ 倉內保證金，價格往下走時會變大。'
        '提醒門檻：強平距離 &lt; 35%、區間位置 &gt; 85% 或 &lt; 25%、成本拖累 &gt; 45%、效率不到中位數一半。</p>')


def stress_table(data: dict) -> str:
    rows = "".join(
        f'<tr><td>全部跌 {s["drop_pct"]}%</td><td class="t">{esc("、".join(s["bots"])) or "沒有倉位強平"}</td>'
        f'<td>{num(s["equity_at_risk"])}</td></tr>' for s in data["portfolio"]["stress"])
    return ('<div class="table-wrap"><table class="data compact"><thead><tr><th class="t">情境</th>'
            '<th class="t">會被強平的倉位</th><th>倉內保證金 U</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            '<p class="note">依每倉派網預估強平價推算；預估強平價已考慮價格往下時網格陸續買進。美股代幣在週末和盤後也會交易，'
            '開盤時可能直接跳過強平價，所以實際風險比表上更集中。</p>')


def goal_section(data: dict) -> str:
    g = data["goal"]
    fx = data["positions"].get("fx_usdt_twd") or 31.4
    rows = []
    for t in g["targets"]:
        cells = "".join(
            f'<td>{num(v["profit_only"])}<small>每月需入金 {num(v["monthly_deposit"])} U ≈ {num(v["monthly_deposit"] * fx / 10000, 1)} 萬台幣</small></td>'
            for v in t["by_scenario"].values())
        rows.append(f'<tr><td class="t"><b>{esc(t["label"])}</b><small>{num(t["usdt"])} U</small></td>{cells}</tr>')
    heads = "".join(f"<th>{esc(n)}</th>" for n in g["paths"])
    return (
        f'<div class="tiles">'
        f'<div class="tile"><span>目前錢包</span><b>{num(g["wallet"])} U</b><small>約 {num(g["wallet"] * fx / 10000, 0)} 萬台幣</small></div>'
        f'<div class="tile"><span>近 30 天日均網格利潤</span><b>{num(g["avg_daily"], 1)} U</b><small>約 {num(g["avg_daily"] * 30)} U／月</small></div>'
        f'<div class="tile"><span>投入資金每天報酬</span><b>{num(g["daily_rate_pct"], 3)}%</b><small>錢包 {num(g["invested_ratio"] * 100)}% 在網格裡</small></div>'
        f'</div>{goal_chart(data)}'
        f'<div class="table-wrap"><table class="data compact"><thead><tr><th class="t">目標</th>{heads}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        '<p class="note">表中大字是「只靠網格利潤、不入金」到期時的錢包；小字是要準時達標每月需要的入金。'
        '情境：保守＝近 30 天效率的 0.8 倍、持平＝1 倍、樂觀＝1.3 倍；假設入金後一樣有約七成放進網格，利潤繼續複利。'
        '屯幣寶未計入（API 讀不到）。</p>')


def candidate_cards(data: dict, notes: dict) -> str:
    by = {m["base"]: m for m in data.get("candidates", [])}
    cards = []
    for c in notes.get("candidates", []):
        m = by.get(c["base"])
        if not m:
            continue
        s = m["suggest"]
        cards.append(
            f'<article class="cand"><header><h3>{esc(c["base"])}<span>{esc(c["name"])}</span></h3>'
            f'<span class="tag">{esc(m["class"])} · {esc(m["sector"])}</span></header>'
            f'<p>{esc(c["why"])}</p>'
            '<dl class="facts">'
            f'<div><dt>現價</dt><dd>{price(m["last"])}</dd></div>'
            f'<div><dt>日波動</dt><dd>{num(m["atr14_pct"], 1)}%</dd></div>'
            f'<div><dt>來回度</dt><dd>{num((1 - m["er90"]) * 100)}%</dd></div>'
            f'<div><dt>180 天最大回撤</dt><dd>{num(m["maxdd180_pct"])}%</dd></div>'
            f'<div><dt>180 天區間位置</dt><dd>{num(m["pos180_pct"])}%</dd></div>'
            f'<div><dt>與持倉相關性</dt><dd>{num(m["corr_book"], 2)}</dd></div>'
            '</dl>'
            '<dl class="facts plan">'
            f'<div><dt>建議槓桿</dt><dd>{s["leverage"]} 倍</dd></div>'
            f'<div><dt>建議區間</dt><dd>{price(s["bottom"])}～{price(s["top"])}</dd></div>'
            f'<div><dt>格數（每格）</dt><dd>{s["rows"]} 格（{num(s["step_pct"], 2)}%）</dd></div>'
            f'<div><dt>強平距離至少</dt><dd>{num(s["liq_target_pct"])}%</dd></div>'
            f'<div><dt>建議金額</dt><dd>{num(c.get("capital"))} U</dd></div>'
            '</dl>'
            f'<p class="risk"><b class="when">時機</b>{esc(c.get("timing"))}</p>'
            f'<p class="risk"><b>風險</b>{esc(c["risk"])}</p></article>')
    return "".join(cards)


def screen_table(data: dict, notes: dict) -> str:
    picked = {c["base"] for c in notes.get("candidates", [])}
    rows = "".join(
        f'<tr{" class=picked" if m["base"] in picked else ""}><td class="t"><b>{esc(m["base"])}</b><small>{esc(m["sector"])}</small></td>'
        f'<td>{num(m["grid_score"], 1)}</td><td>{num(m["atr14_pct"], 1)}%</td><td>{num((1 - m["er90"]) * 100)}%</td>'
        f'<td>{num(m["maxdd180_pct"])}%</td><td>{num(m["pos180_pct"])}%</td><td>{num(m["corr_book"], 2)}</td>'
        f'<td>{num(m["amount24h"] / 1e6, 1)}M</td></tr>'
        for m in data.get("candidates", [])[:30])
    return ('<details><summary>看完整篩選表（網格分數前 30 名，粗體列為本次建議）</summary>'
            '<div class="table-wrap"><table class="data compact"><thead><tr><th class="t">標的</th><th>網格分數</th>'
            '<th>日波動</th><th>來回度</th><th>最大回撤</th><th>區間位置</th><th>相關性</th><th>24h 成交</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            '<p class="note">網格分數＝近 90 天平均日振幅 × 來回度。來回度＝1 −（90 天淨漲跌 ÷ 90 天每日漲跌絕對值總和），'
            '越高代表越常來回、越不單邊。拿你現有倉位驗證：分數高的 SNDKX、SUI、ADA 實際效率也高，分數低的 AVGOX、ETH 效率也低。</p></details>')


def earnings_table(notes: dict) -> str:
    rows = "".join(
        f'<tr{" class=held" if e.get("held") else ""}><td>{esc(e["date"][5:].replace("-", "/"))}</td>'
        f'<td class="t"><b>{esc(e["symbol"])}</b></td><td class="t">{"持有" if e.get("held") else "候選"}</td>'
        f'<td class="t">{esc(e["note"])}</td></tr>' for e in notes.get("earnings", []))
    return ('<div class="table-wrap"><table class="data compact"><thead><tr><th>日期</th><th class="t">標的</th>'
            f'<th class="t">類型</th><th class="t">說明</th></tr></thead><tbody>{rows}</tbody></table></div>')


def wallet_table(data: dict) -> str:
    cats = [c for c in data.get("wallet_categories", []) if c.get("usdt")]
    rows = "".join(f'<tr><td class="t">{esc(c["name"])}</td><td>{num(c["usdt"])}</td></tr>' for c in cats)
    rows += '<tr><td class="t">屯幣寶</td><td class="muted">API 讀不到，請提供金額</td></tr>'
    return ('<div class="table-wrap"><table class="data compact narrow"><thead><tr><th class="t">資金位置</th><th>USDT</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


CSS = """
:root{--bg:#f7f5f0;--paper:#fff;--ink:#22252a;--ink-2:#4a4f57;--muted:#7b8087;--line:#e5e1d8;--line-2:#d6d1c6;
--accent:#2e6970;--accent-soft:#eaf1f1;--pos:#2f6f4a;--neg:#a43d2c;--warn:#8a5a14;--warn-soft:#f6ecd9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.75 "Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:900px;margin:0 auto;padding:48px 20px 72px}
h1,h2,h3{font-family:"Noto Serif TC","Source Han Serif TC","Songti TC","PMingLiU",serif;font-weight:600;color:var(--ink);letter-spacing:.01em}
h1{font-size:30px;margin:0 0 4px}
h2{font-size:21px;margin:0 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line-2)}
h3{font-size:17px;margin:0}
.lede{color:var(--ink-2);margin:0 0 28px}
.meta{color:var(--muted);font-size:13px}
section{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:26px 28px;margin:20px 0}
.summary li{margin:6px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:4px 0 18px}
.tile{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:#fcfbf8}
.tile span{display:block;color:var(--muted);font-size:12.5px}
.tile b{display:block;font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
.tile small{color:var(--muted);font-size:12px}
.actions{list-style:none;padding:0;margin:0}
.actions li{display:grid;grid-template-columns:52px 1fr;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
.actions li:first-child{border-top:0}
.actions b{display:block;margin-bottom:2px}
.actions p{margin:0;color:var(--ink-2);font-size:14px}
.lv{font-size:12px;font-weight:600;text-align:center;border-radius:4px;padding:2px 0;height:fit-content}
.lv-high{background:var(--warn-soft);color:var(--warn)}
.lv-mid{background:var(--accent-soft);color:var(--accent)}
.lv-low{background:#efede8;color:var(--ink-2)}
.table-wrap{overflow-x:auto;margin:6px 0}
table.data{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
table.data th{font-weight:600;color:var(--muted);font-size:12px;text-align:right;padding:8px 10px;border-bottom:1px solid var(--line-2);white-space:nowrap}
table.data td{text-align:right;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
table.data .t{text-align:left}
table.data td small{display:block;color:var(--muted);font-size:11.5px;line-height:1.5}
table.data td.t b{font-weight:600}
table.data.compact td{padding:7px 10px}
table.data.narrow{max-width:420px}
tr.held td{background:#fbf7ee}
tr.picked td{font-weight:600}
td.rb,th.rb{text-align:left}
.pos{color:var(--pos)}.neg{color:var(--neg)}.muted{color:var(--muted)}
.chip{display:inline-block;font-size:11.5px;color:var(--warn);background:var(--warn-soft);border-radius:4px;padding:0 6px;margin:0 4px 3px 0}
.ok{font-size:12px;color:var(--muted)}
.note{color:var(--muted);font-size:12.5px;line-height:1.7;margin:10px 0 0}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12.5px;color:var(--ink-2);margin:0 0 8px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{display:inline-block;width:16px;height:3px;border-radius:2px}
.sw-actual{background:var(--ink)}.sw-target{border-top:2px dashed #a7a29a;height:0}
.sw-dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--accent)}
.sw-liq{display:inline-block;width:3px;height:12px;border-radius:1px;background:var(--neg)}
.rbar{display:block}.rb-track{fill:#e7e3db}.rb-dot{fill:var(--accent);stroke:#fff;stroke-width:2}.rb-liq{fill:var(--neg)}
.chart{width:100%;height:auto;display:block;margin:4px 0 6px}
.chart .grid{stroke:#ece8e0;stroke-width:1}.chart .axis{fill:var(--muted);font-size:11px}
.chart .label{fill:var(--ink-2);font-size:11.5px}.chart .target{stroke:#a7a29a;stroke-width:1.5;stroke-dasharray:5 4}
.chart .target-dot{fill:#fff;stroke:#8b867e;stroke-width:2}.chart .actual{fill:none;stroke:var(--ink);stroke-width:2.2;stroke-linejoin:round}
.bars{display:grid;gap:8px;margin:4px 0}
.bar-row{display:grid;grid-template-columns:120px 1fr 150px;gap:12px;align-items:center;font-size:13.5px}
.bar-label{color:var(--ink-2)}
.bar-track{height:10px;background:#f0ede6;border-radius:5px;overflow:hidden}
.bar-fill{display:block;height:100%;background:var(--accent);border-radius:0 4px 4px 0}
.bar-val{font-variant-numeric:tabular-nums}.bar-val small{color:var(--muted);margin-left:8px;font-size:12px}
.cands{display:grid;gap:16px}
.cand{border:1px solid var(--line);border-radius:10px;padding:18px 20px;background:#fcfbf8}
.cand header{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.cand h3 span{font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;font-weight:400;font-size:13.5px;color:var(--muted);margin-left:10px}
.tag{font-size:12px;color:var(--accent);background:var(--accent-soft);border-radius:4px;padding:1px 8px}
.cand p{margin:6px 0 10px;color:var(--ink-2);font-size:14px}
.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 16px;margin:0 0 10px;padding:10px 0;border-top:1px solid var(--line)}
.facts.plan{border-top:1px dashed var(--line-2)}
.facts dt{font-size:11.5px;color:var(--muted)}.facts dd{margin:0;font-variant-numeric:tabular-nums;font-size:14px}
.risk{font-size:13.5px !important;margin:4px 0 !important}.risk b{color:var(--warn);margin-right:8px;font-weight:600}.risk b.when{color:var(--accent)}
.flags{white-space:normal;margin-top:3px;line-height:1.4}.flags:empty{display:none}
.avoid li,.plan-list li{margin:6px 0}
details summary{cursor:pointer;color:var(--accent);font-size:14px;margin-top:16px}
.callout{background:var(--accent-soft);border-radius:8px;padding:12px 16px;font-size:14px;color:var(--ink-2);margin:0 0 16px}
.sources{font-size:13px;color:var(--ink-2);padding-left:18px}.sources a{color:var(--accent)}
.disclaimer{font-size:12.5px;color:var(--muted);border-top:1px solid var(--line-2);padding-top:14px;margin-top:18px}
@media (max-width:640px){main{padding:28px 12px 48px}section{padding:20px 16px}h1{font-size:25px}
.bar-row{grid-template-columns:90px 1fr;}.bar-val{grid-column:2}.actions li{grid-template-columns:44px 1fr}}
@media print{body{background:#fff;font-size:13px}main{max-width:none;padding:0}section{border:0;padding:12px 0;break-inside:avoid-page}
details{display:block}details>summary{display:none}.cand{break-inside:avoid}}
"""


def render(data: dict, notes: dict) -> str:
    pos = data["positions"]
    fx = pos.get("fx_usdt_twd") or 31.4
    title = notes.get("title", "派網網格健檢")
    actions = "".join(
        f'<li><span class="lv {LEVEL.get(a["level"], LEVEL["low"])[1]}">{LEVEL.get(a["level"], LEVEL["low"])[0]}</span>'
        f'<div><b>{esc(a["title"])}</b><p>{esc(a["body"])}</p></div></li>' for a in notes.get("actions", []))
    summary = "".join(f"<li>{esc(s)}</li>" for s in notes.get("summary", []))
    avoid = "".join(f'<li><b>{esc(a["what"])}</b>：{esc(a["why"])}</li>' for a in notes.get("avoid", []))
    plan = "".join(f"<li>{esc(p)}</li>" for p in notes.get("plan", []))
    sources = "".join(f'<li><a href="{esc(s["url"])}">{esc(s["label"])}</a></li>' for s in notes.get("sources", []))
    classes = "、".join(f'{esc(g["name"])} {g["share_pct"]:.0f}%' for g in data["portfolio"]["by_class"])
    under = "、".join(f'{esc(g["name"])} ×{g["bots"]} {g["share_pct"]:.0f}%'
                      for g in data["portfolio"]["by_underlying"] if g["bots"] > 1)
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} {esc(data["date"])}</title><style>{CSS}</style></head>
<body><main>
<p class="meta">{esc(data["date"])} · 資料時間 {esc(taipei(pos.get("as_of")))}（台北）</p>
<h1>{esc(title)}</h1>
<p class="lede">{esc(notes.get("subtitle", ""))}</p>

<section id="s-summary"><h2>一頁摘要</h2>
<div class="tiles">
<div class="tile"><span>錢包總額</span><b>{num(pos.get("wallet_usdt"))} U</b><small>約 {num((pos.get("wallet_usdt") or 0) * fx / 10000)} 萬台幣（1 U ≈ {num(fx, 2)}）</small></div>
<div class="tile"><span>網格投入（含追加保證金）</span><b>{num(pos["invested"])} U</b><small>{len(pos["rows"])} 個倉位，全部做多</small></div>
<div class="tile"><span>運行中配對利潤</span><b>{num(pos["grid_total"])} U</b><small>過去 24 小時 {signed(pos["profit_24h"])} U</small></div>
</div>
<ul class="summary">{summary}</ul></section>

<section id="s-actions"><h2>先做這幾件事</h2><ol class="actions">{actions}</ol></section>

<section id="s-goal"><h2>目標進度</h2>{goal_section(data)}</section>

<section id="s-health"><h2>倉位健檢</h2>{positions_table(data)}
<p class="note">{esc(notes.get("spcx_note", ""))}</p></section>

<section id="s-risk"><h2>集中度與壓力測試</h2>
<p class="callout">資產類別：{classes}。同標的多倉：{under or "無"}。</p>
<h3 style="margin:4px 0 10px">依產業（占網格投入）</h3>{sector_bars(data["portfolio"]["by_sector"])}
<h3 style="margin:22px 0 6px">如果全部一起跌</h3>{stress_table(data)}</section>

<section id="s-earn"><h2>財報日曆（未來 10 週）</h2>{earnings_table(notes)}
<p class="note">財報常有 5%～15% 的跳空。持有的倉位在財報前檢查強平距離和區間上緣；候選標的建議財報後再開。</p></section>

<section id="s-new"><h2>新標的建議（只做多）</h2>
<p class="callout">{esc(notes.get("leverage_note", ""))}</p>
<div class="cands">{candidate_cards(data, notes)}</div>
<h3 style="margin:24px 0 6px">暫時不建議</h3><ul class="avoid">{avoid}</ul>
{screen_table(data, notes)}</section>

<section id="s-plan"><h2>建議的執行順序</h2><ol class="plan-list">{plan}</ol>
<h3 style="margin:22px 0 6px">資金在哪裡</h3>{wallet_table(data)}</section>

<section id="s-method"><h2>方法與資料來源</h2>
<p class="note" style="margin-top:0">倉位資料來自本機 SQLite 帳本與每 30 分鐘的即時快照（只讀，沒有改動帳本，也沒有發布到 Gist）。市場資料來自派網公開行情 API：
可交易的 USDT 永續合約、24 小時成交量、近 200 天日 K；美股代幣 24 小時成交量至少 100 萬 U、加密與商品至少 300 萬 U 才列入；排除槓桿／反向 ETF 和上市前合成合約。
建議區間：下限取「180 天低點的九成」和「現價的六成」較低者，上限取「180 天高點的 1.05 倍」和「現價的 1.35 倍」較高者；每格約為日波動的十分之一（0.2%～0.5%）。</p>
<ul class="sources">{sources}</ul>
<p class="disclaimer">這份報告是用你的帳本和公開行情做的資料分析，不是投資建議。網格在單邊下跌時會持續買進、虧損放大，槓桿會讓強平提早發生；
過去的波動和效率不代表未來。開任何倉位前，請以派網開單畫面顯示的預估強平價為準，並只投入你能承受損失的金額。</p>
</section>
</main></body></html>
"""
