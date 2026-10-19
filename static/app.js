// docsis-watch dashboard. Vanilla JS, no build step, no CDN — this must work
// when the internet is exactly what's broken.
const $ = (s) => document.querySelector(s);
const COLORS = ["#5aa9ff", "#4cc38a", "#f5b849", "#c084fc", "#ff8a65"];
let status = null, series = null, pendingTs = null, minutes = 15;

const fmtT = (ms) => new Date(ms).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

// ---------------------------------------------------------------- live status
function renderStatus(s) {
  status = s;
  $("#demo-banner").hidden = !s.demo;
  $("#uptime").textContent = `· up ${Math.floor(s.uptime_s / 60)}m`;
  $("#health").innerHTML = (s.health || []).map((h) =>
    `<span class="${h.ok ? "ok" : "bad"}" title="${esc(h.message)}">${esc(h.collector)}</span>`).join("");
  const w = s.wifi;
  $("#wifi-now").textContent = w && w.connected && w.rssi_dbm != null
    ? `${w.rssi_dbm} dBm · ${w.tx_rate_mbps ?? "?"} Mbps · ch ${w.channel ?? "?"}`
    : (w && w.connected ? "connected (signal fields unavailable)" : "");
  const h = s.host;
  $("#wifi-note").textContent = h ? `this computer: default route via ${h.default_iface ?? "?"}`
    + (h.iface_is_wifi == null ? "" : h.iface_is_wifi ? " (Wi-Fi — measurements include Wi-Fi noise)" : " (wired)")
    + ` · load ${h.load1 ?? "?"} / ${h.cpu_count ?? "?"} cores` : "";
  const m = s.modem;
  const caps = Object.keys(m.caps || {});
  $("#modem-state").textContent = m.state
    ? `DOCSIS ${m.state.docsis_version ?? "?"} · ${m.state.status ?? ""} · up ${Math.floor((m.state.uptime_s ?? 0) / 3600)}h`
    : (caps.length ? `reading ${caps.join(", ")}` : "no read-only endpoint found — Layer 1 unavailable");
  renderLine(m, caps);
  $("#modem-note").textContent = caps.length ? `polled unauthenticated every ${60}s · reset = modem rebooted` : "unavailable ≠ zero";
}

// ---------------------------------------------------------------- cable line, in words
const fmtDay = (ms) => new Date(ms).toLocaleString([], {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
function gradeJs(v, lim) {
  if (v == null || !lim) return "";
  const inside = ([lo, hi]) => (lo == null || v >= lo) && (hi == null || v <= hi);
  return inside(lim.good) ? "good" : inside(lim.ok) ? "watch" : "bad";
}
function renderLine(m, caps) {
  const h = m.health || {};
  $("#line-verdict").className = `verdict ${h.verdict || "unknown"}`;
  $("#line-verdict").textContent = h.headline || (caps.length ? "waiting for first reading…" : "No modem data");
  $("#line-items").innerHTML = (h.items || []).map((i) =>
    `<div class="li"><span class="dot ${i.status}"></span><span class="lbl">${esc(i.label)}</span>`
    + `<span class="val">${esc(i.value)}</span>`
    + `<span class="why">${esc(i.meaning)} <b>Good:</b> ${esc(i.good)}</span></div>`).join("")
    || `<div class="muted">${caps.length ? "Waiting for the first modem reading…"
        : "No read-only modem endpoint found, so line health is unavailable. Nothing here is guessed."}</div>`;
  $("#line-events").innerHTML = (h.events || []).length
    ? `<h3 style="margin-top:10px">Latest from the modem's own log</h3>` + h.events.map((e) =>
        `<div class="ev" title="${esc(e.raw)}"><span class="dot ${e.status}"></span>`
        + `<span class="when">${fmtDay(e.ts_ms)}</span><span>${esc(e.plain)}</span></div>`).join("")
    : "";
  const L = h.limits || {};
  const table = (rows, cols) => `<table class="chtab"><tr>${cols.map((c) => `<th>${c[0]}</th>`).join("")}</tr>`
    + rows.map((r) => `<tr>${cols.map((c) => { const v = c[1](r); return `<td class="${c[2] ? c[2](r) : ""}">${v ?? "–"}</td>`; }).join("")}</tr>`).join("")
    + `</table>`;
  const ds = (m.channels || []).filter((c) => c.direction === "downstream");
  const us = (m.channels || []).filter((c) => c.direction === "upstream");
  $("#channels").innerHTML = (ds.length ? `<b>Download (${ds.length})</b>` + table(ds, [
      ["Channel", (c) => `${esc(c.channel_id)} <span class="muted">${esc(c.channel_type ?? "")}</span>`],
      ["Power dBmV", (c) => c.power, (c) => gradeJs(c.power, L.ds_power)],
      ["SNR dB", (c) => c.snr, (c) => gradeJs(c.snr, L.ds_snr)],
      ["Corrected (total)", (c) => c.corrected?.toLocaleString()],
      ["Uncorrectable (total)", (c) => c.uncorrected?.toLocaleString()]]) : "")
    + (us.length ? `<b>Upload (${us.length})</b>` + table(us, [
      ["Channel", (c) => `${esc(c.channel_id)} <span class="muted">${esc(c.channel_type ?? "")}</span>`],
      ["Power dBmV", (c) => c.power, (c) => gradeJs(c.power, L.us_power)],
      ["T3 (total)", (c) => c.t3_timeouts], ["T4 (total)", (c) => c.t4_timeouts]]) : "");
}

// ---------------------------------------------------------------- charts
function chart(canvas, draw) {
  // CSS height comes from data-h, not the height attribute: canvas.height below
  // rewrites that attribute, so reading it back doubled the chart on every redraw
  // on a Retina screen (dpr 2).
  const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth;
  const H = +(canvas.dataset.h ||= canvas.getAttribute("height") || 140);
  canvas.style.height = `${H}px`; canvas.width = W * dpr; canvas.height = H * dpr;
  const g = canvas.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  g.font = "11px -apple-system, sans-serif";
  draw(g, W, H);
}
function xScale(W, from, to) { return (t) => 36 + (t - from) / (to - from) * (W - 44); }
function axes(g, W, H, yMin, yMax, unit, from, to) {
  g.strokeStyle = "rgba(128,128,128,.25)"; g.fillStyle = "#8b93a7"; g.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = 8 + (H - 28) * i / 3, v = yMax - (yMax - yMin) * i / 3;
    g.beginPath(); g.moveTo(36, y); g.lineTo(W - 8, y); g.stroke();
    g.fillText(`${Math.round(v)}${unit}`, 2, y + 4);
  }
  g.fillText(fmtT(from), 36, H - 4); g.textAlign = "right"; g.fillText(fmtT(to), W - 8, H - 4); g.textAlign = "left";
}
function marks(g, W, H, from, to, anns) {
  const x = xScale(W, from, to);
  for (const a of anns) {
    const px = x(a.ts_ms); if (px < 36 || px > W - 8) continue;
    g.strokeStyle = "#e85d3a"; g.lineWidth = 1.5; g.setLineDash([4, 3]);
    g.beginPath(); g.moveTo(px, 4); g.lineTo(px, H - 20); g.stroke(); g.setLineDash([]);
  }
}
function drawPing(g, W, H) {
  const {from_ms: from, to_ms: to, pings, annotations} = series;
  const all = Object.values(pings).flat().map((b) => b.rtt).filter((v) => v != null);
  const yMax = Math.max(20, Math.min(500, Math.ceil((percentile(all, .98) || 20) * 1.3)));
  axes(g, W, H, 0, yMax, "ms", from, to);
  const x = xScale(W, from, to), y = (v) => 8 + (H - 28) * (1 - Math.min(v, yMax) / yMax);
  let legend = [];
  Object.entries(pings).forEach(([tgt, buckets], i) => {
    const col = COLORS[i % COLORS.length]; legend.push(`<span style="color:${col}">■ ${esc(tgt)}${tgt === status?.gateway ? " (gw)" : ""}</span>`);
    g.strokeStyle = col; g.lineWidth = 1.5; g.beginPath(); let pen = false;
    for (const b of buckets) {
      if (b.rtt == null) { pen = false; continue; }
      const px = x(b.t), py = y(b.rtt);
      pen ? g.lineTo(px, py) : g.moveTo(px, py); pen = true;
    }
    g.stroke();
    g.fillStyle = "#ef5350";
    for (const b of buckets) if (b.lost) g.fillRect(x(b.t) - 1, H - 26 - i * 3, 3, 3);
  });
  $("#ping-legend").innerHTML = legend.join(" ");
  marks(g, W, H, from, to, annotations);
}
function drawModem(g, W, H) {
  const {from_ms: from, to_ms: to, modem, annotations} = series;
  const vals = modem.map((m) => m.uncorr_delta).filter((v) => v != null);
  const yMax = Math.max(10, Math.ceil((Math.max(0, ...vals) || 10) * 1.2));
  axes(g, W, H, 0, yMax, "", from, to);
  const x = xScale(W, from, to), y = (v) => 8 + (H - 28) * (1 - Math.min(v, yMax) / yMax);
  for (const m of modem) {
    const px = x(m.t);
    if (m.reset) { g.fillStyle = "#f5b849"; g.fillText("reset", px - 12, 18); continue; }
    g.fillStyle = m.uncorr_delta > 0 ? "#ef5350" : "#4cc38a";
    g.fillRect(px - 2, y(m.uncorr_delta), 4, H - 20 - y(m.uncorr_delta));
  }
  if (!modem.length) { g.fillStyle = "#8b93a7"; g.fillText("no DOCSIS samples in range", 44, H / 2); }
  marks(g, W, H, from, to, annotations);
}
function drawWifi(g, W, H) {
  const {from_ms: from, to_ms: to, wifi, annotations} = series;
  axes(g, W, H, -95, -30, "dBm", from, to);
  const x = xScale(W, from, to), y = (v) => 8 + (H - 28) * (1 - (v + 95) / 65);
  g.strokeStyle = "#5aa9ff"; g.lineWidth = 1.5; g.beginPath(); let pen = false;
  for (const w of wifi) {
    if (w.rssi == null || !w.connected) { pen = false; continue; }
    const px = x(w.t), py = y(w.rssi); pen ? g.lineTo(px, py) : g.moveTo(px, py); pen = true;
  }
  g.stroke();
  g.strokeStyle = "#8b93a7"; g.setLineDash([2, 3]); g.beginPath(); pen = false;
  for (const w of wifi) { if (w.noise == null) { pen = false; continue; } const px = x(w.t), py = y(w.noise); pen ? g.lineTo(px, py) : g.moveTo(px, py); pen = true; }
  g.stroke(); g.setLineDash([]);
  g.fillStyle = "#ef5350";
  for (const w of wifi) if (w.connected === 0) g.fillRect(x(w.t) - 1, 8, 3, H - 28);
  if (!wifi.length) { g.fillStyle = "#8b93a7"; g.fillText("no Wi-Fi telemetry (unsupported here, or wired)", 44, H / 2); }
  marks(g, W, H, from, to, annotations);
}
function percentile(arr, p) { if (!arr.length) return null; const s = [...arr].sort((a, b) => a - b); return s[Math.floor(p * (s.length - 1))]; }

async function refreshSeries() {
  series = await (await fetch(`/api/series?minutes=${minutes}`)).json();
  chart($("#c-ping"), drawPing); chart($("#c-modem"), drawModem); chart($("#c-wifi"), drawWifi);
  renderAnns(series.annotations);
}

// ---------------------------------------------------------------- annotations
function renderAnns(list) {
  const all = list.length ? list : [];
  $("#anns").innerHTML = all.length ? all.slice().reverse().map((a) =>
    `<div class="ann" data-id="${a.id}"><span class="muted">${fmtT(a.ts_ms)}</span><span class="cat">${esc(a.category.replace(/_/g, " "))}</span>`
    + `<span class="muted">${a.duration_s ? "~" + a.duration_s + "s" : ""} ${esc(a.call_app ?? "")} ${esc(a.connection ?? "")}</span>`
    + `<span>${esc(a.note ?? "")}</span><span class="muted" style="margin-left:auto">analyse →</span></div>`).join("")
    : `<div class="muted">No marks in this window. Press the big button when something happens — the timestamp is taken instantly, details can follow.</div>`;
  document.querySelectorAll(".ann").forEach((el) => el.onclick = () => showReport(el.dataset.id));
}

$("#problem-now").onclick = () => {
  pendingTs = Date.now();  // captured NOW; the form is filled in afterwards
  $("#ann-form").hidden = false; $("#problem-now").textContent = `Marked ${fmtT(pendingTs)} — add details`;
  $("#ann-form [name=note]").focus();
};
$("#ann-cancel").onclick = () => { pendingTs = null; $("#ann-form").hidden = true; $("#problem-now").textContent = "Problem now"; };
$("#ann-form").onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target), body = Object.fromEntries(fd.entries());
  body.ts_ms = (pendingTs || Date.now()) - (parseInt(body.ago_s || "0", 10) * 1000); delete body.ago_s;
  const r = await fetch("/api/annotations", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const a = await r.json();
  $("#ann-cancel").onclick(); e.target.reset();
  await refreshSeries();
  if (a.id) setTimeout(() => showReport(a.id), 300);
};

// ---------------------------------------------------------------- report
async function showReport(id) {
  const a = await (await fetch(`/api/incidents/${id}`)).json();
  $("#report").hidden = false; $("#rep-id").textContent = `#${id} · ${fmtT(a.annotation.ts_ms)}`;
  const o = a.overall;
  const exp = status?.demo ? `<span class="muted small">exports disabled in demo</span>`
    : `<a href="/api/incidents/${id}/summary.md" target="_blank">summary.md</a> <a href="/api/incidents/${id}/data.json" target="_blank">data.json</a> <a href="/api/incidents/${id}/bundle.zip">bundle.zip</a>`;
  $("#rep-exports").innerHTML = exp;
  const probes = Object.entries(a.probes).map(([t, p]) => {
    const f = (s) => s ? `${s.loss_pct}% loss · ${s.rtt_med ?? "–"}/${s.rtt_p95 ?? "–"} ms` : "no data";
    return `<tr><td><code>${esc(t)}</code>${t === a.gateway ? " <span class='muted'>gw</span>" : ""}</td><td>${f(p.baseline || p.before)}</td><td><b>${f(p.during)}</b></td><td>${f(p.after)}</td></tr>`;
  }).join("");
  const deltas = a.channel_deltas.filter((d) => d.uncorrected !== 0 && d.uncorrected != null || d.t3_timeouts > 0 || d.t4_timeouts > 0 || d.corrected === "reset");
  $("#rep-body").innerHTML = `
    <p><b>${esc(o.hypothesis)}</b> <span class="badge ${o.confidence}">confidence ${o.confidence}</span>
      ${o.layers_agreeing.length ? `<span class="muted">· ${o.layers_agreeing.join(" + ")} agree</span>` : ""}</p>
    <p class="muted small">Windows: baseline ${a.windows.baseline.start.slice(11, 19)}–${a.windows.baseline.end.slice(11, 19)}Z · during ${a.windows.during.start.slice(11, 19)}–${a.windows.during.end.slice(11, 19)}Z</p>
    <table><tr><th>target</th><th>baseline</th><th>during</th><th>after</th></tr>${probes}</table>
    <p>Fault domain from probes: <b>${esc(a.fault_domain.label)}</b> ${a.fault_domain.evidence.length ? "— " + esc(a.fault_domain.evidence.join("; ")) : ""}</p>
    ${a.channel_deltas.length ? `<p class="small">DOCSIS: ${a.channel_deltas.length} channels compared. ${deltas.length ? "Channels that moved: " + deltas.map((d) => `${d.direction.slice(0, 2)}${d.channel_id} unc ${d.uncorrected ?? "–"}${d.t3_timeouts ? " T3+" + d.t3_timeouts : ""}${d.t4_timeouts ? " T4+" + d.t4_timeouts : ""}`).join(", ") : "No counter moved."}</p>` : ""}
    ${a.findings.map((f) => `<div class="finding ${f.confidence}"><b>${esc(f.hypothesis)}</b> <span class="badge ${f.confidence}">${f.confidence}</span>
      <ul>${f.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}${f.contradictions.map((c) => `<li class="muted">contradicts: ${esc(c)}</li>`).join("")}<li class="muted">next: ${esc(f.next_step)}</li></ul></div>`).join("")}
    ${a.missing_signals.length ? `<p class="small"><b>Not available:</b></p><ul class="small">${a.missing_signals.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""}`;
  $("#report").scrollIntoView({behavior: "smooth", block: "start"});
}

// ---------------------------------------------------------------- boot
(async function boot() {
  try { renderStatus(await (await fetch("/api/status")).json()); } catch (e) {}
  await refreshSeries();
  const es = new EventSource("/events");
  es.onmessage = (ev) => { try { renderStatus(JSON.parse(ev.data).status); } catch (e) {} };
  setInterval(refreshSeries, 5000);
  window.addEventListener("resize", () => series && refreshSeries());
})();
