import { state } from "./state.js";
import { initTips, registerGoHistory } from "./common.js";
import { applyRoute, go, pageHash, parseHash, setRenderers, startRouter } from "./router.js";
import { renderLive, startLivePolling } from "./pages/live.js";
import { renderBoard } from "./pages/records.js";
import { renderDaily } from "./pages/daily.js";
import { renderCalendar } from "./pages/calendar.js";
import { renderEfficiency } from "./pages/efficiency.js";
import { renderFunding } from "./pages/funding.js";
import { renderLeverCompare } from "./pages/levercmp.js";
import { renderHistory } from "./pages/history.js";
import { renderSettlements } from "./pages/settlements.js";
import { renderStatus } from "./pages/status.js";

setRenderers({
  live: renderLive,
  records: renderBoard,
  daily: renderDaily,
  calendar: renderCalendar,
  efficiency: renderEfficiency,
  funding: renderFunding,
  levercmp: renderLeverCompare,
  history: renderHistory,
  settlements: renderSettlements,
  status: renderStatus,
});
registerGoHistory(() => go("history"));
initTips();
startRouter();
startLivePolling();

if (!parseHash().page && state.historyId) location.replace(pageHash("history"));
else applyRoute();
