"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "–" : Number(n).toLocaleString("en-MY", { maximumFractionDigits: d, minimumFractionDigits: d }));
const INK = "#1D2A30", MUTED = "#5C6B71", RULE = "#C3CAC5", TEAL = "#1F7A6D", BLUE = "#3B6EA8", AMBER = "#B87412", RED = "#A23B2C";
const BLOCKS = { AUD: "Auditoriums", B: "Block B · Classrooms", TL: "Tech Labs" };
const AC_MODE_TEXT = { schedule: "on building schedule", after_hours_off: "off after hours", occupancy: "follows occupancy" };
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
    `AI: ${m.gemini ? `Gemini ${m.model}` : "offline mode (no Gemini key)"}. Assumptions: energy RM ${m.tariff_rm_per_kwh}/kWh, grid carbon ${m.grid_kg_co2e_per_kwh} kg CO₂e/kWh.`;
  $("#ask-mode").textContent = m.gemini
    ? "Gemini writes the SQL, CampusPulse checks it is read-only, the warehouse runs it."
    : "Offline mode: tested questions only. Add a Gemini key to ask anything.";
}

// "template (Gemini error: 503 UNAVAILABLE: long text)" -> "the template (Gemini busy, 503)"
function shortSource(src) {
  const m = String(src || "").match(/Gemini error: (\d{3})/);
  if (m) return `the template, from the engine's numbers (Gemini unavailable just now, ${m[1]})`;
  return String(src || "template").replace(/ \(Gemini not configured\)/, " (Gemini not configured)");
}

function kpiTiles(items) {
  return items.map(([v, label, cls]) => `<div class="kpi ${cls || ""}"><b>${v}</b><span>${esc(label)}</span></div>`).join("");
}

function renderLead(brief, kpis) {
  const now = new Date();
  $("#when").textContent = `Morning brief for ${now.toLocaleDateString("en-MY", { weekday: "long", day: "numeric", month: "long" })}`;
  $("#headline").textContent = brief.headline;
  $("#figures").innerHTML = kpiTiles([
    [`RM ${fmt(kpis.wasted_rm)}`, "spent cooling empty rooms, last 7 days"],
    [`${fmt(kpis.wasted_share * 100)}%`, "of all cooling went to empty rooms"],
    [`${fmt((kpis.wasted_kg_co2e || 0) / 1000, 1)} t`, "CO₂e from that cooling"],
    [`${fmt(kpis.stale_air_hours)} h`, "of classes with stale air (CO₂ over limit)"],
  ]);
  $("#brief-summary").textContent = brief.summary || "";
  $("#brief-source").textContent = `Brief written by ${shortSource(brief.source)}.`;
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
      ${r.ac_mode && r.ac_mode !== "schedule" ? `<span class="acmode" title="Set by an approved change">AC ${esc(AC_MODE_TEXT[r.ac_mode] || r.ac_mode)}</span>` : ""}
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
  const pending = ordered.filter((a) => !decided.has(a.id)).length;
  $("#actions-count").hidden = !pending;
  $("#actions-count").textContent = pending;
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
      saves <b>${fmt(kwh)} kWh</b> a week, about <b>RM ${fmt(kwh * tariff)}</b>, or RM ${fmt(kwh * tariff * 52)} and
      <b>${fmt(kwh * (mode.grid_kg_co2e_per_kwh || 0.758) * 52 / 1000, 1)} t CO₂e</b> a year if sustained.</p>
    <div class="proj-bars" role="img" aria-label="Empty-room cooling: ${fmt(now)} kWh now, ${fmt(after)} kWh with these changes">
      <span class="lbl">Empty-room cooling now</span><span class="track"><span class="meter now" style="width:100%"></span></span><span class="val">${fmt(now)} kWh</span>
      <span class="lbl">With these changes</span><span class="track"><span class="meter after" style="width:${now ? (after / now) * 100 : 0}%"></span></span><span class="val">${fmt(after)} kWh</span>
    </div>
    ${notes.length ? `<p class="hint">${notes.join(" · ")}.</p>` : ""}
    <p class="hint">Rooms touched by these changes are outlined on the floor plan.</p>`;
}

function impactLine(a) {
  const parts = [];
  if (a.kwh_per_week > 0.5) parts.push(`<span>Saves <b>${fmt(a.kwh_per_week)} kWh</b> / week (≈ RM ${fmt(a.rm_per_week)}, ${fmt(a.kg_co2e_per_week)} kg CO₂e)</span>`);
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
    const res = await api(`/api/actions/${id}/decide`, { method: "POST", body: JSON.stringify({ status }) });
    decided.set(id, status);
    const n = res.commands?.length || 0;
    toast(status === "approved"
      ? (n ? `Approved and logged. AC command sent to ${n} room${n > 1 ? "s" : ""}: ${AC_MODE_TEXT[res.commands[0].ac_mode]}.` : "Approved. Logged to the decision table.")
      : "Dismissed. Logged to the decision table.");
    renderActions(lastBrief.actions, lastBrief.brief);
    loadCommands().catch(() => {});
    loadOverview().catch(() => {});
  } catch (e) {
    btn.disabled = false;
    toast(`Could not save the decision: ${e.message}`);
  }
}

async function loadCommands() {
  const d = await api("/api/commands");
  clearTimeout(loadCommands.timer);
  if (d.commands.some((c) => (c.delivered || "").startsWith("sending"))) {
    loadCommands.timer = setTimeout(() => loadCommands().catch(() => {}), 3000);
  }
  const list = $("#commands");
  if (!d.commands.length) {
    list.innerHTML = `<li class="empty">Nothing sent yet. Approve an air-conditioning change to send its rooms a new AC mode.</li>`;
    return;
  }
  const time = (iso) => new Date(iso).toLocaleTimeString("en-MY", { hour: "2-digit", minute: "2-digit" });
  list.innerHTML = d.commands.slice(0, 8).map((c) => `<li>
      <span class="when">${esc(time(c.issued_at))}</span>
      <span><b>${esc(c.room_id)}</b> → AC ${esc(c.ac_mode_text)}${c.ac_mode === "occupancy" ? ` (pre-cool ${c.precool_min} min)` : ""}</span>
      <code>${esc(c.topic)}</code>
      <span class="hint">${esc(c.delivered || "sending…")}</span>
    </li>`).join("") + (d.commands.length > 8 ? `<li class="empty">and ${d.commands.length - 8} earlier.</li>` : "");
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

/* ---------- tabs ---------- */
const TABS = ["today", "autopilot", "actions", "students", "data"];
function showTab(name, push = true) {
  if (!TABS.includes(name)) name = "today";
  document.querySelectorAll("[role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== name));
  if (push) history.replaceState(null, "", `${location.pathname}${location.search}#${name}`);
  if (name === "autopilot" && !apPlan && !apBusy) loadLatestPlan();
  window.scrollTo({ top: 0 });
}
document.querySelectorAll("[role=tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.goto)));
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
  const tabs = [...document.querySelectorAll("[role=tab]")];
  const i = tabs.indexOf(document.activeElement);
  const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  next.focus();
  showTab(next.dataset.tab);
});

/* ---------- autopilot ---------- */
let apPlan = null, apBusy = false, apShowAll = false;
const AP_EXAMPLES = ["Don't move Friday classes", "Comfort first, students said rooms are stuffy", "Keep moves in the same block, at most 6", "Leave the Tech Lab classes where they are"];
$("#ap-examples").innerHTML = AP_EXAMPLES.map((q) => `<button type="button" class="chip-btn">${esc(q)}</button>`).join("");
$("#ap-examples").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { $("#ap-goals").value = b.textContent; runPlan(); }));
$("#ap-form").addEventListener("submit", (e) => { e.preventDefault(); runPlan(); });

async function loadLatestPlan() {
  try {
    const p = await api("/api/autopilot/latest");
    if (p && p.id) renderPlanResult(p);
  } catch { /* nothing planned yet */ }
}

async function runPlan() {
  if (apBusy) return;
  apBusy = true;
  const btn = $("#ap-run");
  btn.disabled = true;
  btn.textContent = "Planning…";
  $("#ap-status").textContent = "Forecasting classes, reading the weather forecast, solving the room assignment…";
  try {
    const p = await api("/api/autopilot/plan", { method: "POST", body: JSON.stringify({ instructions: $("#ap-goals").value.trim() }) });
    $("#ap-status").textContent = "";
    apShowAll = false;
    renderPlanResult(p);
  } catch (e) {
    $("#ap-status").textContent = `Could not plan: ${e.message}`;
  } finally {
    apBusy = false;
    btn.disabled = false;
    btn.textContent = "Plan tomorrow";
  }
}

function renderPlanResult(p) {
  apPlan = p;
  $("#ap-result").hidden = false;
  $("#ap-applied").hidden = true;
  const s = p.saving, ex = p.explanation || {};
  $("#ap-headline").textContent = ex.headline || `Plan for ${p.day_label}`;
  $("#ap-summary").textContent = ex.summary || "";
  const goals = (p.constraints.notes || []).filter(Boolean);
  $("#ap-source").textContent = `For ${p.day_label}. ` + (goals.length ? `Goals understood: ${goals.join("; ")} (read by ${shortSource(p.constraints_read_by)}). ` : "") +
    `Written by ${shortSource(ex.source)}; every figure comes from the solver and the twin.`;
  $("#ap-kpis").innerHTML = kpiTiles([
    [`${fmt(s.kwh)} kWh`, `saved on ${p.day_label.split(" ")[0]} (${fmt(s.pct * 100)}% of today's setup)`, "good"],
    [`RM ${fmt(s.rm)}`, `a day; about RM ${fmt(s.rm * 5 * 14)} over a 14-week semester`],
    [`${fmt(s.kg_co2e)} kg`, "CO₂e avoided"],
    [`${fmt(p.baseline.co2_breach_hours)} → ${fmt(p.plan.co2_breach_hours)} h`, "class-hours with stale air"],
  ]);
  const max = Math.max(p.baseline.kwh, 1);
  const bar = (label, v, cls) => `<span class="lbl">${label}</span><span class="track"><span class="meter ${cls}" style="width:${(v / max) * 100}%"></span></span><span class="val">${fmt(v)} kWh</span>`;
  $("#ap-bars").innerHTML = bar("Today's setup (AC on 07:30–21:00)", p.baseline.kwh, "now") +
    bar("+ AC only around classes", p.ac_only.kwh, "mid") + bar("+ classes in right-sized rooms", p.plan.kwh, "after");
  drawPlanChart(p);
  renderMoves();
  $("#ap-schedule").innerHTML = `<table class="rows"><thead><tr><th>Room</th><th>AC on</th><th class="num">Hours (was)</th><th>Applied</th></tr></thead><tbody>${
    p.ac_schedule.map((r) => `<tr><td>${esc(r.room_id)}</td><td>${r.windows.length ? esc(r.windows.join(", ")) : "off all day"}</td>
      <td class="num">${fmt(r.hours_on, 1)} (${fmt(r.hours_before, 1)})</td>
      <td>${r.decision === "auto" ? `<span class="badge auto">auto</span>` : `<span class="badge review" title="Data confidence ${r.confidence}/100">needs you</span>`}</td></tr>`).join("")
  }</tbody></table>`;
  const acc = p.accuracy || {};
  $("#ap-trust").innerHTML = [
    acc.classes ? `<span><b>Forecast checked:</b> last week's ${fmt(acc.classes)} classes predicted within ±${fmt(acc.mean_abs_error_people, 1)} people on average; ${fmt(acc.within_tolerance_pct * 100)}% within tolerance.</span>` : "",
    `<span><b>Solver:</b> ${esc(p.solver.solver)}, ${esc(p.solver.status.replace(/^.*\((.*)\)$/, "$1"))} in ${fmt(p.solver.ms)} ms over ${fmt(p.solver.variables)} choices.</span>`,
    `<span><b>Weather:</b> up to ${fmt(p.weather.max_c, 1)}°C in teaching hours (${esc(p.weather.source)}); hotter mornings get a longer pre-cool.</span>`,
  ].filter(Boolean).join("");
  const autoRooms = p.ac_schedule.filter((r) => r.decision === "auto").length;
  $("#ap-apply-safe").textContent = `Apply safe changes (${autoRooms} rooms, ${p.auto_moves} moves)`;
  $("#ap-apply-all").textContent = p.review_moves ? `Approve all, incl. ${p.review_moves} for review` : "Approve all";
  $("#ap-apply-hint").textContent = "Safe = trusted sensors, same block, room not too full. The rest waits for you.";
}

function renderMoves() {
  const p = apPlan, list = $("#ap-moves");
  $("#ap-moves-count").textContent = p.moves.length ? `${p.auto_moves} automatic · ${p.review_moves} need you` : "";
  if (!p.moves.length) {
    list.innerHTML = `<li class="empty">No room changes: ${p.constraints.no_moves ? "you asked to keep every class where it is" : "every class is already in a sensible room"}. The saving comes from the AC schedule.</li>`;
    $("#ap-more").hidden = true;
    return;
  }
  const shown = apShowAll ? p.moves : p.moves.slice(0, 6);
  list.innerHTML = shown.map((m) => `<li class="${m.decision}">
      <div class="mv-top"><b>${esc(m.module)}</b><span class="badge ${m.decision}">${m.decision === "auto" ? "auto" : "needs you"}</span></div>
      <div class="mv-route">${esc(m.time)} · ${esc(m.from)} <span aria-hidden="true">→</span><span class="visually-hidden">to</span> <b>${esc(m.to)}</b>
        ${m.kwh_saved > 0.5 ? `<span class="save">−${fmt(m.kwh_saved, 1)} kWh</span>` : ""}${m.co2_fix ? `<span class="fix">fresher air</span>` : ""}</div>
      <div class="hint">${esc(m.why)}${m.reasons.length ? `. Needs you: ${esc(m.reasons.join(", "))}` : ""}</div>
    </li>`).join("");
  $("#ap-more").hidden = p.moves.length <= 6;
  $("#ap-more").textContent = apShowAll ? "Show fewer" : `Show all ${p.moves.length}`;
}
$("#ap-more").addEventListener("click", () => { apShowAll = !apShowAll; renderMoves(); });

function drawPlanChart(p) {
  const hours = [...Array(24).keys()].slice(6, 22);
  chart($("#ap-chart"), {
    data: {
      labels: hours.map((h) => `${String(h).padStart(2, "0")}:00`),
      datasets: [
        { type: "bar", label: "Today's setup (kWh)", data: hours.map((h) => p.baseline.hourly_kwh[h]), backgroundColor: "rgba(59,110,168,.35)", borderRadius: 2, yAxisID: "y" },
        { type: "bar", label: "Plan (kWh)", data: hours.map((h) => p.plan.hourly_kwh[h]), backgroundColor: TEAL, borderRadius: 2, yAxisID: "y" },
        { type: "line", label: "Outdoor °C", data: hours.map((h) => p.weather.hourly_c[h]), borderColor: AMBER, pointRadius: 0, borderWidth: 2, tension: 0.3, yAxisID: "t" },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: true, position: "bottom", labels: { boxWidth: 12 } } },
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true } },
        y: { beginAtZero: true, title: { display: true, text: "kWh" } },
        t: { position: "right", grid: { display: false }, title: { display: true, text: "°C" }, suggestedMin: 22, suggestedMax: 34 },
      },
    },
  });
}

async function applyPlan(all) {
  if (!apPlan) return;
  const btns = [$("#ap-apply-safe"), $("#ap-apply-all")];
  btns.forEach((b) => (b.disabled = true));
  try {
    const r = await api("/api/autopilot/apply", { method: "POST", body: JSON.stringify({ plan_id: apPlan.id, approve_review: all }) });
    const box = $("#ap-applied");
    box.hidden = false;
    box.innerHTML = `<h3>Done. ${r.rooms.length} rooms switched to AC by occupancy; ${r.moves_applied.length} room changes approved${r.moves_held ? `, ${r.moves_held} held for you` : ""}.</h3>
      <p class="hint">${r.commands.length ? `${r.commands.length} AC commands sent (see Actions → Sent to the rooms).` : "Those rooms already had this AC mode."} Logged to the decision table.</p>
      ${r.notices.notices.length ? `<h4>Notices to send <span class="hint">(${esc(r.notices.source)})</span></h4>
        <ol class="notices">${r.notices.notices.map((n) => `<li>${esc(n.text)}</li>`).join("")}</ol>
        <button type="button" class="ghost" id="copy-notices">Copy notices</button>` : ""}`;
    $("#copy-notices")?.addEventListener("click", () => {
      navigator.clipboard?.writeText(r.notices.notices.map((n) => n.text).join("\n\n")).then(() => toast("Notices copied."), () => toast("Copy failed; select the text instead."));
    });
    toast(`Plan applied: ${r.commands.length} AC commands sent.`);
    loadCommands().catch(() => {});
    loadOverview().catch(() => {});
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    toast(`Could not apply the plan: ${e.message}`);
  } finally {
    btns.forEach((b) => (b.disabled = false));
  }
}
$("#ap-apply-safe").addEventListener("click", () => applyPlan(false));
$("#ap-apply-all").addEventListener("click", () => applyPlan(true));

/* ---------- boot ---------- */
let lastBrief = null;

/* ---------- demo clock: replay the floor plan at a past time ---------- */
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
let replayAt = null;          // "YYYY-MM-DDTHH:MM" local, or null for live
let replayRangeSet = false;
let playTimer = null;
const pad = (n) => String(n).padStart(2, "0");
const hhmm = (h) => `${pad(Math.floor(h))}:${pad(Math.round((h % 1) * 60))}`;
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function setupReplayDays(range) {
  if (replayRangeSet || !range) return;
  replayRangeSet = true;
  const last = new Date(range.last), first = new Date(range.first);
  const firstDay = new Date(first.getFullYear(), first.getMonth(), first.getDate());
  const sel = $("#replay-day");
  const keep = sel.value;               // a day chosen from a ?at= link
  const days = [];
  for (let i = 1; i <= 7; i++) {        // the last 7 full days, newest first
    const d = new Date(last.getFullYear(), last.getMonth(), last.getDate() - i);
    if (d >= firstDay) days.push(d);
  }
  sel.innerHTML = days.map((d) =>
    `<option value="${ymd(d)}">${WEEKDAYS[d.getDay()]} ${d.getDate()} ${d.toLocaleString("en-MY", { month: "short" })}</option>`).join("");
  if (keep && days.some((d) => ymd(d) === keep)) { sel.value = keep; return; }
  // Default to the most recent busy teaching day: Wednesday if present, else any weekday.
  const pick = days.find((d) => d.getDay() === 3) || days.find((d) => d.getDay() >= 1 && d.getDay() <= 5);
  if (pick) sel.value = ymd(pick);
}

function replayValue() {
  return `${$("#replay-day").value}T${hhmm(Number($("#replay-hour").value))}`;
}

function replayLabel() {
  const opt = $("#replay-day").selectedOptions[0];
  return `${opt ? opt.textContent : ""}, ${hhmm(Number($("#replay-hour").value))}`;
}

function renderReplayUi() {
  const on = replayAt != null;
  $("#replay").classList.toggle("replaying", on);
  $("#replay-live").setAttribute("aria-pressed", String(!on));
  $("#replay-time").textContent = hhmm(Number($("#replay-hour").value));
  $("#replay-play").textContent = playTimer ? "\u275A\u275A Pause" : "\u25B6 Play the day";
  $("#study-hint").textContent = on
    ? `Replaying ${replayLabel()}. Free rooms with no class for at least 30 minutes; already-cooled rooms first.`
    : "Free rooms with no class for at least 30 minutes. Rooms already being cooled come first, so studying there uses cooling that would otherwise be wasted.";
  if (on) {
    $("#plan-title").textContent = `Rooms on ${replayLabel()}`;
    $("#plan-hint").textContent = "Replaying recorded readings. Press Live to return to now.";
    $("#quiet-nudge").hidden = true;
  } else {
    $("#plan-title").textContent = "Rooms right now";
    $("#plan-hint").textContent = "Select a room for its last 48 hours and tomorrow's forecast.";
  }
}

let replayFetch = 0;
async function goReplay(at) {
  replayAt = at;
  renderReplayUi();
  const ticket = ++replayFetch;
  const o = await api(at ? `/api/overview?at=${encodeURIComponent(at)}` : "/api/overview");
  if (ticket !== replayFetch) return;   // a newer request superseded this one
  applyOverview(o);
}

function stopPlay() {
  clearInterval(playTimer);
  playTimer = null;
  renderReplayUi();
}

function startPlay() {
  const slider = $("#replay-hour");
  if (Number(slider.value) >= Number(slider.max)) slider.value = slider.min;
  playTimer = setInterval(() => {
    const next = Number(slider.value) + 0.5;
    if (next > Number(slider.max)) return stopPlay();
    slider.value = next;
    goReplay(replayValue()).catch(() => {});
  }, 1100);
  goReplay(replayValue()).catch(() => {});
}

$("#replay-hour").addEventListener("input", () => { stopPlay(); goReplay(replayValue()).catch((e) => toast(e.message)); });
$("#replay-day").addEventListener("change", () => goReplay(replayValue()).catch((e) => toast(e.message)));
$("#replay-live").addEventListener("click", () => { stopPlay(); goReplay(null).catch((e) => toast(e.message)); });
$("#replay-play").addEventListener("click", () => (playTimer ? stopPlay() : startPlay()));
$("#quiet-replay").addEventListener("click", () => startPlay());

function renderStudy(st) {
  if (!st) return;
  $("#study-msg").innerHTML = esc(st.message) + (!st.open
    ? ` <button type="button" class="linkish" id="study-demo">See it at 2 pm on a teaching day</button>` : "");
  $("#study-demo")?.addEventListener("click", () => {
    const sel = $("#replay-day");
    const weekday = [...sel.options].find((o) => /^(Mon|Tue|Wed|Thu|Fri)/.test(o.textContent));
    if (weekday) sel.value = weekday.value;
    $("#replay-hour").value = 14;
    goReplay(replayValue()).catch((e) => toast(e.message));
  });
  $("#study").innerHTML = st.rooms.map((r) => `<li class="study-room${r.cooled ? " cooled" : ""}">
      <button type="button" data-id="${esc(r.room_id)}" aria-label="${esc(`${r.room_id}, free until ${r.free_until}, ${r.reasons.join(", ")}`)}">
        <span class="code">${esc(r.room_id)}</span>
        <span class="name">${esc(r.name)} · ${r.seats} seats</span>
        <span class="until">Free until <b>${esc(r.free_until)}</b> <span class="hint">(${esc(r.free_until_why)})</span></span>
        <span class="why">${r.cooled ? `<b class="ok">Already cooled</b> · ` : ""}${fmt(r.co2_ppm)} ppm · ${fmt(r.temp_c, 1)}°C</span>
      </button>
    </li>`).join("");
  $("#study").querySelectorAll("button[data-id]").forEach((b) => b.addEventListener("click", () => openRoom(b.dataset.id)));
}

function applyOverview(o) {
  mode = o.mode;
  if (o.weather) {
    $("#weather").textContent = `KL ${fmt(o.weather.temp_c, 0)}°C`;
    $("#weather").title = `Outdoor temperature at APU now (${o.weather.source})`;
  }
  renderStudy(o.study);
  setupReplayDays(o.replay_range);
  if (replayAt) renderReplayUi();
  renderStatus(o.mode);
  renderPlan(o.rooms);
  if (o.replay_at) {
    $("#status").innerHTML = `<span class="dot"></span>Replaying recorded readings from ${esc(replayLabel())}`;
    return o;
  }
  $("#quiet-nudge").hidden = !o.rooms.every((r) => r.status === "idle" || r.status === "offline");
  const cams = o.rooms.filter((r) => r.people_source === "camera").map((r) => r.room_id);
  const sensorLive = o.rooms.some((r) => r.simulated === false);
  if (cams.length) {
    $("#status").innerHTML = `<span class="dot on"></span>${sensorLive ? `Live sensor in ${esc(o.mode.live_room)} reporting · ` : ""}` +
      `Camera counting people in ${esc(cams.join(", "))}${sensorLive ? "" : "; other readings simulated"}`;
  }
  return o;
}

async function loadOverview() {
  const o = await api(replayAt ? `/api/overview?at=${encodeURIComponent(replayAt)}` : "/api/overview");
  return applyOverview(o);
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

// A link like /?at=2026-11-04T10:30 opens straight into replay (handy for demos and the video).
{
  const at = new URLSearchParams(location.search).get("at");
  const m = at && at.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})$/);
  if (m) {
    replayAt = at;
    $("#replay-day").innerHTML = `<option value="${m[1]}">${m[1]}</option>`;
    $("#replay-day").value = m[1];
    $("#replay-hour").value = Number(m[2]) + Number(m[3]) / 60;
  }
}

showTab(location.hash.slice(1) || "today", false);

restoreDecisions().then(() => loadBrief()).catch((e) => { $("#headline").textContent = "CampusPulse could not reach its data service."; $("#figures").textContent = e.message; });
loadSuggestions().catch(() => {});
loadCommands().catch(() => {});
loadNetwork().catch(() => {});
setInterval(() => loadOverview().catch(() => {}), 30000);
setInterval(() => loadNetwork().catch(() => {}), 60000);
