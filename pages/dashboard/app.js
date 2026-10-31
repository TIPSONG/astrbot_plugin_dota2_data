const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
let connected = false;
let settingsLoaded = false;
let noticeTimer;
const titles = { overview: "数据与对话，各司其职", query: "每一场，都有据可查", bindings: "认出一起打游戏的人", settings: "让查询保持适度", records: "留下一条清晰的查询线索" };
const actionNames = { profile: "玩家资料", matches: "条件战绩", match: "单场详情", hero_pool: "英雄池", compare: "多人比较", meta: "英雄版本统计" };
const fieldNames = { account_id: "账号 ID", match_id: "比赛 ID", hero_id: "英雄 ID", hero: "英雄", hero_name: "英雄", name: "名称", alias: "别名", games: "场次", matches: "场次", wins: "胜场", losses: "负场", win_rate: "胜率", kills: "击杀", deaths: "死亡", assists: "助攻", kda: "KDA", duration: "时长（秒）", duration_ms: "耗时（毫秒）", start_time: "开局时间", gold_per_min: "GPM", xp_per_min: "XPM", last_hits: "补刀", denies: "反补", hero_damage: "英雄伤害", tower_damage: "建筑伤害", hero_healing: "治疗", rank_tier: "段位代码", leaderboard_rank: "排名", mode: "模式", outcome: "结果", player_name: "玩家", personaname: "玩家名", scope: "会话", user_id: "发送者 ID", action: "查询类型", status: "状态", at: "时间", created_at: "时间", timestamp: "时间", count: "条目数", fetched_at: "数据时间", source: "来源", cached: "缓存命中", complete: "范围完整", scan_limit: "扫描上限", total: "合计", limit: "返回上限" };

Object.assign(fieldNames, {
  summary: "样本统计", players: "玩家", heroes: "英雄", won: "胜负",
  game_mode: "模式代码", lobby_type: "大厅类型", player_slot: "玩家槽位",
  known_outcomes: "已知胜负场次", unknown_outcomes: "胜负缺失场次",
  win_rate_percent: "胜率（%）", average_kills: "场均击杀", average_deaths: "场均死亡",
  average_assists: "场均助攻", average_gpm: "平均 GPM", average_xpm: "平均 XPM",
  average_last_hits: "场均补刀", average_duration_seconds: "平均时长（秒）",
});

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text == null ? "—" : String(text);
  if (className) node.className = className;
  return node;
}

function message(error) { return error instanceof Error ? error.message : String(error || "未知错误"); }
function showNotice(text, kind = "success", persistent = false) {
  clearTimeout(noticeTimer);
  const box = $("notice");
  box.textContent = text;
  box.className = `notice ${kind}`;
  if (!persistent) noticeTimer = setTimeout(() => box.classList.add("hidden"), 6500);
}

// Current AstrBot bridge already unwraps {status:'ok', data:...}.
// The fallback handles versions that return the wrapper unchanged.
function unwrap(value) {
  if (value && value.status === "error") throw new Error(value.message || "请求失败");
  if (value && value.status === "ok" && Object.hasOwn(value, "data") && !Object.hasOwn(value, "meta")) return value.data;
  return value;
}

async function api(endpoint, body) {
  if (!connected) throw new Error("面板尚未连接。请从 AstrBot 插件页面打开此视图。");
  return unwrap(await (body === undefined ? bridge.apiGet(endpoint) : bridge.apiPost(endpoint, body)));
}

async function busy(button, fn) {
  if (button.disabled) return;
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = button.dataset.busyLabel || "处理中…";
  try { return await fn(); }
  catch (error) { showNotice(message(error), "error", true); }
  finally { button.disabled = false; button.textContent = previous; }
}

function openTab(name) {
  if (!(name in titles)) return;
  document.querySelectorAll(".tab-panel").forEach((node) => node.classList.toggle("hidden", node.id !== `tab-${name}`));
  document.querySelectorAll(".nav-item").forEach((node) => {
    node.classList.toggle("active", node.dataset.tab === name);
    if (node.dataset.tab === name) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  $("page-title").textContent = titles[name];
}

function formatTime(value) {
  if (value == null || value === "") return "—";
  const date = typeof value === "number" ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function display(value, key = "") {
  if (value == null) return "—";
  if (key === "won" && typeof value === "boolean") return value ? "胜" : "负";
  if (["at", "created_at", "timestamp", "start_time", "fetched_at"].includes(key)) return formatTime(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value);
  if (key === "action") return actionNames[value] || String(value);
  return String(value);
}

function renderTable(rows, columns, actions) {
  const wrap = textNode("div", "", "table-wrap");
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const head = document.createElement("tr");
  columns.forEach((key) => head.append(textNode("th", fieldNames[key] || key)));
  if (actions) head.append(textNode("th", "操作"));
  thead.append(head); table.append(thead);
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const key of columns) {
      const value = display(row[key], key);
      const td = textNode("td", value, key === "scope" ? "wrap" : "");
      td.title = value;
      tr.append(td);
    }
    if (actions) { const td = document.createElement("td"); td.append(actions(row)); tr.append(td); }
    tbody.append(tr);
  }
  table.append(tbody); wrap.append(table);
  return wrap;
}

function renderValues(data) {
  const grid = textNode("div", "", "key-values");
  for (const [key, value] of Object.entries(data)) {
    if (value !== null && typeof value === "object") continue;
    const cell = textNode("div", "", "key-value");
    cell.append(textNode("span", fieldNames[key] || key), textNode("strong", display(value, key)));
    grid.append(cell);
  }
  return grid;
}

function firstPresent(object, keys) {
  for (const key of keys) if (object[key] !== undefined && object[key] !== null) return object[key];
  return "—";
}

async function loadStatus() {
  const data = await api("status");
  if (!data || typeof data !== "object") throw new Error("状态响应格式不正确。");
  $("stat-enabled").textContent = typeof data.enabled === "boolean" ? (data.enabled ? "已启用" : "已关闭") : "—";
  $("stat-bindings").textContent = firstPresent(data, ["binding_count", "bindings_count", "bindings"]);
  $("stat-cache").textContent = firstPresent(data, ["cache_entries", "cache_count"]);
  $("stat-requests").textContent = firstPresent(data, ["requests_today", "daily_requests"]);
  $("status-raw").textContent = JSON.stringify(data, null, 2);
  const preferred = { stratz: "STRATZ", opendota: "OpenDota" }[data.data_source_priority] || "未知";
  const tokenState = data.stratz_configured ? "已配置" : "未配置";
  $("connection").textContent = `玩家与比赛查询优先使用 ${preferred}。STRATZ Token ${tokenState}。渠道连通性以实际查询结果为准。`;
  $("connection").className = "notice";
}

const numericSettings = ["timeout_seconds", "cache_ttl_seconds", "max_matches", "scan_limit", "requests_per_minute", "daily_request_limit"];
async function loadSettings() {
  const data = await api("settings");
  if (!data || typeof data !== "object") throw new Error("设置响应格式不正确。");
  const form = $("settings-form");
  for (const key of numericSettings) form.elements[key].value = data[key] ?? "";
  form.elements.timezone.value = data.timezone || "";
  form.elements.enabled.checked = data.enabled === true;
  form.elements.data_source_priority.value = data.data_source_priority === "stratz" ? "stratz" : "opendota";
  form.elements.fallback_enabled.checked = data.fallback_enabled !== false;
  form.elements.stratz_api_key.value = "";
  form.elements.clear_stratz_api_key.checked = false;
  form.elements.opendota_api_key.value = "";
  form.elements.clear_api_key.checked = false;
  $("key-status").textContent = data.api_key_configured ? "已保存 OpenDota Key；留空保持不变。" : "尚未配置 OpenDota Key，可使用公开接口。";
  $("stratz-key-status").textContent = data.stratz_api_key_configured ? "已保存 STRATZ Token；留空保持不变。密钥有效性尚未在此页面验证。" : "尚未配置 STRATZ Token；使用 STRATZ 需要填写有效 Token。";
  settingsLoaded = true;
  $("settings-state").textContent = "已从后端读取当前设置。";
}

async function loadBindings() {
  const response = await api("bindings");
  const rows = Array.isArray(response) ? response : response?.bindings;
  if (!Array.isArray(rows)) throw new Error("绑定列表格式不正确。");
  const list = $("bindings-list");
  list.replaceChildren(); list.className = "";
  if (!rows.length) { list.append(textNode("p", "还没有绑定账号。可以在聊天中绑定，也可以填写上方表单。", "empty-state")); return; }
  list.append(renderTable(rows, ["scope", "user_id", "account_id", "alias"], (row) => {
    const actions = textNode("div", "", "row-actions");
    const edit = textNode("button", "编辑", "button secondary small"); edit.type = "button";
    edit.addEventListener("click", () => {
      const form = $("binding-form");
      for (const key of ["scope", "user_id", "account_id", "alias"]) form.elements[key].value = row[key] ?? "";
      form.scrollIntoView({ block: "start" });
    });
    const remove = textNode("button", "移除", "button secondary small danger"); remove.type = "button";
    let armed = false;
    remove.addEventListener("click", () => {
      if (!armed) { armed = true; remove.textContent = "再次点击确认"; setTimeout(() => { if (!remove.disabled) { armed = false; remove.textContent = "移除"; } }, 5000); return; }
      void busy(remove, async () => { await api("bindings/remove", { scope: row.scope, user_id: row.user_id }); await loadBindings(); await loadStatus(); showNotice("绑定已移除。"); });
    });
    actions.append(edit, remove); return actions;
  }));
}

async function loadRecords() {
  const response = await api("queries");
  const rows = Array.isArray(response) ? response : response?.queries;
  if (!Array.isArray(rows)) throw new Error("查询记录格式不正确。");
  const list = $("records-list"); list.replaceChildren(); list.className = "";
  if (!rows.length) { list.append(textNode("p", "还没有查询记录。", "empty-state")); return; }
  const preferred = ["at", "created_at", "timestamp", "action", "status", "scope", "user_id", "count", "duration_ms", "summary"];
  const keys = preferred.filter((key) => rows.some((row) => Object.hasOwn(row, key)));
  list.append(renderTable(rows, keys.length ? keys : Object.keys(rows[0]).slice(0, 8)));
}

function queryVisibility() {
  const action = $("query-action").value;
  document.querySelectorAll("[data-query-for]").forEach((node) => node.classList.toggle("hidden", !node.dataset.queryFor.split(" ").includes(action)));
  const dates = ["matches", "hero_pool", "compare"].includes(action) && $("query-period").value === "custom";
  $("start-date-field").classList.toggle("hidden", !dates);
  $("end-date-field").classList.toggle("hidden", !dates);
}

function collectQuery() {
  const form = $("query-form");
  const value = (key) => form.elements[key].value.trim();
  const action = value("action"); const params = {};
  if (["profile", "matches", "hero_pool"].includes(action)) params.target = value("target") || "me";
  if (action === "match") {
    if (!/^\d+$/.test(value("match_id"))) throw new Error("请填写数字比赛 ID。");
    params.match_id = value("match_id");
  }
  if (action === "compare") {
    params.targets = value("targets").split(/[\n,，]+/).map((s) => s.trim()).filter(Boolean);
    if (params.targets.length < 2 || params.targets.length > 6) throw new Error("请填写 2 至 6 个对比玩家。");
    params.common_matches_only = form.elements.common_matches_only.checked;
  }
  if (["matches", "hero_pool", "compare", "meta"].includes(action)) {
    params.limit = Number(value("limit"));
    if (value("hero")) params.hero = value("hero");
  }
  if (["matches", "hero_pool", "compare"].includes(action)) {
    params.mode = value("mode"); params.outcome = value("outcome");
    if (value("period") === "custom") {
      if (!value("start_date") || !value("end_date")) throw new Error("自选日期需要同时填写开始和结束日期。");
      if (value("start_date") > value("end_date")) throw new Error("开始日期不能晚于结束日期。");
      params.start_date = value("start_date"); params.end_date = value("end_date");
    } else params.period = value("period");
  }
  return { action, params, scope: value("scope"), user_id: value("user_id") };
}

function renderResult(response) {
  // Panel query returns {result: full service result}; this preserves meta through bridge unwrapping.
  const result = response?.result ?? response;
  if (!result || typeof result !== "object") throw new Error("查询返回了无法识别的数据。");
  $("result-raw").textContent = JSON.stringify(result, null, 2);
  $("result-detail").classList.remove("hidden");
  const state = String(result.status || "unknown");
  const statusLabel = { ok: "查询完成", success: "查询完成", empty: "没有匹配记录", partial: "部分数据", error: "查询失败", unavailable: "暂时不可用", needs_binding: "需要绑定账号", needs_input: "需要补充信息", clarification: "需要补充信息", needs_clarification: "需要补充信息", disabled: "工具已关闭" };
  $("result-status").textContent = statusLabel[state] || state;
  $("result-status").className = `pill ${["error", "unavailable"].includes(state) ? "error" : ["ok", "success"].includes(state) ? "" : "warn"}`;
  const content = $("result-content"); content.replaceChildren(); content.className = "";
  const meta = result.meta || {};
  const badges = textNode("div", "", "result-meta");
  for (const key of ["source", "fetched_at", "cached", "complete"]) if (meta[key] != null) badges.append(textNode("span", `${fieldNames[key] || key}：${display(meta[key], key)}`, "pill neutral"));
  if (badges.childNodes.length) content.append(badges);
  if (Array.isArray(meta.sources) && meta.sources.length) {
    content.append(textNode("p", `本次涉及的数据源：${meta.sources.map((source) => typeof source === "string" ? source : JSON.stringify(source)).join("；")}`, "result-note provenance-note"));
  }
  const fallback = meta.fallbacks ?? meta.fallback ?? meta.fallback_reason;
  if (fallback != null && fallback !== false && (!Array.isArray(fallback) || fallback.length)) {
    content.append(textNode("p", `数据源回退：${display(fallback)}`, "result-note"));
  }
  if (result.message) content.append(textNode("p", result.message, "result-note"));
  const notes = [...(Array.isArray(meta.limitations) ? meta.limitations : meta.limitations ? [meta.limitations] : []), ...(Array.isArray(meta.warnings) ? meta.warnings : meta.warnings ? [meta.warnings] : [])];
  notes.forEach((note) => content.append(textNode("p", typeof note === "object" ? JSON.stringify(note) : note, "result-note")));
  if (meta.coverage) content.append(textNode("p", `查询范围：${display(meta.coverage)}`, "result-note"));
  const data = result.data;
  if (data && !Array.isArray(data) && typeof data === "object") content.append(renderValues(data));
  if (data?.summary && typeof data.summary === "object") {
    content.append(textNode("h3", "样本统计"), renderValues(data.summary));
  }
  const collections = Array.isArray(data) ? [["结果", data]] : data && typeof data === "object" ? Object.entries(data).filter(([, value]) => Array.isArray(value)) : [];
  for (const [key, rows] of collections) {
    content.append(textNode("h3", `${fieldNames[key] || key} · ${rows.length} 条`));
    if (!rows.length) { content.append(textNode("p", "该集合为空。", "muted")); continue; }
    if (typeof rows[0] !== "object" || rows[0] === null) { content.append(textNode("pre", JSON.stringify(rows, null, 2))); continue; }
    // Comparisons carry per-player summaries; surface them instead of showing only IDs.
    const tableRows = rows.map((row) => row.summary ? { ...row, ...row.summary, source: row.coverage?.source } : row);
    const preferred = key === "matches" ? ["match_id", "hero_name", "won", "kills", "deaths", "assists", "gold_per_min", "xp_per_min", "start_time"]
      : key === "players" && rows[0].summary ? ["account_id", "matches", "wins", "losses", "win_rate_percent", "average_kills", "average_deaths", "average_assists", "source"]
      : [];
    const candidates = [...new Set([...preferred, ...tableRows.slice(0, 10).flatMap((row) => Object.keys(row))])];
    const scalar = candidates.filter((k) => tableRows.some((row) => row[k] != null && typeof row[k] !== "object")).slice(0, 10);
    if (scalar.length) content.append(renderTable(tableRows.slice(0, 100), scalar));
    else content.append(textNode("p", "嵌套数据请展开下方完整结果查看。", "muted"));
    if (rows.length > 100) content.append(textNode("p", "表格仅显示前 100 条；完整返回值见下方。", "muted"));
  }
  if (data && typeof data !== "object") content.append(textNode("p", data));
  if (!content.textContent.trim()) content.append(textNode("p", "没有可显示的摘要，请查看完整结果。", "empty-state"));
}

async function refresh() {
  const loaders = [["后端状态", loadStatus], ["设置", loadSettings], ["账号绑定", loadBindings], ["查询记录", loadRecords]];
  const results = await Promise.allSettled(loaders.map(([, fn]) => fn()));
  const failures = results.flatMap((result, index) => result.status === "rejected" ? [`${loaders[index][0]}：${message(result.reason)}`] : []);
  if (failures.length) showNotice(failures.join("；"), "error", true);
  if (results[0].status === "rejected") { $("connection").className = "notice error"; $("connection").textContent = "无法读取插件状态。请检查插件是否已加载，以及当前 AstrBot 是否支持插件视图 API。"; }
}

document.querySelectorAll("[data-tab]").forEach((node) => node.addEventListener("click", () => openTab(node.dataset.tab)));
document.querySelectorAll("[data-open-tab]").forEach((node) => node.addEventListener("click", () => openTab(node.dataset.openTab)));
$("query-action").addEventListener("change", queryVisibility);
$("query-period").addEventListener("change", queryVisibility);
$("refresh").addEventListener("click", (event) => void busy(event.currentTarget, refresh));
$("reload-settings").addEventListener("click", (event) => void busy(event.currentTarget, loadSettings));
$("reload-bindings").addEventListener("click", (event) => void busy(event.currentTarget, loadBindings));
$("reload-records").addEventListener("click", (event) => void busy(event.currentTarget, loadRecords));

$("query-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void busy(event.submitter || event.currentTarget.querySelector("[type=submit]"), async () => {
    const query = collectQuery();
    $("result-status").textContent = "查询中"; $("result-status").className = "pill neutral";
    try { renderResult(await api("query", query)); }
    catch (error) { $("result-status").textContent = "本次查询失败"; $("result-status").className = "pill error"; $("result-content").replaceChildren(textNode("p", message(error), "result-note")); $("result-detail").classList.add("hidden"); throw error; }
    const outcomes = await Promise.allSettled([loadRecords(), loadStatus()]);
    if (outcomes.some((result) => result.status === "rejected")) showNotice("查询结果已返回，但状态或记录刷新失败，可稍后点击刷新。", "warn");
  });
});

$("binding-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  void busy(event.submitter || form.querySelector("[type=submit]"), async () => {
    const body = Object.fromEntries(["scope", "user_id", "account_id", "alias"].map((key) => [key, form.elements[key].value.trim()]));
    if (!/^\d+$/.test(body.account_id)) throw new Error("Dota / Steam64 账号必须是数字 ID。");
    await api("bindings/save", body);
    showNotice("绑定已保存。");
    await loadBindings(); await loadStatus();
  });
});

$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  void busy(event.submitter || form.querySelector("[type=submit]"), async () => {
    if (!settingsLoaded) throw new Error("请先成功读取当前设置，避免覆盖未知配置。");
    const payload = { enabled: form.elements.enabled.checked, timezone: form.elements.timezone.value.trim(), data_source_priority: form.elements.data_source_priority.value, fallback_enabled: form.elements.fallback_enabled.checked, clear_api_key: form.elements.clear_api_key.checked, clear_stratz_api_key: form.elements.clear_stratz_api_key.checked };
    for (const key of numericSettings) payload[key] = Number(form.elements[key].value);
    const key = form.elements.opendota_api_key.value.trim();
    if (key && payload.clear_api_key) throw new Error("填写新 Key 与清除 Key 不能同时选择。");
    if (key) payload.opendota_api_key = key;
    const stratzKey = form.elements.stratz_api_key.value.trim();
    if (stratzKey && payload.clear_stratz_api_key) throw new Error("填写新 STRATZ Token 与清除 STRATZ Token 不能同时选择。");
    if (stratzKey) payload.stratz_api_key = stratzKey;
    if (payload.scan_limit < payload.max_matches) throw new Error("扫描场次上限不能小于返回场次上限。");
    await api("settings", payload);
    form.elements.opendota_api_key.value = "";
    form.elements.stratz_api_key.value = "";
    showNotice("设置已保存。");
    await loadSettings(); await loadStatus();
  });
});

let clearArmed = false;
$("clear-cache").addEventListener("click", (event) => {
  const button = event.currentTarget;
  if (!clearArmed) { clearArmed = true; button.textContent = "再次点击确认清空"; setTimeout(() => { if (!button.disabled) { clearArmed = false; button.textContent = "清空缓存"; } }, 5000); return; }
  void busy(button, async () => { await api("cache/clear", {}); clearArmed = false; showNotice("查询缓存已清空。"); await loadStatus(); }).finally(() => { button.textContent = "清空缓存"; });
});

queryVisibility();
if (!bridge || typeof bridge.ready !== "function") {
  $("connection").textContent = "此页面需要从 AstrBot 的插件视图中打开。直接打开 HTML 无法连接后端；当前未发起任何数据请求。";
  $("connection").className = "notice warn";
} else {
  try { await bridge.ready(); connected = true; await refresh(); }
  catch (error) { $("connection").className = "notice error"; $("connection").textContent = `AstrBot 面板连接失败：${message(error)}`; }
}
