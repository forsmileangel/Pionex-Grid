export const state = {
  page: "live",
  historyId: new URLSearchParams(location.search).get("grid") || "",
  historyFrom: "",
  boardSort: { key: "", dir: "desc" },
  dailyData: null,
};
