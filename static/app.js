"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const RESULTS = ["INACTIVE", "UNSURE", "ACTIVE"];
const RESULT_LABEL = { INACTIVE: "Inactive", UNSURE: "Unsure", ACTIVE: "Active" };
const TYPE_ALIASES = {
  cisco: "cisco_ios", ios: "cisco_ios", cisco_xe: "cisco_ios", catalyst: "cisco_ios",
  nxos: "cisco_nxos", nexus: "cisco_nxos", n7k: "cisco_nxos", n9k: "cisco_nxos",
  dell: "dell_powerconnect", powerconnect: "dell_powerconnect", "5548p": "dell_powerconnect",
  "5548": "dell_powerconnect", "5524": "dell_powerconnect",
  s3500: "aruba_mas", s2500: "aruba_mas", s1500: "aruba_mas", aruba_os: "aruba_mas", mas: "aruba_mas",
};
["2960", "2960s", "2960x", "3560", "3560g", "3560cg", "3650", "3750", "3750g", "4506", "4506e",
  "4507", "4948", "c4948", "9200", "9200l", "c9200l"].forEach(m => { TYPE_ALIASES[m] = "cisco_ios"; });
const PORTS_PER_BAND = 48;

const state = {
  settings: null,
  switches: [],
  types: {},
  models: {},
  scope: "",
  switchFind: "",
  dirty: false,
  report: null,
  ports: [],
  filters: { results: new Set(["INACTIVE"]), switch: "", building: "", search: "" },
  sort: { key: null, dir: 1 },
  logNext: 0,
  pollTimer: null,
};

// ---------------------------------------------------------------- helpers
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let res, data = {};
  try {
    res = await fetch(path, opts);
    data = await res.json();
  } catch (err) {
    return { ok: false, data: { problems: ["Can't reach the app. Is the RunAnalyzer window still open?"] } };
  }
  return { ok: res.ok && data.ok !== false, status: res.status, data };
}

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function notice(id, kind, messages) {
  const box = $("#" + id);
  if (!messages || !messages.length) { box.hidden = true; box.replaceChildren(); return; }
  box.className = "notice " + (kind || "");
  box.replaceChildren(messages.length === 1
    ? h("p", { text: messages[0] })
    : h("ul", {}, messages.map(m => h("li", { text: m }))));
  box.hidden = false;
}

function cleanType(value) {
  const v = String(value || "").trim().toLowerCase().replace(/[-\s]/g, "_");
  const t = TYPE_ALIASES[v] || v;
  return state.types[t] ? t : "";
}

function remember(key, value) { try { localStorage.setItem("pa." + key, value); } catch (e) { /* private window */ } }
function recall(key) { try { return localStorage.getItem("pa." + key); } catch (e) { return null; } }
function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + "s")}`; }
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const natural = (a, b) => String(a || "").localeCompare(String(b || ""), undefined, { numeric: true });

// ---------------------------------------------------------------- what to check
function savedSwitches() { return state.switches.filter(s => s.address); }

function buildingsInUse() {
  const counts = new Map();
  savedSwitches().filter(s => s.building).forEach(s => counts.set(s.building, (counts.get(s.building) || 0) + 1));
  return [...counts].sort((a, b) => natural(a[0], b[0]));
}

function switchesInScope() {
  const saved = savedSwitches();
  if (state.scope.startsWith("b:")) return saved.filter(s => s.building === state.scope.slice(2));
  if (state.scope.startsWith("s:")) return saved.filter(s => s.address === state.scope.slice(2));
  return saved;
}

function switchLabel(s) { return s.location ? `${s.location} (${s.address})` : s.address; }

function renderScope() {
  const saved = savedSwitches();
  const buildings = buildingsInUse();
  const valid = state.scope === "" ||
    (state.scope.startsWith("b:") && buildings.some(([b]) => b === state.scope.slice(2))) ||
    (state.scope.startsWith("s:") && saved.some(s => s.address === state.scope.slice(2)));
  if (!valid) state.scope = "";
  const groups = [h("option", { value: "", text: `Every switch (${saved.length})` })];
  if (buildings.length) {
    groups.push(h("optgroup", { label: "One building" },
      buildings.map(([b, n]) => h("option", { value: `b:${b}`, text: `${b} (${n})` }))));
  }
  if (saved.length) {
    const sorted = [...saved].sort((a, b) => natural(a.building, b.building) || natural(a.location, b.location)
      || natural(a.address, b.address));
    groups.push(h("optgroup", { label: "One switch" },
      sorted.map(s => h("option", { value: `s:${s.address}`,
        text: (s.building ? `${s.building}: ` : "") + switchLabel(s) }))));
  }
  $("#scope").replaceChildren(...groups);
  $("#scope").value = state.scope;
  refreshScopeHint();
}

function refreshScopeHint() {
  const n = switchesInScope().length;
  const months = $("#months").value || state.settings.months;
  $("#scopeHint").textContent = savedSwitches().length
    ? `Checks ${plural(n, "switch", "switches")} for ports unused for ${months}+ months.`
    : "Add your switches under Switch list first.";
}

// ---------------------------------------------------------------- switch list
function typeOptions(selected) {
  return [h("option", { value: "", text: "Choose a type", selected: !selected }),
    ...Object.entries(state.types).map(([value, label]) =>
      h("option", { value, text: label, selected: value === selected }))];
}

function refreshSwitchSummary() {
  const saved = savedSwitches();
  const ssh = saved.filter(s => s.protocol === "ssh");
  const telnet = saved.filter(s => s.protocol !== "ssh");
  const buildings = buildingsInUse();
  let text = "None yet. Import your switch list, or add switches one at a time.";
  if (saved.length) {
    const parts = [];
    if (ssh.length) parts.push(`${ssh.length} over SSH`);
    if (telnet.length) parts.push(`${telnet.length} over Telnet`);
    const where = buildings.length > 1 ? ` in ${plural(buildings.length, "building")}` : "";
    text = `${plural(saved.length, "switch", "switches")} saved${where}: ${parts.join(", ")}.`;
  }
  $("#switchSummary").textContent = text;
  $("#buildingList").replaceChildren(...buildings.map(([b]) => h("option", { value: b })));
  const used = new Set(saved.map(s => s.protocol));
  $("#sshDifferentRow").hidden = !(used.has("ssh") && used.has("telnet"));
  if ($("#sshDifferentRow").hidden) { $("#sshDifferent").checked = false; syncSshLogin(); }
  renderScope();
}

function toggleEditor(force) {
  const editor = $("#switchEditor");
  const show = force === undefined ? editor.hidden : force;
  editor.hidden = !show;
  $("#toggleEditor").setAttribute("aria-expanded", String(show));
  $("#toggleEditor").textContent = show ? "Done editing" : "Edit switches";
}

function markDirty(dirty = true) {
  state.dirty = dirty;
  $("#switchDirty").hidden = !dirty;
}

function switchMatches(s) {
  const q = state.switchFind.trim().toLowerCase();
  if (!q || !s.address) return true;
  return [s.address, s.type, state.types[s.type], s.protocol, s.building, s.location].join(" ").toLowerCase().includes(q);
}

function renderSwitchRows() {
  const rows = [];
  let shown = 0;
  state.switches.forEach((s, i) => {
    if (!switchMatches(s)) return;
    shown++;
    const testCell = h("td", { colspan: "6", class: "test-result" });
    const testRow = h("tr", { class: "test-row", hidden: true }, testCell);
    rows.push(h("tr", {},
      h("td", {}, h("input", { class: "mono", value: s.address, placeholder: "10.99.4.11", "aria-label": "Switch address",
        oninput: e => { s.address = e.target.value.trim(); markDirty(); } })),
      h("td", {}, h("select", { "aria-label": "Switch type",
        onchange: e => { s.type = e.target.value; markDirty(); } }, typeOptions(s.type))),
      h("td", {}, h("select", { "aria-label": "Connect with", onchange: e => { s.protocol = e.target.value; markDirty(); } },
        h("option", { value: "telnet", text: "Telnet", selected: s.protocol !== "ssh" }),
        h("option", { value: "ssh", text: "SSH", selected: s.protocol === "ssh" }))),
      h("td", {}, h("input", { value: s.building || "", placeholder: "Optional", list: "buildingList", "aria-label": "Building",
        oninput: e => { s.building = e.target.value.trim(); markDirty(); } })),
      h("td", {}, h("input", { value: s.location || "", placeholder: "Optional", "aria-label": "Location",
        oninput: e => { s.location = e.target.value; markDirty(); } })),
      h("td", { class: "row-actions" },
        h("button", { class: "btn small", text: "Test login", onclick: () => testSwitch(s, testRow, testCell) }),
        h("button", { class: "btn small quiet", text: "Remove", "aria-label": `Remove ${s.address || "this switch"}`,
          onclick: () => { state.switches.splice(i, 1); markDirty(); renderSwitchRows(); } })),
    ), testRow);
  });
  $("#switchRows").replaceChildren(...rows);
  $("#switchEmpty").hidden = state.switches.length > 0;
  $("#switchShown").textContent = state.switchFind.trim() ? `Showing ${shown} of ${savedSwitches().length}.` : "";
}

function addSwitch(s) {
  state.switches.push({
    address: s?.address || "", type: s?.type || "",
    protocol: s?.protocol || state.settings.default_protocol || "telnet",
    building: s?.building || "", location: s?.location || "",
  });
}

function csvSplit(line) {
  const out = [];
  let cur = "", quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cur += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { out.push(cur); cur = ""; }
    else cur += ch;
  }
  out.push(cur);
  return out.map(s => s.trim());
}

// CSV/TSV with a header row (any column order), or "address, type, ssh or telnet, location, building"
function parseSwitchText(text) {
  const lines = text.split(/\r?\n/).filter(l => l.trim());
  const split = line => (line.includes("\t") ? line.split("\t").map(s => s.trim()) : csvSplit(line));
  let cols = { address: 0, type: 1, protocol: 2, location: 3, building: 4 };
  let start = 0;
  if (lines.length) {
    const head = split(lines[0]).map(c => c.toLowerCase());
    const isAddress = c => /^(address|ip|ip address|ipv4|management ip|mgmt ip)$/.test(c);
    if (head.some(isAddress)) {
      cols = {};
      head.forEach((c, n) => {
        if (isAddress(c)) cols.address ??= n;
        else if (/type|model|platform/.test(c)) cols.type ??= n;
        else if (/protocol|connect|method/.test(c)) cols.protocol ??= n;
        else if (/building|bldg|group/.test(c)) cols.building ??= n;
        else if (/location|closet|idf|room/.test(c)) cols.location ??= n;
      });
      start = 1;
    }
  }
  const list = [];
  let unknown = 0;
  for (const line of lines.slice(start)) {
    const parts = split(line);
    const get = key => (cols[key] !== undefined ? (parts[cols[key]] || "").trim() : "");
    const address = get("address");
    if (!address) continue;
    const rawType = get("type");
    const type = cleanType(rawType);
    if (rawType && !type) unknown++;
    const proto = get("protocol").toLowerCase();
    list.push({ address, type, protocol: proto === "ssh" || proto === "telnet" ? proto : undefined,
      location: get("location"), building: get("building") });
  }
  return { list, unknown };
}

function mergeSwitches(list) {
  let added = 0, updated = 0;
  state.switches = savedSwitches();
  const byAddress = new Map(state.switches.map(s => [s.address.toLowerCase(), s]));
  for (const s of list) {
    const existing = byAddress.get(s.address.toLowerCase());
    if (existing) {
      for (const [k, v] of Object.entries(s)) if (v) existing[k] = v;
      updated++;
    } else {
      addSwitch(s);
      byAddress.set(s.address.toLowerCase(), state.switches[state.switches.length - 1]);
      added++;
    }
  }
  return { added, updated };
}

function applySwitchText(text, source) {
  const { list, unknown } = parseSwitchText(text);
  if (!list.length) {
    notice("switchNotes", "error", [`No switches found in ${source}. Put one switch per line, starting with its address.`]);
    return false;
  }
  const { added, updated } = mergeSwitches(list);
  markDirty();
  renderSwitchRows();
  const parts = [];
  if (added) parts.push(`${added} added`);
  if (updated) parts.push(`${updated} already in the list and updated`);
  const msgs = [`Read ${plural(list.length, "switch", "switches")} from ${source}: ${parts.join(", ")}. Click Save switches to keep them.`];
  if (unknown) msgs.push(`${unknown} had a type this app can't check. Pick one in the Type column, or remove them.`);
  notice("switchNotes", unknown ? "warn" : "ok", msgs);
  return true;
}

function toggleBulk(force) {
  const box = $("#bulkBox");
  const show = force === undefined ? box.hidden : force;
  box.hidden = !show;
  $("#toggleBulk").setAttribute("aria-expanded", String(show));
  if (show) $("#bulkText").focus();
}

async function saveSwitches() {
  const { ok, data } = await api("/api/switches", { switches: state.switches });
  if (!ok) { notice("switchNotes", "error", data.problems || ["Couldn't save the switches."]); return false; }
  state.switches = data.switches;
  markDirty(false);
  renderSwitchRows();
  refreshSwitchSummary();
  notice("switchNotes", "ok", [`Saved ${plural(data.switches.length, "switch", "switches")}.`]);
  return true;
}

async function testSwitch(s, row, cell) {
  row.hidden = false;
  cell.className = "test-result";
  cell.textContent = `Logging into ${s.address} and reading its ports…`;
  const { ok, data } = await api("/api/test-switch", { switch: s, ...loginPayload() });
  cell.className = "test-result " + (ok && !data.problem ? "ok" : "error");
  if (!ok) { cell.textContent = `Couldn't check it: ${data.message || (data.problems || []).join(" ")}`; return; }
  const up = data.uptime_days !== null ? ` It has been up ${Math.round(data.uptime_days)} days.` : "";
  cell.textContent = `Logged in to ${data.name} and read ${plural(data.ports, "port")}: ${data.inactive} inactive` +
    (data.unsure ? `, ${data.unsure} unsure` : "") + `.${up} (${data.seconds}s)` + (data.problem ? ` ${data.problem}` : "");
}

function fillSettings() {
  const s = state.settings;
  $("#months").value = s.months;
  $("#defaultProtocol").value = s.default_protocol || "telnet";
  $("#parallel").value = s.parallel;
  $("#timeout").value = s.timeout;
  $("#typeHelp").replaceChildren(...Object.entries(state.types).flatMap(([value, label]) =>
    [h("dt", { text: label }), h("dd", { text: state.models[value] || "" })]));
}

async function saveSettings() {
  const { ok, data } = await api("/api/settings", {
    default_protocol: $("#defaultProtocol").value, parallel: $("#parallel").value, timeout: $("#timeout").value,
  });
  if (!ok) { notice("settingsNotes", "error", data.problems || ["Couldn't save the settings."]); return; }
  state.settings = { ...state.settings, ...data.settings };
  notice("settingsNotes", "ok", ["Saved the settings."]);
}

// ---------------------------------------------------------------- run
function loginPayload() {
  return {
    username: $("#user").value, password: $("#pass").value, secret: $("#secret").value,
    ssh_different: $("#sshDifferent").checked,
    ssh_username: $("#sshUser").value, ssh_password: $("#sshPass").value, ssh_secret: $("#sshSecret").value,
  };
}

function syncSshLogin() {
  $("#sshLogin").hidden = !$("#sshDifferent").checked;
  remember("sshDifferent", $("#sshDifferent").checked ? "1" : "0");
}

async function startRun() {
  if (state.dirty && !(await saveSwitches())) {
    notice("runNotes", "error", ["Fix the switch list below before running."]);
    return;
  }
  remember("user", $("#user").value);
  remember("sshUser", $("#sshUser").value);
  remember("scope", state.scope);
  const { ok, data } = await api("/api/run", { scope: state.scope, months: $("#months").value, ...loginPayload() });
  if (!ok) { notice("runNotes", "error", data.problems || ["Couldn't start."]); return; }
  notice("runNotes", null, []);
  $("#log").replaceChildren();
  state.logNext = 0;
  setRunning(true);
  poll();
}

function setRunning(running) {
  $("#runBtn").disabled = running;
  $("#runBtn").textContent = running ? "Checking…" : "Check ports";
  $("#stopBtn").hidden = !running;
  $("#stopBtn").disabled = false;
  $("#progressBox").hidden = !running;
}

async function poll() {
  clearTimeout(state.pollTimer);
  const { data } = await api(`/api/job?since=${state.logNext}`);
  if (!data || !data.state) { state.pollTimer = setTimeout(poll, 2000); return; }
  const log = $("#log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
  for (const line of data.lines) {
    log.append(h("li", { class: line.level }, h("time", { text: line.t }), h("span", { text: line.msg })));
  }
  if (atBottom) log.scrollTop = log.scrollHeight;
  state.logNext = data.next;
  const p = data.progress;
  $("#progressCount").textContent = p.total ? `${p.done} of ${p.total}` : "";
  $("#progressFill").style.width = p.total ? `${Math.round(100 * p.done / p.total)}%` : "0";
  if (data.state === "running") { state.pollTimer = setTimeout(poll, 1000); return; }
  setRunning(false);
  if (data.state === "done" && data.report_file) {
    await loadReport(data.report_file, true);
    $("#results").scrollIntoView({ behavior: reduceMotion() ? "auto" : "smooth" });
  }
}

// ---------------------------------------------------------------- results
async function loadReport(file, freshRun) {
  const { data } = await api("/api/report" + (file ? `?file=${encodeURIComponent(file)}` : ""));
  if (!data || !data.file) { state.report = null; state.ports = []; renderResults(); return; }
  state.report = data;
  state.ports = data.ports.map((p, i) => ({ ...p, _i: i }));
  if (freshRun) clearFilters(false);
  renderResults();
}

function filtersActive() {
  const f = state.filters;
  return !(f.results.size === 1 && f.results.has("INACTIVE")) || f.switch || f.building || f.search.trim();
}

// The wall always shows every port's real state; only building and search fade it
function onWall(p) {
  const f = state.filters;
  if (f.building && p.building !== f.building) return false;
  const q = f.search.trim().toLowerCase();
  if (!q) return true;
  return [p.port, p.description, p.vlan, p.why, p.how, p.status, p.switch, p.location].join(" ").toLowerCase().includes(q);
}

function matches(p) {
  const f = state.filters;
  return onWall(p) && (!f.results.size || f.results.has(p.result)) && (!f.switch || p.address === f.switch);
}

function visibleRows() {
  const rows = state.ports.filter(matches);
  const { key, dir } = state.sort;
  if (!key) return rows;
  return [...rows].sort((a, b) => {
    let cmp;
    if (key === "result") cmp = RESULTS.indexOf(a.result) - RESULTS.indexOf(b.result);
    else if (key === "last_used") cmp = (+a.days_unused || 0) - (+b.days_unused || 0);
    else if (key === "switch") cmp = natural(a.switch, b.switch) || natural(a.port, b.port);
    else if (key === "vlan") cmp = (+a.vlan || 1e9) - (+b.vlan || 1e9) || natural(a.vlan, b.vlan);
    else cmp = natural(a[key], b[key]);
    return cmp * dir;
  });
}

function runTime(created) {
  const d = new Date((created || "").replace(" ", "T"));
  return isNaN(d) ? created : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function renderResults() {
  const r = state.report;
  const has = !!r && (r.ports.length > 0 || r.switches.length > 0);
  $("#resultsEmpty").hidden = has;
  $("#resultsBody").hidden = !has;
  $("#downloadBtn").hidden = !has || !r.ports.length;
  if (!has) { $("#resultsMeta").textContent = ""; return; }
  const checked = r.switches.filter(s => s.ports > 0).length;
  const failed = r.switches.filter(s => !s.ports).length;
  $("#resultsMeta").textContent = `${plural(r.ports.length, "port")} on ${plural(checked, "switch", "switches")}, ` +
    `unused for ${r.months}+ months, checked ${runTime(r.created)}` +
    (failed ? `. ${plural(failed, "switch", "switches")} couldn't be checked.` : ".");

  // Result buttons: pick one or several
  const counts = new Map();
  state.ports.forEach(p => counts.set(p.result, (counts.get(p.result) || 0) + 1));
  $("#filterChips").replaceChildren(
    h("button", { class: "chip", "data-all": "1", onclick: () => { state.filters.results.clear(); renderFiltered(); } },
      "All ", h("b", { text: state.ports.length })),
    ...RESULTS.filter(k => counts.get(k)).map(k => h("button", { class: "chip", "data-r": k,
      onclick: () => { const s = state.filters.results; s.has(k) ? s.delete(k) : s.add(k); renderFiltered(); } },
    h("span", { class: "dot", "data-r": k }), RESULT_LABEL[k] + " ", h("b", { text: counts.get(k) }))));

  const f = state.filters;
  const sws = [...r.switches].filter(s => s.ports).sort((a, b) => natural(a.name, b.name));
  if (f.switch && !sws.some(s => s.address === f.switch)) f.switch = "";
  $("#switchFilter").replaceChildren(h("option", { value: "", text: "All switches" }),
    ...sws.map(s => h("option", { value: s.address, text: `${s.name} (${s.inactive} inactive)`, selected: s.address === f.switch })));
  const blds = [...new Set(r.switches.map(s => s.building).filter(Boolean))].sort(natural);
  if (f.building && !blds.includes(f.building)) f.building = "";
  $("#buildingFilter").replaceChildren(h("option", { value: "", text: "All buildings" }),
    ...blds.map(b => h("option", { value: b, text: b, selected: b === f.building })));
  $("#buildingFilter").closest(".field").hidden = blds.length < 2;
  $("#search").value = f.search;
  renderHead();
  renderFiltered();
}

const COLUMNS = [
  { key: "result", label: "Result" }, { key: "switch", label: "Switch" }, { key: "port", label: "Port" },
  { key: "description", label: "Description" }, { key: "status", label: "Status" }, { key: "vlan", label: "VLAN" },
  { key: "last_used", label: "Last used" }, { key: "why", label: "Why", nosort: true },
];

function renderHead() {
  $("#resultsHead").replaceChildren(...COLUMNS.map(c => {
    const th = h("th", { text: c.label });
    if (c.nosort) return th;
    th.tabIndex = 0;
    th.dataset.sort = c.key;
    if (state.sort.key === c.key) th.setAttribute("aria-sort", state.sort.dir > 0 ? "ascending" : "descending");
    const go = () => { state.sort = { key: c.key, dir: state.sort.key === c.key ? -state.sort.dir : 1 }; renderHead(); renderTable(); };
    th.addEventListener("click", go);
    th.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    return th;
  }));
}

function renderFiltered() {
  const f = state.filters;
  $$("#filterChips .chip").forEach(chip => {
    chip.setAttribute("aria-pressed", String(chip.dataset.all ? f.results.size === 0 : f.results.has(chip.dataset.r)));
  });
  $("#switchFilter").value = f.switch;
  $("#clearFilters").hidden = !filtersActive();
  renderWall();
  renderTable();
}

function clearFilters(render = true) {
  state.filters = { results: new Set(["INACTIVE"]), switch: "", building: "", search: "" };
  if (render) renderResults();
}

function renderWall() {
  const r = state.report;
  const bySwitch = new Map();
  state.ports.forEach(p => { if (!bySwitch.has(p.address)) bySwitch.set(p.address, []); bySwitch.get(p.address).push(p); });
  const tiles = r.switches.map(s => {
    const where = [s.building, s.location].filter(Boolean).join(", ");
    if (!s.ports) {
      return h("div", { class: "tile failed" + (state.filters.building && s.building !== state.filters.building ? " dimmed" : "") },
        h("div", { class: "tile-top" }, h("span", { class: "tile-name", text: s.name })),
        h("div", { class: "tile-where", text: where ? `${where}, ${s.address}` : s.address }),
        h("div", { class: "tile-problem", text: `Couldn't check: ${s.problem}` }));
    }
    const ports = bySwitch.get(s.address) || [];
    const bands = [];
    for (let i = 0; i < ports.length; i += PORTS_PER_BAND) {
      bands.push(h("div", { class: "band" }, ports.slice(i, i + PORTS_PER_BAND).map(p =>
        h("span", { class: "led" + (onWall(p) ? "" : " dim"), "data-r": p.result,
          title: `${p.port}${p.description ? " " + p.description : ""}\n${RESULT_LABEL[p.result]}: ${p.last_used}` }))));
    }
    const selected = state.filters.switch === s.address;
    const outside = state.filters.building && s.building !== state.filters.building;
    return h("button", { class: "tile" + (outside ? " dimmed" : ""), "aria-pressed": String(selected),
      title: s.problem || `${s.name}: show only this switch's ports`,
      onclick: () => { state.filters.switch = selected ? "" : s.address; renderFiltered(); } },
    h("div", { class: "tile-top" }, h("span", { class: "tile-name", text: s.name }),
      h("span", { class: "tile-count" }, h("b", { text: s.inactive }), ` of ${s.ports} unused`)),
    h("div", { class: "tile-where", text: where ? `${where}, ${s.address}` : s.address }),
    h("div", { class: "bands" }, bands),
    s.problem ? h("div", { class: "tile-problem", text: s.problem }) : null);
  });
  $("#wall").replaceChildren(...tiles);
}

function cell(p, key) {
  switch (key) {
    case "result":
      return h("td", {}, h("span", { class: "result-cell", "data-r": p.result },
        h("span", { class: "dot", "data-r": p.result }), RESULT_LABEL[p.result]));
    case "switch":
      return h("td", { class: "switch-cell" }, p.switch, h("small", { text: p.location || p.address }));
    case "port":
      return h("td", { class: "mono", text: p.port });
    case "status":
      return h("td", { class: p.status === "disabled" ? "status-disabled" : "", text: p.status === "disabled" ? "shut down" : p.status });
    case "why":
      return h("td", { class: "note", title: p.how || "", text: p.why });
    default:
      return h("td", { text: p[key] ?? "" });
  }
}

function renderTable() {
  const visible = visibleRows();
  $("#resultRows").replaceChildren(...visible.map(p => h("tr", {}, COLUMNS.map(c => cell(p, c.key)))));
  $("#noMatches").hidden = visible.length > 0;
  const total = state.ports.length;
  $("#shownCount").textContent = visible.length === total ? `Showing all ${total} ports.` : `Showing ${visible.length} of ${total} ports.`;
  $("#downloadBtn").textContent = `Download Excel (${plural(visible.length, "port")})`;
  $("#downloadBtn").disabled = visible.length === 0;
}

async function downloadExcel() {
  const rows = visibleRows();
  const f = state.filters;
  const switches = state.report.switches.filter(s =>
    (!f.switch || s.address === f.switch) && (!f.building || s.building === f.building)).map(s => s.address);
  const btn = $("#downloadBtn");
  btn.disabled = true;
  try {
    const res = await fetch("/api/excel", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: state.report.file, rows: rows.map(p => p._i), switches }) });
    if (!res.ok) throw new Error(res.statusText);
    const blob = await res.blob();
    const link = h("a", { href: URL.createObjectURL(blob), download: res.headers.get("X-Filename") || "ports.xlsx" });
    document.body.append(link);
    link.click();
    setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
  } catch (e) {
    notice("runNotes", "error", ["Couldn't make the Excel file. Is the RunAnalyzer window still open?"]);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- start-up
async function init() {
  const { data } = await api("/api/state");
  if (!data || !data.settings) {
    document.body.prepend(h("p", { class: "notice error", text: "Couldn't load the app's data. Restart RunAnalyzer.bat and refresh this page." }));
    return;
  }
  state.settings = data.settings;
  state.switches = data.switches;
  state.types = data.types;
  state.models = data.models || {};
  state.scope = recall("scope") || "";

  fillSettings();
  renderSwitchRows();
  refreshSwitchSummary();
  toggleEditor(state.switches.length === 0);
  $("#user").value = recall("user") || "";
  $("#sshUser").value = recall("sshUser") || "";
  $("#sshDifferent").checked = recall("sshDifferent") === "1" && !$("#sshDifferentRow").hidden;
  syncSshLogin();

  $("#scope").addEventListener("change", e => { state.scope = e.target.value; remember("scope", state.scope); refreshScopeHint(); });
  $("#months").addEventListener("input", refreshScopeHint);
  $("#toggleEditor").addEventListener("click", () => toggleEditor());
  $("#switchFile").addEventListener("change", e => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { applySwitchText(String(reader.result), file.name); e.target.value = ""; };
    reader.readAsText(file);
  });
  $("#toggleBulk").addEventListener("click", () => toggleBulk());
  $("#addBulk").addEventListener("click", () => {
    if (applySwitchText($("#bulkText").value, "the pasted list")) { $("#bulkText").value = ""; toggleBulk(false); }
  });
  $("#addSwitch").addEventListener("click", () => {
    addSwitch(); markDirty(); state.switchFind = ""; $("#switchFind").value = ""; renderSwitchRows();
    const inputs = $$("#switchRows input.mono");
    inputs[inputs.length - 1]?.focus();
  });
  $("#switchFind").addEventListener("input", e => { state.switchFind = e.target.value; renderSwitchRows(); });
  $("#saveSwitches").addEventListener("click", saveSwitches);
  $("#saveSettings").addEventListener("click", saveSettings);
  $("#sshDifferent").addEventListener("change", syncSshLogin);
  $("#runBtn").addEventListener("click", startRun);
  $("#stopBtn").addEventListener("click", async () => { $("#stopBtn").disabled = true; await api("/api/stop", {}); });
  $("#switchFilter").addEventListener("change", e => { state.filters.switch = e.target.value; renderFiltered(); });
  $("#buildingFilter").addEventListener("change", e => { state.filters.building = e.target.value; renderFiltered(); });
  $("#search").addEventListener("input", e => { state.filters.search = e.target.value; renderFiltered(); });
  $("#clearFilters").addEventListener("click", () => clearFilters());
  $("#downloadBtn").addEventListener("click", downloadExcel);
  window.addEventListener("beforeunload", e => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

  if (data.job && data.job.state === "running") { setRunning(true); poll(); }
  if (data.latest_report) loadReport(data.latest_report); else renderResults();
}

init();
