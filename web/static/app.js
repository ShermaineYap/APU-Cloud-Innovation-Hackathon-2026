"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "–" : Number(n).toLocaleString("en-MY", { maximumFractionDigits: d, minimumFractionDigits: d }));
const INK = "#1D2A30", MUTED = "#5C6B71", RULE = "#C3CAC5", TEAL = "#1F7A6D", BLUE = "#3B6EA8", AMBER = "#B87412", RED = "#A23B2C";
const BLOCKS = { AUD: "Auditoriums", B: "Block B · Classrooms", TL: "Tech Labs" };
const STATUS_TEXT = { in_use: "In use", empty_cooled: "Cooled, empty", stale_air: "CO₂ high", idle: "Empty", offline: "No data" };

if (window.Chart) {
  Chart.defaults.font.family = "Barlow, system-ui, sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.color = MUTED;
  Chart.defaults.borderColor = "rgba(195,202,197,.6)";
  Chart.defaults.plugins.legend.display = false;
}

const charts = new Map();
function chart(canvas, config) {
  if (!window.Chart || !canvas) return;
  charts.get(canvas)?.destroy();
  charts.set(canvas, new Chart(canvas, config));
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3200);
}

let mode = {};
let rooms = [];

/* ---------- status bar + lead ---------- */
function renderStatus(m) {
  const live = m.live_node_online;
  $("#status").innerHTML = `<span class="dot ${live ? "on" : ""}"></span>${
    live ? `Live sensor in ${esc(m.live_room)} reporting` : `Live sensor in ${esc(m.live_room)} offline, showing simulated fleet`
  }`;
  $("#foot-mode").textContent =
    `Data: 1 physical node in ${m.live_room} + 20 real APU rooms on their real timetable, sensors simulated (labelled). Store: ${m.backend === "bigquery" ? "BigQuery" : "local DuckDB"}. ` +
    `AI: ${m.gemini ? `Gemini ${m.model}` : "offline mode (no Gemini key)"}. Energy price assumption: RM ${m.tariff_rm_per_kwh}/kWh.`;
  $("#ask-mode").textContent = m.gemini
    ? "Gemini writes the SQL, CampusPulse checks it is read-only, the warehouse runs it."
    : "Offline mode: tested questions only. Add a Gemini key to ask anything.";
}

function renderLead(brief, kpis) {
  const now = new Date();
  $("#when").textContent = `Morning brief for ${now.toLocaleDateString("en-MY", { weekday: "long", day: "numeric", month: "long" })}`;
  $("#headline").textContent = brief.headline;
  $("#figures").innerHTML =
    `In the ${esc(kpis.window)}, <b>${fmt(kpis.wasted_kwh)} kWh</b> (about <b>RM ${fmt(kpis.wasted_rm)}</b>) went to cooling empty rooms, ` +
    `<b>${fmt(kpis.stale_air_hours)}</b> occupied room-hours had stale air, and booked rooms were <b>${fmt(kpis.avg_utilisation * 100)}%</b> full on average. ` +
    `Fleet data confidence is <b>${fmt(kpis.fleet_confidence)}/100</b>.`;
  $("#brief-summary").textContent = brief.summary || "";
  $("#brief-source").textContent = `Brief written by ${brief.source}.`;
}

/* ---------- floor plan ---------- */
function renderPlan(list) {
  rooms = list;
  const plan = $("#plan");
  plan.innerHTML = Object.entries(BLOCKS).map(([b, title]) => {
    const rs = list.filter((r) => r.block === b).sort((x, y) => x.row - y.row || x.col - y.col);
    return `<div class="block"><h3>${esc(title)}</h3><div class="rooms">${rs.map(roomCell).join("")}</div></div>`;
  }).join("");
  plan.querySelectorAll(".room").forEach((el) => el.addEventListener("click", () => openRoom(el.dataset.id)));
}

function roomCell(r) {
  const fill = r.people != null && r.capacity ? Math.min(100, (r.people / r.capacity) * 100) : 0;
  const isLive = r.room_id === mode.live_room && r.simulated === false;
  const isCam = r.people_source === "camera";
  const reading = r.status === "offline"
    ? `<span>Silent ${r.minutes_ago != null ? fmt(r.minutes_ago) + " min" : ""}</span>`
    : `${r.people != null ? `<span><b>${fmt(r.people)}</b>/${r.capacity}</span>` : `<span><b>${r.occupied ? "Motion" : "No motion"}</b></span>`}
       <span class="co2"><b>${fmt(r.co2_ppm)}</b> ppm</span>
       <span><b>${fmt(r.temp_c, 1)}</b>°C</span>`;
  const label = `${r.room_id} ${r.name}: ${STATUS_TEXT[r.status]}` +
    (r.people != null ? `, ${r.people} of ${r.capacity} seats${isCam ? " counted by camera" : ""}` : "") +
    (r.co2_ppm ? `, CO2 ${Math.round(r.co2_ppm)} ppm` : "") + `, data confidence ${r.confidence}`;
  return `<button type="button" class="room ${r.status}${picked.has(r.room_id) ? " picked" : ""}" data-id="${esc(r.room_id)}" aria-label="${esc(label)}">
      ${isCam ? `<span class="live"><span class="dot on"></span>camera count</span>`
        : isLive ? `<span class="live"><span class="dot on"></span>live</span>` : `<span class="tag">${esc(STATUS_TEXT[r.status])}</span>`}
      <span class="code">${esc(r.room_id)}</span>
      <span class="name">${esc(r.name)}</span>
      ${r.confidence_level === "low" ? `<span class="lowconf">Low data confidence</span>` : ""}
      <span class="reading">${reading}</span>
      ${r.status === "in_use" || r.status === "stale_air" ? `<span class="fill" style="width:${fill}%"></span>` : ""}
    </button>`;
}

/* ---------- actions ---------- */
const decided = new Map();
const whatIf = new Set();
let picked = new Set();
let lastKpis = null;
const CUTS_EMPTY_COOLING = new Set(["occupancy_linked_ac", "after_hours_ac"]);

function renderActions(actions, brief) {
  const why = Object.fromEntries((brief.items || []).map((i) => [i.id, i]));
  const ordered = [...actions.filter((a) => why[a.id]), ...actions.filter((a) => !why[a.id])].slice(0, 8);
  const list = $("#actions");
  list.innerHTML = ordered.map((a) => actionItem(a, why[a.id])).join("") || `<li class="empty">No changes recommended right now.</li>`;
  list.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", () => {
    if (d.open) drawEvidence(d, ordered.find((a) => a.id === d.dataset.id));
  }, { once: false }));
  list.querySelectorAll("[data-decide]").forEach((b) => b.addEventListener("click", () => decide(b.dataset.id, b.dataset.decide, b)));
  list.querySelectorAll("[data-whatif]").forEach((b) => b.addEventListener("click", () => {
    const id = b.dataset.whatif;
    whatIf.has(id) ? whatIf.delete(id) : whatIf.add(id);
    renderActions(lastBrief.actions, lastBrief.brief);
  }));
  renderProjection(actions);
}

/* What if: the week recalculated with the selected and approved changes in place. */
function renderProjection(actions) {
  const chosen = actions.filter((a) => decided.get(a.id) === "approved" || (whatIf.has(a.id) && decided.get(a.id) !== "dismissed"));
  picked = new Set(chosen.flatMap((a) => a.rooms));
  document.querySelectorAll(".room").forEach((el) => el.classList.toggle("picked", picked.has(el.dataset.id)));
  const box = $("#projection");
  if (!chosen.length) {
    box.innerHTML = `<p class="hint">Press <b>What if</b> on any change to see this week recalculated with it in place. Approved changes count automatically.</p>`;
    return;
  }
  const tariff = mode.tariff_rm_per_kwh || 0.5;
  const kwh = chosen.reduce((s, a) => s + a.kwh_per_week, 0);
  const now = lastKpis?.wasted_kwh || 0;
  const cut = chosen.filter((a) => CUTS_EMPTY_COOLING.has(a.kind)).reduce((s, a) => s + Math.max(0, a.kwh_per_week), 0);
  const after = Math.max(0, now - cut);
  const approvedN = chosen.filter((a) => decided.get(a.id) === "approved").length;
  const comfort = chosen.filter((a) => a.kind === "air_quality").length;
  const fixes = chosen.filter((a) => a.kind === "data_reliability").length;
  const notes = [comfort ? `${comfort} stale-air fix${comfort > 1 ? "es" : ""} (comfort, not energy)` : "",
                 fixes ? `${fixes} data fix${fixes > 1 ? "es" : ""} that raise confidence` : ""].filter(Boolean);
  box.innerHTML = `
    <p class="proj-line"><b>${chosen.length} change${chosen.length > 1 ? "s" : ""}</b>${approvedN ? ` (${approvedN} approved)` : ""} in place:
      saves <b>${fmt(kwh)} kWh</b> a week, about <b>RM ${fmt(kwh * tariff)}</b>, or RM ${fmt(kwh * tariff * 52)} a year if sustained.</p>
    <div class="proj-bars" role="img" aria-label="Empty-room cooling: ${fmt(now)} kWh now, ${fmt(after)} kWh with these changes">
      <span class="lbl">Empty-room cooling now</span><span class="track"><span class="meter now" style="width:100%"></span></span><span class="val">${fmt(now)} kWh</span>
      <span class="lbl">With these changes</span><span class="track"><span class="meter after" style="width:${now ? (after / now) * 100 : 0}%"></span></span><span class="val">${fmt(after)} kWh</span>
    </div>
    ${notes.length ? `<p class="hint">${notes.join(" · ")}.</p>` : ""}
    <p class="hint">Rooms touched by these changes are outlined on the floor plan.</p>`;
}

function impactLine(a) {
  const parts = [];
  if (a.kwh_per_week > 0.5) parts.push(`<span>Saves <b>${fmt(a.kwh_per_week)} kWh</b> / week (≈ RM ${fmt(a.rm_per_week)})</span>`);
  if (a.kwh_per_week < -0.5) parts.push(`<span>Costs <b>${fmt(-a.kwh_per_week)} kWh</b> / week more</span>`);
  if (a.comfort) parts.push(`<span>${esc(a.comfort)}</span>`);
  parts.push(`<span>Confidence <span class="conf ${a.confidence_level}">${a.confidence_level} (${a.confidence})</span></span>`);
  return parts.join("");
}

function actionItem(a, w) {
  const state = decided.get(a.id);
  return `<li class="action" data-kind="${esc(a.kind)}" ${state ? `data-decided="${state}"` : ""}>
    <div class="action-top">
      <h3>${esc(a.title)}</h3>
      <div class="impact">${impactLine(a)}</div>
    </div>
    <p class="why">${esc(w?.why_now || a.summary)}</p>
    <details data-id="${esc(a.id)}">
      <summary>Show the evidence</summary>
      <div class="evidence">
        ${w?.first_step ? `<p class="first-step"><b>First step:</b> ${esc(w.first_step)}</p>` : ""}
        ${w?.why_now && w.why_now !== a.summary ? `<p class="first-step">${esc(a.summary)}</p>` : ""}
        <p class="caption" data-caption="${esc(a.id)}"></p>
        <canvas aria-label="${esc(a.chart?.label || "Evidence chart")}" role="img"></canvas>
        ${rowsTable(a.evidence_rows)}
        <pre class="sql">${esc(a.evidence_sql)}</pre>
      </div>
    </details>
    ${state
      ? `<p class="decided-note">${state === "approved" ? "Approved and logged" : "Dismissed"}.</p>`
      : `<div class="decide">
          <button type="button" class="solid" data-decide="approved" data-id="${esc(a.id)}">Approve</button>
          <button type="button" class="ghost" data-decide="dismissed" data-id="${esc(a.id)}">Dismiss</button>
          <button type="button" class="ghost whatif" data-whatif="${esc(a.id)}" aria-pressed="${whatIf.has(a.id)}">What if</button>
        </div>`}
  </li>`;
}

async function loadCaption(details, a) {
  const p = $("[data-caption]", details);
  if (!p || p.dataset.loaded) return;
  p.dataset.loaded = "1";
  p.textContent = "Reading the chart…";
  try {
    const c = await api(`/api/actions/${encodeURIComponent(a.id)}/caption`);
    p.innerHTML = c.caption ? `${esc(c.caption)} <span class="src">${esc(c.source.startsWith("Gemini") ? c.source : "Summary from the chart's numbers")}</span>` : "";
  } catch {
    p.textContent = "";
  }
}

function drawEvidence(details, a) {
  if (!a?.chart?.x) return;
  loadCaption(details, a);
  const c = a.chart;
  const color = { air_quality: AMBER, data_reliability: RED, right_size: TEAL }[a.kind] || BLUE;
  const datasets = [{ label: c.label, data: c.y, backgroundColor: color, borderColor: color, borderRadius: 2 }];
  if (c.ref != null) datasets.push({ type: "line", label: "Reference", data: c.x.map(() => c.ref), borderColor: INK, borderDash: [5, 4], borderWidth: 1.2, pointRadius: 0 });
  chart($("canvas", details), {
    type: "bar",
    data: { labels: c.x, datasets },
    options: { responsive: true, maintainAspectRatio: false, plugins: { title: { display: true, text: c.label, align: "start", color: INK } },
      scales: { x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true } }, y: { beginAtZero: true } } },
  });
  $("canvas", details).parentElement.style.minHeight = "200px";
}

async function decide(id, status, btn) {
  btn.disabled = true;
  try {
    await api(`/api/actions/${id}/decide`, { method: "POST", body: JSON.stringify({ status }) });
    decided.set(id, status);
    toast(status === "approved" ? "Approved. Logged to the decision table." : "Dismissed. Logged to the decision table.");
    renderActions(lastBrief.actions, lastBrief.brief);
  } catch (e) {
    btn.disabled = false;
    toast(`Could not save the decision: ${e.message}`);
  }
}

function rowsTable(rows) {
  if (!rows?.length) return "";
  const cols = Object.keys(rows[0]).slice(0, 7);
  const isNum = (c) => rows.every((r) => r[c] == null || typeof r[c] === "number");
  return `<div class="table-scroll"><table><thead><tr>${cols.map((c) => `<th class="${isNum(c) ? "num" : ""}">${esc(c.replaceAll("_", " "))}</th>`).join("")}</tr></thead>
    <tbody>${rows.slice(0, 12).map((r) => `<tr>${cols.map((c) => `<td class="${isNum(c) ? "num" : ""}">${esc(typeof r[c] === "number" ? fmt(r[c], Number.isInteger(r[c]) ? 0 : 2) : r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

/* ---------- room drawer ---------- */
async function openRoom(id) {
  const r = rooms.find((x) => x.room_id === id);
  const drawer = $("#drawer");
  $("#drawer-body").innerHTML = `<h2 id="drawer-title">${esc(id)} · ${esc(r?.name || "")}</h2><p class="sub">Loading the last 48 hours…</p>`;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#scrim").hidden = false;
  $("#drawer-close").focus();
  try {
    const d = await api(`/api/rooms/${encodeURIComponent(id)}`);
    const room = d.room;
    $("#drawer-body").innerHTML = `
      <h2 id="drawer-title">${esc(room.room_id)} · ${esc(room.name)}</h2>
      <p class="sub">${esc(room.kind)} room, ${room.capacity} seats, ${room.ac_kw} kW cooling, gateway ${esc(room.gateway_id)}</p>
      <div class="facts">
        <span>Status: <b>${esc(STATUS_TEXT[r?.status] || "–")}</b></span>
        <span>Data confidence: <b class="conf ${r?.confidence_level}">${r?.confidence ?? "–"}</b></span>
        <span>Head count: <b>${r?.people_source === "camera" ? `camera (YOLO on the device, ${esc(r.camera_device)})` : r?.people_source === "sensor" ? "sensor node" : "simulated estimate"}</b></span>
        ${r?.issues?.length ? `<span>${esc(r.issues.join("; "))}</span>` : ""}
      </div>
      <div class="chart"><h3>People: last 48 hours and the next 24 forecast</h3><canvas id="c-people" role="img" aria-label="Occupancy and forecast"></canvas>
        <p class="hint">Forecast: ${esc(d.forecast_method)}. Shaded band is the 10th to 90th percentile of past weeks.</p></div>
      <div class="chart"><h3>CO₂ peak per hour (limit ${mode.co2_limit_ppm} ppm)</h3><canvas id="c-co2" role="img" aria-label="CO2 per hour"></canvas></div>
      <div class="chart"><h3>Energy per hour</h3><canvas id="c-kwh" role="img" aria-label="Energy per hour"></canvas>
        <p class="hint">Blue: AC on with the room empty. Teal: AC on with people in. Grey: AC off.</p></div>
      ${d.anomalies.length ? `<div class="chart"><h3>Unusual energy hours (last 4 weeks)</h3>${rowsTable(d.anomalies)}</div>` : ""}`;
    drawRoomCharts(d, room);
  } catch (e) {
    $("#drawer-body").innerHTML += `<p>Could not load this room: ${esc(e.message)}</p>`;
  }
}

function hourLabel(iso) {
  const d = new Date(iso);
  return d.toLocaleString("en-MY", { weekday: "short", hour: "numeric" });
}

function drawRoomCharts(d, room) {
  const rec = d.recent, fc = d.forecast;
  const labels = [...rec.hour, ...fc.hour].map(hourLabel);
  const pad = (arr, before, after) => [...Array(before).fill(null), ...arr, ...Array(after).fill(null)];
  chart($("#c-people"), {
    type: "line",
    data: { labels, datasets: [
      { label: "Forecast high", data: pad(fc.high, rec.hour.length, 0), borderWidth: 0, pointRadius: 0, backgroundColor: "rgba(31,122,109,.14)", fill: "+1" },
      { label: "Forecast low", data: pad(fc.low, rec.hour.length, 0), borderWidth: 0, pointRadius: 0, fill: false },
      { label: "Forecast", data: pad(fc.forecast, rec.hour.length, 0), borderColor: TEAL, borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5 },
      { label: "People", data: pad(rec.avg_people, 0, fc.hour.length), borderColor: INK, pointRadius: 0, borderWidth: 2 },
      { label: "Seats", data: labels.map(() => room.capacity), borderColor: RULE, pointRadius: 0, borderWidth: 1 },
    ] },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } }, y: { beginAtZero: true } } },
  });
  const recLabels = rec.hour.map(hourLabel);
  chart($("#c-co2"), {
    type: "line",
    data: { labels: recLabels, datasets: [
      { label: "Peak CO2", data: rec.max_co2_ppm, borderColor: AMBER, pointRadius: 0, borderWidth: 2 },
      { label: "Limit", data: recLabels.map(() => mode.co2_limit_ppm), borderColor: INK, borderDash: [5, 4], pointRadius: 0, borderWidth: 1 },
    ] },
    options: { responsive: true, maintainAspectRatio: false, scales: { x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } } } },
  });
  chart($("#c-kwh"), {
    type: "bar",
    data: { labels: recLabels, datasets: [{
      label: "kWh", data: rec.kwh,
      backgroundColor: rec.kwh.map((v, i) => (rec.ac_on_frac[i] >= 0.5 && !(rec.avg_people[i] > 0) ? BLUE : (rec.ac_on_frac[i] >= 0.5 ? TEAL : RULE))),
    }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { tooltip: { callbacks: { afterLabel: (ctx) => (rec.ac_on_frac[ctx.dataIndex] >= 0.5 && !(rec.avg_people[ctx.dataIndex] > 0) ? "AC on, room empty" : "") } } },
      scales: { x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } }, y: { beginAtZero: true } } },
  });
}

function closeRoom() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").hidden = true;
}
$("#drawer-close").addEventListener("click", closeRoom);
$("#scrim").addEventListener("click", closeRoom);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeRoom());

/* ---------- ask ---------- */
async function askQuestion(q) {
  const box = $("#answer");
  const btn = $("#ask-form button");
  btn.disabled = true;
  box.hidden = false;
  box.innerHTML = `<p class="text">Querying the warehouse…</p>`;
  try {
    const a = await api("/api/ask", { method: "POST", body: JSON.stringify({ question: q }) });
    box.innerHTML = `
      <p class="text">${esc(a.answer)}</p>
      <p class="src">Answered by ${esc(a.source)}${a.attempts?.length > 1 ? `, after ${a.attempts.length} queries` : ""}.</p>
      <div>${a.chart ? `<canvas id="ask-chart" role="img" aria-label="Answer chart"></canvas>` : ""}${rowsTable(a.rows)}</div>
      <div>${a.sql ? `<pre class="sql">${esc(a.sql)}</pre>` : ""}</div>`;
    if (a.chart) {
      chart($("#ask-chart"), {
        type: a.chart.type,
        data: { labels: a.chart.x, datasets: [{ label: a.chart.y_field, data: a.chart.y, backgroundColor: BLUE, borderColor: BLUE, pointRadius: 0, borderWidth: 2 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { title: { display: true, text: a.chart.y_field.replaceAll("_", " "), align: "start", color: INK } },
          scales: { x: { ticks: { maxRotation: 0, autoSkip: true }, grid: { display: false } }, y: { beginAtZero: true } } },
      });
      $("#ask-chart").parentElement.style.minHeight = "260px";
    }
  } catch (e) {
    box.innerHTML = `<p class="text">The question did not go through: ${esc(e.message)}. Try one of the suggestions.</p>`;
  } finally {
    btn.disabled = false;
  }
}

$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value.trim();
  if (q.length >= 3) askQuestion(q);
});

async function loadSuggestions() {
  const qs = await api("/api/suggestions");
  $("#suggestions").innerHTML = qs.slice(0, 8).map((q) => `<button type="button" class="chip-btn">${esc(q)}</button>`).join("");
  $("#suggestions").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    $("#question").value = b.textContent;
    askQuestion(b.textContent);
  }));
}

/* ---------- network ---------- */
async function loadNetwork() {
  const n = await api("/api/network");
  const nodes = n.nodes.slice(0, 9);
  const outageGws = [...new Set(n.outages.map((o) => o.gateway_id))];
  $("#network").innerHTML = `<div class="net-grid">${nodes.map((h) => `
      <div class="node">
        <span class="score ${h.level}" aria-label="Confidence ${h.confidence}">${h.confidence}</span>
        <div><div class="id">${esc(h.room_id)} · ${esc(h.device_id)}</div>
          <div class="meta">${fmt(h.delivery_ratio * 100, 1)}% delivered · ${fmt(h.avg_rssi_dbm)} dBm · ${esc(h.gateway_id)}${h.simulated ? "" : " · physical node"}</div></div>
        ${h.issues.length ? `<div class="issue">${esc(h.issues.join("; "))}</div>` : ""}
      </div>`).join("")}</div>
    <p class="outages">${outageGws.length
      ? `Gateway silence this week: ${outageGws.map((g) => `${esc(g)} (${n.outages.filter((o) => o.gateway_id === g).length} h)`).join(", ")}. Decisions for those hours are marked lower confidence.`
      : "No gateway outages in the last 7 days."} Showing the ${nodes.length} least trusted of ${n.nodes.length} nodes.</p>`;
}

/* ---------- boot ---------- */
let lastBrief = null;

async function loadOverview() {
  const o = await api("/api/overview");
  mode = o.mode;
  renderStatus(o.mode);
  renderPlan(o.rooms);
  const cams = o.rooms.filter((r) => r.people_source === "camera").map((r) => r.room_id);
  const sensorLive = o.rooms.some((r) => r.simulated === false);
  if (cams.length) {
    $("#status").innerHTML = `<span class="dot on"></span>${sensorLive ? `Live sensor in ${esc(o.mode.live_room)} reporting · ` : ""}` +
      `Camera counting people in ${esc(cams.join(", "))}${sensorLive ? "" : "; other readings simulated"}`;
  }
  return o;
}

async function loadBrief(refresh = false) {
  const btn = $("#refresh");
  btn.disabled = true;
  try {
    const [o, b] = await Promise.all([loadOverview(), api(`/api/brief${refresh ? "?refresh=true" : ""}`)]);
    lastBrief = b;
    lastKpis = o.kpis;
    renderLead(b.brief, o.kpis);
    renderActions(b.actions, b.brief);
    if (refresh) toast("Brief refreshed from the latest data.");
  } finally {
    btn.disabled = false;
  }
}

$("#refresh").addEventListener("click", () => loadBrief(true).catch((e) => toast(e.message)));

async function restoreDecisions() {
  try {
    const log = await api("/api/decisions");
    for (const d of log) if (!decided.has(d.action_id)) decided.set(d.action_id, d.status); // newest first
  } catch { /* the log is optional */ }
}

restoreDecisions().then(() => loadBrief()).catch((e) => { $("#headline").textContent = "CampusPulse could not reach its data service."; $("#figures").textContent = e.message; });
loadSuggestions().catch(() => {});
loadNetwork().catch(() => {});
setInterval(() => loadOverview().catch(() => {}), 30000);
setInterval(() => loadNetwork().catch(() => {}), 60000);
