import { state } from "./state.js";

import { app, esc, clearFlash } from "./common.js";

export const PAGES = ["live", "records", "daily", "calendar", "efficiency", "funding", "levercmp", "history", "settlements", "status"];
export const PAGE_LABELS = {
  live: "目前倉位",
  records: "倉位紀錄",
  daily: "每日詳情",
  calendar: "月曆",
  efficiency: "效率走勢",
  funding: "資金費",
  levercmp: "槓桿對照",
  settlements: "關倉紀錄",
  status: "執行狀態",
};

// 頁面放在網址 #hash：重新整理留在原頁，瀏覽器上一頁可回到來源頁。
export function parseHash() {
  let raw = "";
  try {
    raw = decodeURIComponent(location.hash.replace(/^#/, ""));
  } catch (_) {
    return { page: "", id: "" };
  }
  const [name, ...rest] = raw.split("/");
  return { page: PAGES.includes(name) ? name : "", id: rest.join("/") };
}

export function isCurrent(seq, page) {
  return seq === state.renderSeq && state.page === page;
}

export function pageHash(name) {
  return name === "history" && state.historyId ? "#history/" + encodeURIComponent(state.historyId) : "#" + name;
}

export function go(name, opts = {}) {
  const target = pageHash(name);
  if (location.hash === target) { applyRoute(); return; }
  if (opts.replace) location.replace(target);
  else location.hash = target;
}

export function applyRoute() {
  const route = parseHash();
  const next = route.page || "live";
  if (next === "history" && route.id) state.historyId = route.id;
  if (next !== "history") state.historyFrom = "";
  if (next !== state.page) clearFlash();
  state.page = next;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === state.page));
  render();
}

let routerStarted = false;
export function startRouter() {
  if (routerStarted) return;
  routerStarted = true;
  window.addEventListener("hashchange", applyRoute);
  document.querySelectorAll("nav button").forEach((btn) => {
    btn.onclick = () => go(btn.dataset.page);
  });
}

const renderers = {};

export function setRenderers(map) {
  Object.assign(renderers, map);
}

export async function render() {
  const seq = ++state.renderSeq;
  const page = state.page;
  try {
    if (page === "live") await renderers.live(seq);
    else if (page === "records") await renderers.records(seq);
    else if (page === "daily") await renderers.daily(seq);
    else if (page === "calendar") await renderers.calendar(seq);
    else if (page === "efficiency") await renderers.efficiency(seq);
    else if (page === "funding") await renderers.funding(seq);
    else if (page === "levercmp") await renderers.levercmp(seq);
    else if (page === "history") await renderers.history(seq);
    else if (page === "settlements") await renderers.settlements(seq);
    else await renderers.status(seq);
  } catch (err) {
    if (!isCurrent(seq, page)) return;
    app.innerHTML = `<p class="warn">${esc(err.message)}</p>`;
  }
}
