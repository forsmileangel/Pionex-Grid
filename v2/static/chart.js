/* Dashboard charts: dependency-free SVG so the page keeps working offline.
   Colours come from the :root tokens in index.html (--s1..--s20, --heat-pos/--heat-neg). */
(function (global) {
  "use strict";

  const TZ_OFFSET = 8 * 3600 * 1000; // Asia/Taipei, no DST
  const PALETTE = 20;
  const DOW = "日一二三四五六";

  const resizer = typeof ResizeObserver === "function" ? new ResizeObserver((entries) => {
    entries.forEach((entry) => {
      const el = entry.target;
      if (!el.isConnected) { resizer.unobserve(el); return; }
      const w = Math.floor(entry.contentRect.width);
      if (el._chartDraw && w && w !== el._chartWidth) el._chartDraw();
    });
  }) : null;

  function mount(el, draw) {
    el._chartDraw = draw;
    if (resizer && !el._chartObserved) { resizer.observe(el); el._chartObserved = true; }
    draw();
  }

  function token(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function color(i) {
    const k = (((Number(i) || 0) % PALETTE) + PALETTE) % PALETTE + 1;
    return token(`--s${k}`, "#2e6970");
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function parts(ms) {
    const d = new Date(ms + TZ_OFFSET);
    return { y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(),
             h: d.getUTCHours(), mi: d.getUTCMinutes(), dow: d.getUTCDay() };
  }
  const pad = (v) => String(v).padStart(2, "0");

  function fmtTime(ms, g) {
    const p = parts(ms);
    if (g === "1M") return `${p.y}/${pad(p.mo)}`;
    if (g === "1d" || g === "1w") return `${p.y}/${pad(p.mo)}/${pad(p.d)}（${DOW[p.dow]}）`;
    return `${pad(p.mo)}/${pad(p.d)} ${pad(p.h)}:${pad(p.mi)}`;
  }

  // Bucket label for tooltips: "10/03 14:00–15:00", "2026/10/03（六）", "09/28–10/04", "2026/10".
  function fmtRange(ms, hours, g) {
    const a = parts(ms);
    if (g === "1M") return `${a.y}/${pad(a.mo)}`;
    if (g === "1d") return fmtTime(ms, "1d");
    const end = ms + hours * 3600 * 1000;
    if (g === "1w") {
      const b = parts(end - 86400000);
      return `${a.y}/${pad(a.mo)}/${pad(a.d)}–${pad(b.mo)}/${pad(b.d)}`;
    }
    const b = parts(end);
    return `${pad(a.mo)}/${pad(a.d)} ${pad(a.h)}:${pad(a.mi)}–${pad(b.h)}:${pad(b.mi)}`;
  }

  function niceScale(lo, hi, count) {
    if (lo === hi) { const d = Math.abs(lo) * 0.1 || 1; lo -= d; hi += d; }
    const raw = (hi - lo) / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const min = Math.floor(lo / step + 1e-9) * step;
    const max = Math.ceil(hi / step - 1e-9) * step;
    const ticks = [];
    for (let v = min, k = 0; v <= max + step / 2 && k < 40; v += step, k++) ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
    return { min, max: max > min ? max : min + step, ticks };
  }

  // Tick density: the coarsest clock boundary that still fits, so labels land on round times.
  const LEVELS = [
    () => true,
    (p) => p.mi === 0,
    (p) => p.mi === 0 && p.h % 2 === 0,
    (p) => p.mi === 0 && p.h % 3 === 0,
    (p) => p.mi === 0 && p.h % 6 === 0,
    (p) => p.mi === 0 && p.h % 12 === 0,
    (p) => p.mi === 0 && p.h === 0,
    (p) => p.mi === 0 && p.h === 0 && p.d % 2 === 1,
    (p) => p.mi === 0 && p.h === 0 && p.dow === 1,
    (p) => p.mi === 0 && p.h === 0 && p.d === 1,
    (p) => p.mi === 0 && p.h === 0 && p.d === 1 && p.mo % 3 === 1,
    (p) => p.mi === 0 && p.h === 0 && p.d === 1 && p.mo === 1,
  ];

  function timeTicks(times, maxTicks) {
    const ps = times.map(parts);
    for (const test of LEVELS) {
      const idx = [];
      ps.forEach((p, i) => { if (test(p)) idx.push(i); });
      if (idx.length >= 2 && idx.length <= maxTicks) return idx;
    }
    return evenTicks(times.length, maxTicks);
  }

  function evenTicks(n, maxTicks) {
    const step = Math.max(1, Math.ceil(n / Math.max(1, maxTicks)));
    const idx = [];
    for (let i = 0; i < n; i += step) idx.push(i);
    return idx;
  }

  function tickLabel(ms, g) {
    const p = parts(ms);
    if (g === "1M") return `${p.y}/${pad(p.mo)}`;
    if (p.h === 0 && p.mi === 0) return `${pad(p.mo)}/${pad(p.d)}`;
    return `${pad(p.h)}:${pad(p.mi)}`;
  }

  const finite = (v) => v != null && Number.isFinite(v);
  const f1 = (v) => v.toFixed(1);

  function linePath(s, n, X, Y) {
    const v = s.values, f = s.flags || [];
    let solid = "", est = "", dots = "", prev = null;
    for (let i = 0; i < n - 1; i++) {
      const a = v[i], b = v[i + 1];
      if (!finite(a) || !finite(b)) { prev = null; continue; }
      const kind = f[i] >= 2 || f[i + 1] >= 2 ? "e" : "s";
      const seg = (prev === kind ? "" : `M${f1(X(i))},${f1(Y(a))}`) + `L${f1(X(i + 1))},${f1(Y(b))}`;
      if (kind === "s") solid += seg; else est += seg;
      prev = kind;
    }
    const many = n > 72;
    for (let i = 0; i < n; i++) {
      if (!finite(v[i])) continue;
      const lone = (i === 0 || !finite(v[i - 1])) && (i === n - 1 || !finite(v[i + 1]));
      if (many && !lone) continue;
      dots += `<circle class="dot${f[i] >= 2 ? " hollow" : ""}" cx="${f1(X(i))}" cy="${f1(Y(v[i]))}" r="${lone ? 3 : 2.4}"/>`;
    }
    const cls = "series" + (s.faded ? " faded" : "") + (s.strong ? " strong" : "") + (s.dashed ? " dashed" : "");
    return `<g class="${cls}" style="--c:${s.color}">`
      + (solid ? `<path class="ln" d="${solid}"/>` : "")
      + (est ? `<path class="ln est" d="${est}"/>` : "") + dots + `</g>`;
  }

  /* plot(el, {
       n, times?, labels?, g?, lines?: [{id,color,values,flags,faded,strong,dashed}],
       stacks?: [{id,color,values,faded}], barFlags?, selected?, overlay?: {values,color,format},
       yFormat, yDomain?, height, zero, markers?: [{i,color,tip}],
       tooltip?(i) -> html, onBrush?(i0,i1), onClick?(i), empty, label }) */
  function plot(el, o) { mount(el, () => drawPlot(el, o)); }

  function drawPlot(el, o) {
    const W = Math.max(260, Math.floor(el.clientWidth || 640));
    el._chartWidth = W;
    const H = o.height || 280;
    const n = o.n || 0;
    const lines = o.lines || [];
    const stacks = o.stacks || [];
    let lo = Infinity, hi = -Infinity;
    const see = (v) => { if (finite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; } };
    lines.forEach((s) => { if (!s.faded || o.fadedScale !== false) s.values.forEach(see); });
    for (let i = 0; i < n && stacks.length; i++) {
      let p = 0, q = 0, any = false;
      stacks.forEach((s) => { const v = s.values[i]; if (!finite(v)) return; any = true; if (v >= 0) p += v; else q += v; });
      if (any) { see(p); see(q); }
    }
    if (o.yDomain) { see(o.yDomain[0]); see(o.yDomain[1]); }
    if (!n || !Number.isFinite(lo)) {
      el.innerHTML = `<div class="chart-empty">${esc(o.empty || "這段期間沒有資料")}</div>`;
      return;
    }
    if (o.zero !== false) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    const sc = niceScale(lo, hi, Math.max(3, Math.round(H / 64)));
    const fmt = o.yFormat || ((v) => String(+v.toFixed(6)));
    const yLabels = sc.ticks.map(fmt);
    let ov = null, ovLabels = [];
    if (o.overlay) {
      let a = Infinity, b = -Infinity;
      o.overlay.values.forEach((v) => { if (finite(v)) { a = Math.min(a, v); b = Math.max(b, v); } });
      if (Number.isFinite(a)) {
        ov = niceScale(a, b, sc.ticks.length - 1);
        ovLabels = ov.ticks.map(o.overlay.format || fmt);
      }
    }
    const width = (labels) => Math.round(Math.min(110, 12 + Math.max(0, ...labels.map((s) => String(s).length)) * 6.6));
    const ml = width(yLabels);
    const mr = ov ? width(ovLabels) : 14;
    const mt = 10;
    const mb = 24 + (o.markers && o.markers.length ? 14 : 0);
    const pw = Math.max(40, W - ml - mr);
    const ph = Math.max(40, H - mt - mb);
    const bw = pw / n;
    const X = (i) => ml + (i + 0.5) * bw;
    const Y = (v) => mt + (sc.max - v) / (sc.max - sc.min) * ph;
    let svg = `<svg class="chart-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.label || "圖表")}">`;
    sc.ticks.forEach((v, k) => {
      const y = f1(Y(v));
      svg += `<line class="grid${v === 0 ? " zero" : ""}" x1="${ml}" x2="${ml + pw}" y1="${y}" y2="${y}"/>`
        + `<text class="ylab" x="${ml - 6}" y="${y}" dy="0.32em" text-anchor="end">${esc(yLabels[k])}</text>`;
    });
    if (ov) {
      const Yr = (v) => mt + (ov.max - v) / (ov.max - ov.min) * ph;
      ov.ticks.forEach((v, k) => {
        svg += `<text class="ylab right" x="${ml + pw + 6}" y="${f1(Yr(v))}" dy="0.32em">${esc(ovLabels[k])}</text>`;
      });
    }
    if (o.selected != null && o.selected >= 0 && o.selected < n) {
      svg += `<rect class="sel-band" x="${f1(ml + o.selected * bw)}" y="${mt}" width="${f1(bw)}" height="${ph}"/>`;
    }
    if (stacks.length) {
      const w = Math.max(1, Math.min(bw * 0.74, 46));
      const off = (bw - w) / 2;
      for (let i = 0; i < n; i++) {
        let p = 0, q = 0;
        const est = o.barFlags && o.barFlags[i] >= 2;
        stacks.forEach((s) => {
          const v = s.values[i];
          if (!finite(v) || v === 0) return;
          const base = v >= 0 ? p : q;
          const top = base + v;
          const y1 = Y(Math.max(base, top));
          const y2 = Y(Math.min(base, top));
          svg += `<rect class="bar${est ? " est" : ""}${s.faded ? " faded" : ""}" x="${f1(ml + i * bw + off)}" y="${f1(y1)}"`
            + ` width="${f1(w)}" height="${Math.max(0.6, y2 - y1).toFixed(1)}" fill="${s.color}"/>`;
          if (v >= 0) p = top; else q = top;
        });
      }
    }
    lines.forEach((s) => { svg += linePath(s, n, X, Y); });
    if (ov) {
      const Yr = (v) => mt + (ov.max - v) / (ov.max - ov.min) * ph;
      svg += linePath({ values: o.overlay.values, color: o.overlay.color || token("--ink-2", "#3d4d54"), dashed: false, strong: false },
        n, X, Yr).replace('class="series"', 'class="series overlay"');
    }
    const tickIdx = o.times ? timeTicks(o.times, Math.max(2, Math.floor(pw / 74)))
      : evenTicks(n, Math.max(2, Math.floor(pw / 46)));
    tickIdx.forEach((i) => {
      const x = f1(X(i));
      const text = o.times ? tickLabel(o.times[i], o.g) : (o.labels ? o.labels[i] : String(i + 1));
      svg += `<line class="tick" x1="${x}" x2="${x}" y1="${mt + ph}" y2="${mt + ph + 4}"/>`
        + `<text class="xlab" x="${x}" y="${mt + ph + 16}" text-anchor="middle">${esc(text)}</text>`;
    });
    (o.markers || []).forEach((m) => {
      if (m.i < 0 || m.i >= n) return;
      const x = X(m.i);
      const y = mt + ph + 22;
      svg += `<path class="marker" d="M${f1(x)},${y}l4.5,7h-9z" fill="${m.color || token("--warn", "#8b5e22")}"`
        + ` tabindex="0" data-tip="${esc(m.tip || "")}"/>`;
    });
    svg += `<line class="cross" x1="0" x2="0" y1="${mt}" y2="${mt + ph}" visibility="hidden"/>`
      + `<g class="hover-dots"></g>`
      + `<rect class="brush" x="0" y="${mt}" width="0" height="${ph}" visibility="hidden"/>`
      + `<rect class="hit${o.onBrush ? " brushable" : ""}${o.onClick ? " clickable" : ""}" x="${ml}" y="${mt}" width="${pw}" height="${ph}"/>`
      + `</svg>`;
    el.innerHTML = `<div class="chart-wrap">${svg}<div class="chart-tip" hidden></div></div>`;
    bindPlot(el, o, { n, ml, bw, X, Y, lines });
  }

  function bindPlot(el, o, g) {
    const wrap = el.querySelector(".chart-wrap");
    const svg = wrap.querySelector("svg");
    const hit = svg.querySelector(".hit");
    const cross = svg.querySelector(".cross");
    const dots = svg.querySelector(".hover-dots");
    const brush = svg.querySelector(".brush");
    const tipBox = wrap.querySelector(".chart-tip");
    let dragFrom = null, lastI = -1;
    const idxAt = (ev) => {
      const r = svg.getBoundingClientRect();
      return Math.max(0, Math.min(g.n - 1, Math.floor((ev.clientX - r.left - g.ml) / g.bw)));
    };
    const show = (ev) => {
      const i = idxAt(ev);
      const x = f1(g.X(i));
      cross.setAttribute("x1", x);
      cross.setAttribute("x2", x);
      cross.setAttribute("visibility", "visible");
      if (i !== lastI) {
        lastI = i;
        dots.innerHTML = g.lines.filter((s) => !s.faded).map((s) => {
          const v = s.values[i];
          return finite(v) ? `<circle cx="${x}" cy="${f1(g.Y(v))}" r="3.6" fill="${s.color}" stroke="#fff" stroke-width="1.5"/>` : "";
        }).join("");
        const html = o.tooltip ? o.tooltip(i) : "";
        tipBox.innerHTML = html || "";
        tipBox.hidden = !html;
      }
      if (!tipBox.hidden) {
        const r = wrap.getBoundingClientRect();
        const tw = tipBox.offsetWidth;
        let left = ev.clientX - r.left + 16;
        if (left + tw > r.width - 4) left = ev.clientX - r.left - tw - 16;
        tipBox.style.left = Math.max(0, left) + "px";
        tipBox.style.top = Math.max(0, Math.min(r.height - tipBox.offsetHeight, ev.clientY - r.top - 24)) + "px";
      }
      if (dragFrom != null) {
        const a = Math.min(dragFrom, i), b = Math.max(dragFrom, i);
        brush.setAttribute("x", f1(g.ml + a * g.bw));
        brush.setAttribute("width", f1((b - a + 1) * g.bw));
        brush.setAttribute("visibility", b > a ? "visible" : "hidden");
      }
    };
    const hide = () => {
      cross.setAttribute("visibility", "hidden");
      dots.innerHTML = "";
      tipBox.hidden = true;
      lastI = -1;
    };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", () => { if (dragFrom == null) hide(); });
    hit.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      if (ev.pointerType === "mouse" && o.onBrush) {
        dragFrom = idxAt(ev);
        hit.setPointerCapture(ev.pointerId);
      }
      show(ev);
    });
    hit.addEventListener("pointerup", (ev) => {
      const i = idxAt(ev);
      if (dragFrom != null) {
        const a = Math.min(dragFrom, i), b = Math.max(dragFrom, i);
        dragFrom = null;
        brush.setAttribute("visibility", "hidden");
        if (b > a) { hide(); o.onBrush(a, b); return; }
      }
      if (o.onClick) o.onClick(i);
    });
  }

  function tipRows(title, rows, foot, limit = 16) {
    const shown = rows.slice(0, limit);
    return `<div class="ct-title">${esc(title)}</div>`
      + shown.map((r) => `<div class="ct-row"><i class="sw" style="background:${r.color || "transparent"}"></i>`
        + `<span class="ct-l">${esc(r.label)}</span><b class="ct-v ${r.cls || ""}">${esc(r.value)}</b></div>`).join("")
      + (rows.length > limit ? `<div class="ct-foot">另有 ${rows.length - limit} 筆</div>` : "")
      + (foot ? `<div class="ct-foot">${esc(foot)}</div>` : "");
  }

  /* heat(el, {cols, rows: [{id,label,color,strong,scale,values,tips}], empty, onRowClick})
     Rows sharing a `scale` key share one colour scale (e.g. a total row apart from single grids). */
  function heat(el, o) {
    const groups = {};
    o.rows.forEach((r) => {
      const mags = groups[r.scale || ""] || (groups[r.scale || ""] = []);
      r.values.forEach((v) => { if (finite(v) && v !== 0) mags.push(Math.abs(v)); });
    });
    if (!Object.values(groups).some((m) => m.length)) {
      el.innerHTML = `<div class="chart-empty">${esc(o.empty || "還沒有資料")}</div>`;
      return;
    }
    const maxOf = {};
    Object.entries(groups).forEach(([key, mags]) => {
      mags.sort((a, b) => a - b);
      maxOf[key] = mags[Math.floor((mags.length - 1) * 0.92)] || mags[mags.length - 1] || 1;
    });
    let max = 1;
    const cell = (v, tip) => {
      if (!finite(v)) return `<span class="hc none" data-tip="${esc(tip || "沒有資料")}"></span>`;
      const a = Math.min(1, Math.abs(v) / max);
      return `<span class="hc" style="background:rgba(var(${v >= 0 ? "--heat-pos" : "--heat-neg"}),${(0.1 + 0.85 * a).toFixed(3)})"`
        + ` data-tip="${esc(tip || "")}"></span>`;
    };
    let html = `<div class="heat" style="--cols:${o.cols.length}"><span class="hl"></span>`
      + o.cols.map((c, k) => `<span class="hh">${k % 3 === 0 ? esc(c) : ""}</span>`).join("");
    o.rows.forEach((r) => {
      max = maxOf[r.scale || ""];
      html += `<span class="hl${r.strong ? " strong" : ""}${r.id ? " link" : ""}" ${r.id ? `data-row="${esc(r.id)}" tabindex="0"` : ""}>`
        + `<i class="sw" style="background:${r.color}"></i>${esc(r.label)}</span>`
        + r.values.map((v, k) => cell(v, r.tips && r.tips[k])).join("");
    });
    html += `</div><div class="heat-key"><span>負</span><i class="neg"></i><span>0</span><i class="pos"></i><span>正</span></div>`;
    el.innerHTML = html;
    if (o.onRowClick) {
      el.querySelectorAll("[data-row]").forEach((node) => {
        node.onclick = () => o.onRowClick(node.dataset.row);
        node.onkeydown = (ev) => { if (ev.key === "Enter") o.onRowClick(node.dataset.row); };
      });
    }
  }

  /* hbars(el, {rows: [{label, sub, parts: [{value,color}], marker, tip}], format}) — diverging stacked bars. */
  function hbars(el, o) { mount(el, () => drawHbars(el, o)); }

  function drawHbars(el, o) {
    const rows = o.rows || [];
    if (!rows.length) { el.innerHTML = `<div class="chart-empty">${esc(o.empty || "沒有資料")}</div>`; return; }
    const W = Math.max(300, Math.floor(el.clientWidth || 640));
    el._chartWidth = W;
    const rowH = 34, ml = Math.round(Math.min(230, Math.max(130, W * 0.26))), mr = 18, mt = 6, mb = 24;
    const H = mt + mb + rows.length * rowH;
    const pw = W - ml - mr;
    let lo = 0, hi = 0;
    rows.forEach((r) => {
      let p = 0, q = 0;
      (r.parts || []).forEach((part) => { if (finite(part.value)) { if (part.value >= 0) p += part.value; else q += part.value; } });
      lo = Math.min(lo, q, finite(r.marker) ? r.marker : 0);
      hi = Math.max(hi, p, finite(r.marker) ? r.marker : 0);
    });
    const sc = niceScale(lo, hi, Math.max(2, Math.floor(pw / 110)));
    const fmt = o.format || ((v) => String(v));
    const X = (v) => ml + (v - sc.min) / (sc.max - sc.min) * pw;
    const maxChars = Math.max(6, Math.floor((ml - 14) / 7.2));
    const clip = (s) => { s = String(s || ""); return s.length > maxChars ? s.slice(0, maxChars - 1) + "…" : s; };
    let svg = `<svg class="chart-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.label || "長條圖")}">`;
    sc.ticks.forEach((v) => {
      const x = f1(X(v));
      svg += `<line class="grid${v === 0 ? " zero" : ""}" x1="${x}" x2="${x}" y1="${mt}" y2="${H - mb}"/>`
        + `<text class="xlab" x="${x}" y="${H - mb + 16}" text-anchor="middle">${esc(fmt(v))}</text>`;
    });
    rows.forEach((r, k) => {
      const y = mt + k * rowH;
      const cy = y + rowH / 2;
      svg += `<text class="hlab" x="${ml - 10}" y="${cy - (r.sub ? 4 : 0)}" dy="0.32em" text-anchor="end">${esc(clip(r.label))}</text>`;
      if (r.sub) svg += `<text class="hsub" x="${ml - 10}" y="${cy + 9}" dy="0.32em" text-anchor="end">${esc(clip(r.sub))}</text>`;
      let p = 0, q = 0;
      (r.parts || []).forEach((part) => {
        const v = part.value;
        if (!finite(v) || v === 0) return;
        const base = v >= 0 ? p : q;
        const top = base + v;
        const x1 = X(Math.min(base, top)), x2 = X(Math.max(base, top));
        svg += `<rect class="bar" x="${f1(x1)}" y="${f1(cy - 7)}" width="${Math.max(0.8, x2 - x1).toFixed(1)}" height="14" fill="${part.color}"/>`;
        if (v >= 0) p = top; else q = top;
      });
      if (finite(r.marker)) {
        const x = f1(X(r.marker));
        svg += `<line class="mk" x1="${x}" x2="${x}" y1="${f1(cy - 11)}" y2="${f1(cy + 11)}"/><circle class="mk" cx="${x}" cy="${f1(cy)}" r="3.2"/>`;
      }
      svg += `<rect class="row-hit" x="0" y="${y}" width="${W}" height="${rowH}" tabindex="0" data-tip="${esc(r.tip || "")}"/>`;
    });
    svg += `</svg>`;
    el.innerHTML = `<div class="chart-wrap">${svg}</div>`;
  }

  /* spark(values, {width, height}) -> inline SVG string for table cells. */
  function spark(values, opts = {}) {
    const w = opts.width || 92, h = opts.height || 26, padY = 3, padX = 3;
    const pts = [];
    values.forEach((v, i) => { if (finite(v)) pts.push([i, v]); });
    if (!pts.length) return `<span class="spark-none">—</span>`;
    let lo = Math.min(0, ...pts.map((p) => p[1]));
    let hi = Math.max(0, ...pts.map((p) => p[1]));
    if (hi === lo) hi = lo + 1;
    const n = values.length;
    const X = (i) => padX + (n === 1 ? (w - 2 * padX) / 2 : i * (w - 2 * padX) / (n - 1));
    const Y = (v) => padY + (hi - v) / (hi - lo) * (h - 2 * padY);
    let d = "", prev = false;
    values.forEach((v, i) => {
      if (!finite(v)) { prev = false; return; }
      d += (prev ? "L" : "M") + f1(X(i)) + "," + f1(Y(v));
      prev = true;
    });
    const avg = pts.reduce((s, p) => s + p[1], 0) / pts.length;
    const c = avg >= 0 ? token("--accent", "#2e6970") : token("--down", "#b42318");
    const last = pts[pts.length - 1];
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">`
      + `<line x1="${padX}" x2="${w - padX}" y1="${f1(Y(0))}" y2="${f1(Y(0))}" class="spark-zero"/>`
      + `<path d="${d}" fill="none" stroke="${c}" stroke-width="1.5" stroke-linejoin="round"/>`
      + `<circle cx="${f1(X(last[0]))}" cy="${f1(Y(last[1]))}" r="2.2" fill="${c}"/></svg>`;
  }

  global.Charts = { plot, heat, hbars, spark, color, token, fmtTime, fmtRange, parts, tipRows, esc };
})(window);
