export const state = {
  page: "live",
  renderSeq: 0,
  historyId: new URLSearchParams(location.search).get("grid") || "",
  historyFrom: "",
  boardSort: { key: "", dir: "desc" },
  dailyData: null,
};
