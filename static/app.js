// docsis-watch dashboard. Vanilla JS, no build step, no CDN — this must work
// when the internet is exactly what's broken.
const $ = (s) => document.querySelector(s);
let status = null, series = null, pendingTs = null, minutes = 15;

const fmtT = (ms) => new Date(ms).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const fmtHM = (ms) => new Date(ms).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
const fmtDay = (ms) => new Date(ms).toLocaleString([], {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function fmtDur(s) {
  if (s == null) return "";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 2 * 86400) return `${Math.round(s / 3600)} hours`;
  return `${Math.round(s / 86400)} days`;
}

const CATEGORY = {video_freeze: "Video froze", audio_dropout: "Sound dropped", robot_voice: "Robot voice",
  call_dropped: "Call dropped", slow_web: "Websites slow", total_outage: "No internet", other: "Something else"};
const APP = {teams_desktop: "Teams app", teams_web: "Teams in the browser", google_meet: "Google Meet",
  zoom_desktop: "Zoom", webex: "Webex", none: "not a call"};
const KNOWN = {"1.1.1.1": "Cloudflare", "1.0.0.1": "Cloudflare", "8.8.8.8": "Google", "8.8.4.4": "Google", "9.9.9.9": "Quad9"};
const targetName = (t) => t === status?.gateway ? "Your router" : KNOWN[t] ? `Internet (${KNOWN[t]})` : t;
const sensorName = (c) => c === "host" ? "This Mac" : c === "modem" ? "Modem" : c === "wifi" ? "Wi-Fi"
  : c.startsWith("ping:") ? `Ping ${targetName(c.slice(5)).toLowerCase()}` : c;

// ---------------------------------------------------------------- overview: one sentence + the path
const RANK = {good: 0, unknown: 0, watch: 1, bad: 2};
const worst = (...xs) => xs.reduce((a, b) => (RANK[b] > RANK[a] ? b : a), "good");
function wifiGrade(rssi) { return rssi >= -67 ? ["good", "Strong"] : rssi >= -75 ? ["watch", "Fair"] : ["bad", "Weak"]; }

function recentLoss(targets, ms) {
  if (!series) return null;
  const since = series.to_ms - ms;
  let lost = 0, n = 0; const rtts = [];
  for (const t of targets) for (const b of series.pings[t] || []) {
    if (b.t < since) continue;
    lost += b.lost; n += b.n; if (b.rtt != null) rtts.push(b.rtt);
  }
  return n ? {pct: 100 * lost / n, rtt: percentile(rtts, .5)} : null;
}

function renderOverview() {
  if (!status) return;
  const gw = status.gateway, offnet = (status.targets || []).filter((t) => t !== gw);
  const local = recentLoss([gw], 120_000), net = recentLoss(offnet, 120_000);
  const set = (key, state, text, name) => {
    const li = $(`#path [data-node=${key}]`); li.className = state;
    li.querySelector(".state").textContent = text; if (name) li.querySelector(".name").textContent = name;
  };

  const hostOk = (status.health || []).find((h) => h.collector === "host")?.ok;
  const h = status.host, busy = h && h.load1 != null && h.cpu_count && h.load1 > h.cpu_count * 1.5;
  set("mac", hostOk ? (busy ? "watch" : "good") : "unknown", hostOk ? (busy ? "Very busy" : "Working") : "Unknown");

  const wired = h && h.iface_is_wifi === 0, w = status.wifi;
  let ls = "unknown", lt = "No data";
  if (wired) { ls = "good"; lt = "Connected"; }
  else if (w && w.connected === 0) { ls = "bad"; lt = "Not connected"; }
  else if (w && w.rssi_dbm != null) { const [g, word] = wifiGrade(w.rssi_dbm); ls = g; lt = `${word} signal`; }
  else if (w && w.connected) { ls = "good"; lt = "Connected"; }
  if (local && local.pct > 5) { ls = "bad"; lt = "Router not answering"; }
  else if (local && local.pct > 1) { ls = worst(ls, "watch"); lt = "Some requests lost"; }
  set("local", ls, lt, wired ? "Home network" : "Wi-Fi");

  const mh = status.modem?.health, lv = mh?.verdict;
  const lineState = lv === "good" ? "good" : lv === "watch" ? "watch" : lv === "bad" ? "bad" : "unknown";
  set("line", lineState, {good: "Healthy", watch: "Worth watching", bad: "Problems found"}[lineState] || "No modem data");

  let ns = "unknown", nt = "No data";
  if (net) {
    ns = net.pct > 5 ? "bad" : net.pct > 1 || (net.rtt ?? 0) > 100 ? "watch" : "good";
    nt = net.pct > 1 ? `${Math.round(net.pct)}% of requests lost` : net.rtt != null ? `${Math.round(net.rtt)} ms` : "Reachable";
  }
  set("net", ns, nt);

  let head, sub = "Based on the last two minutes of measurements.";
  if (!net && !local) {
    head = "No measurements in the last two minutes";
    sub = "The monitor may be starting up, or this Mac was asleep.";
  } else if (ls === "bad") head = wired ? "Your home network is having trouble" : "Your Wi-Fi is having trouble";
  else if (ns === "bad") head = "Your internet is having trouble outside your home";
  else if (lineState === "bad") head = "Your cable line is showing problems";
  else if (worst(ls, ns, lineState) === "watch") head = "Your internet is working, with some hiccups";
  else head = "Your internet is working normally";
  $("#headline").textContent = head; $("#subline").textContent = sub;
}

// ---------------------------------------------------------------- live status
function renderStatus(s) {
  status = s;
  $("#demo-banner").hidden = !s.demo;
  $("#clock").textContent = `${new Date(s.now_ms || Date.now()).toLocaleString([], {weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"})} · monitoring for ${fmtDur(s.uptime_s)}`;
  const hs = s.health || [], ok = hs.filter((x) => x.ok).length;
  $("#sensors").textContent = !hs.length ? "No sensors reporting yet"
    : ok === hs.length ? `All ${hs.length} sensors working` : `${ok} of ${hs.length} sensors working`;
  $("#health").innerHTML = hs.map((x) => `<span class="${x.ok ? "ok" : "bad"}" title="${esc(x.message)}">${esc(sensorName(x.collector))}</span>`).join("");

  const w = s.wifi, h = s.host;
  $("#wifi-title").textContent = h && h.iface_is_wifi === 0 ? "Wi-Fi signal (not in use)" : "Wi-Fi signal";
  if (w && w.connected && w.rssi_dbm != null) {
    $("#now-wifi").textContent = wifiGrade(w.rssi_dbm)[1];
    $("#wifi-now").textContent = `${w.rssi_dbm} dBm${w.tx_rate_mbps ? ` · ${w.tx_rate_mbps} Mbps` : ""}`;
  } else {
    $("#now-wifi").textContent = w && w.connected ? "Connected" : "–";
    $("#wifi-now").textContent = w && w.connected ? "signal strength not available" : "";
  }
  $("#wifi-note").textContent = h ? `This Mac connects through ${h.default_iface ?? "?"}`
    + (h.iface_is_wifi == null ? "" : h.iface_is_wifi ? " (Wi-Fi, so every number includes your Wi-Fi)" : " (cable)")
    + (w?.channel ? ` · Wi-Fi channel ${w.channel}` : "") + ` · load ${h.load1 ?? "?"} on ${h.cpu_count ?? "?"} cores` : "";

  const m = s.modem, caps = Object.keys(m.caps || {});
  $("#modem-state").textContent = m.state
    ? `· DOCSIS ${m.state.docsis_version ?? "?"} · connected for ${fmtDur(m.state.uptime_s ?? 0)}` : "";
  renderLine(m, caps);
  $("#modem-note").textContent = caps.length || m.health?.items?.length ? "data the modem could not repair" : "modem not readable";
  renderOverview();
}

// ---------------------------------------------------------------- cable line, in words
function gradeJs(v, lim) {
  if (v == null || !lim) return "";
  const inside = ([lo, hi]) => (lo == null || v >= lo) && (hi == null || v <= hi);
  return inside(lim.good) ? "good" : inside(lim.ok) ? "watch" : "bad";
}
function renderLine(m, caps) {
  const h = m.health || {};
  $("#line-verdict").className = `verdict ${h.verdict || "unknown"}`;
  $("#line-verdict").textContent = h.headline || (caps.length ? "Waiting for the first reading…" : "No modem data");
  $("#line-items").innerHTML = (h.items || []).map((i) =>
    `<div class="li"><span class="dot ${i.status}"></span><span class="lbl">${esc(i.label)}</span>`
    + `<span class="val">${esc(i.value)}</span>`
    + `<span class="why">${esc(i.meaning)} Good: ${esc(i.good)}.</span></div>`).join("")
    || `<p class="empty">${caps.length ? "Waiting for the first modem reading…"
        : "Your modem can't be read here, so line health isn't available. Nothing on this page is guessed; the other measurements still work."}</p>`;
  $("#line-events").innerHTML = (h.events || []).length
    ? `<h3>Recent messages from your modem</h3>` + h.events.map((e) =>
        `<div class="ev" title="${esc(e.raw)}"><span class="dot ${e.status}"></span>`
        + `<span class="when">${fmtDay(e.ts_ms)}</span><span>${esc(e.plain)}</span></div>`).join("")
    : "";
  const L = h.limits || {};
  const table = (rows, cols) => `<table class="chtab"><tr>${cols.map((c) => `<th>${c[0]}</th>`).join("")}</tr>`
    + rows.map((r) => `<tr>${cols.map((c) => { const v = c[1](r); return `<td class="${c[2] ? c[2](r) : ""}">${v ?? "–"}</td>`; }).join("")}</tr>`).join("")
    + `</table>`;
  const ds = (m.channels || []).filter((c) => c.direction === "downstream");
  const us = (m.channels || []).filter((c) => c.direction === "upstream");
  $("#channels").innerHTML = (ds.length ? `<h3>Download channels (${ds.length})</h3>` + table(ds, [
      ["Channel", (c) => `${esc(c.channel_id)} <span class="muted">${esc(c.channel_type ?? "")}</span>`],
      ["Power dBmV", (c) => esc(c.power), (c) => gradeJs(c.power, L.ds_power)],
      ["SNR dB", (c) => esc(c.snr), (c) => gradeJs(c.snr, L.ds_snr)],
      ["Corrected (total)", (c) => c.corrected?.toLocaleString()],
      ["Uncorrectable (total)", (c) => c.uncorrected?.toLocaleString()]]) : "")
    + (us.length ? `<h3>Upload channels (${us.length})</h3>` + table(us, [
      ["Channel", (c) => `${esc(c.channel_id)} <span class="muted">${esc(c.channel_type ?? "")}</span>`],
      ["Power dBmV", (c) => esc(c.power), (c) => gradeJs(c.power, L.us_power)],
      ["T3 (total)", (c) => esc(c.t3_timeouts)], ["T4 (total)", (c) => esc(c.t4_timeouts)]]) : "");
}

// ---------------------------------------------------------------- charts
const PAD_L = 40, PAD_R = 4;
function chart(canvas, draw, withTimes) {
  // CSS height comes from data-h, not the height attribute: canvas.height below
  // rewrites that attribute, so reading it back doubled the chart on every redraw
  // on a Retina screen (dpr 2).
  const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth;
  const H = +(canvas.dataset.h ||= canvas.getAttribute("height") || 120) + (withTimes ? 16 : 0);
  canvas.style.height = `${H}px`; canvas.width = W * dpr; canvas.height = H * dpr;
  const g = canvas.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  g.font = `11px ${css("--sans")}`;
  const plotH = H - (withTimes ? 16 : 0);
  draw(g, W, plotH);
  if (withTimes) {
    const {from_ms: from, to_ms: to} = series, x = xScale(W, from, to);
    g.fillStyle = css("--faint");
    const step = minutes <= 15 ? 5 : minutes <= 60 ? 15 : 60;
    const first = Math.ceil(from / (step * 60_000)) * step * 60_000;
    g.textAlign = "center";
    for (let t = first; t <= to; t += step * 60_000) if (x(t) > PAD_L + 14 && x(t) < W - 48) g.fillText(fmtHM(t), x(t), H - 3);
    g.textAlign = "right"; g.fillText("now", W - PAD_R, H - 3); g.textAlign = "left";
  }
}
function xScale(W, from, to) { return (t) => PAD_L + (t - from) / (to - from) * (W - PAD_L - PAD_R); }
function grid(g, W, H, ticks) {  // ticks: [[value-y-px, label], …]
  g.strokeStyle = css("--grid"); g.fillStyle = css("--faint"); g.lineWidth = 1;
  for (const [y, label] of ticks) {
    g.beginPath(); g.moveTo(PAD_L, Math.round(y) + .5); g.lineTo(W - PAD_R, Math.round(y) + .5); g.stroke();
    g.fillText(label, 0, y + 4);
  }
}
function marks(g, W, H, anns) {
  const {from_ms: from, to_ms: to} = series, x = xScale(W, from, to);
  g.strokeStyle = css("--accent"); g.lineWidth = 1.5; g.setLineDash([3, 3]);
  for (const a of anns) {
    const px = x(a.ts_ms); if (px < PAD_L || px > W - PAD_R) continue;
    g.beginPath(); g.moveTo(px, 2); g.lineTo(px, H); g.stroke();
  }
  g.setLineDash([]);
}
function line(g, pts, color, width) {
  g.strokeStyle = color; g.lineWidth = width; g.lineJoin = "round"; g.beginPath(); let pen = false;
  for (const [px, py] of pts) { if (py == null) { pen = false; continue; } pen ? g.lineTo(px, py) : g.moveTo(px, py); pen = true; }
  g.stroke();
}
function pingOrder() {  // router first (drawn underneath, muted), then the internet targets
  const gw = status?.gateway, ts = Object.keys(series.pings);
  return [...ts.filter((t) => t === gw), ...ts.filter((t) => t !== gw)];
}
function drawPing(g, W, H) {
  const {from_ms: from, to_ms: to, pings, annotations} = series;
  const all = Object.values(pings).flat().map((b) => b.rtt).filter((v) => v != null);
  const yMax = Math.max(20, Math.min(500, Math.ceil((percentile(all, .98) || 20) * 1.3 / 10) * 10));
  const top = 6, y = (v) => top + (H - top - 4) * (1 - Math.min(v, yMax) / yMax);
  grid(g, W, H, [[y(yMax), `${yMax} ms`], [y(yMax / 2), `${yMax / 2}`], [y(0), "0"]]);
  const x = xScale(W, from, to), cols = [css("--c3"), css("--c1"), css("--c2"), css("--watch")];
  const legend = [];
  pingOrder().forEach((tgt, i) => {
    const col = tgt === status?.gateway ? cols[0] : cols[1 + (i - (pings[status?.gateway] ? 1 : 0)) % 3];
    legend.push(`<span><i style="background:${col}"></i>${esc(targetName(tgt))}</span>`);
    line(g, pings[tgt].map((b) => [x(b.t), b.rtt == null ? null : y(b.rtt)]), col, tgt === status?.gateway ? 1.25 : 1.5);
  });
  g.fillStyle = css("--bad");
  for (const tgt of Object.keys(pings)) for (const b of pings[tgt]) if (b.lost) g.fillRect(x(b.t) - 1, H - 6, 2, 6);
  $("#ping-legend").innerHTML = legend.join("");
  const net = recentLoss((status?.targets || []).filter((t) => t !== status?.gateway), 60_000);
  $("#now-ping").textContent = net?.rtt != null ? `${Math.round(net.rtt)} ms` : "–";
  marks(g, W, H, annotations);
}
function drawModem(g, W, H) {
  const {from_ms: from, to_ms: to, modem, annotations} = series;
  const vals = modem.map((m) => m.uncorr_delta).filter((v) => v != null);
  const total = vals.reduce((a, b) => a + b, 0);
  const yMax = Math.max(10, Math.ceil(Math.max(0, ...vals) * 1.2));
  const top = 6, y = (v) => top + (H - top - 2) * (1 - Math.min(v, yMax) / yMax);
  grid(g, W, H, [[y(yMax), `${yMax}`], [y(0), "0"]]);
  const x = xScale(W, from, to);
  for (const m of modem) {
    const px = x(m.t);
    if (m.reset) { g.fillStyle = css("--watch"); g.fillText("restart", px - 16, 14); continue; }
    if (m.uncorr_delta > 0) { g.fillStyle = css("--bad"); g.fillRect(px - 1.5, y(m.uncorr_delta), 3, y(0) - y(m.uncorr_delta)); }
    else { g.fillStyle = css("--good"); g.fillRect(px - 1, y(0) - 3, 2, 3); }
  }
  if (!modem.length) { g.fillStyle = css("--faint"); g.fillText("No modem readings in this period", PAD_L + 8, H / 2); }
  $("#now-modem").textContent = modem.length ? (total ? `${total.toLocaleString()} lost` : "None") : "–";
  marks(g, W, H, annotations);
}
function drawWifi(g, W, H) {
  const {from_ms: from, to_ms: to, wifi, annotations} = series;
  const lo = -90, hi = -40, y = (v) => 4 + (H - 8) * (1 - (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo));
  g.fillStyle = css("--grid"); g.fillRect(PAD_L, y(-75), W - PAD_L - PAD_R, y(lo) - y(-75));  // weak zone
  grid(g, W, H, [[y(-50), "strong"], [y(-82), "weak"]]);
  const x = xScale(W, from, to);
  line(g, wifi.map((w) => [x(w.t), w.rssi == null || !w.connected ? null : y(w.rssi)]), css("--c1"), 1.5);
  g.fillStyle = css("--bad");
  for (const w of wifi) if (w.connected === 0) g.fillRect(x(w.t) - 1, 4, 2, H - 8);
  if (!wifi.length) { g.fillStyle = css("--faint"); g.fillText("No Wi-Fi readings (this Mac may be on a cable)", PAD_L + 8, H / 2); }
  marks(g, W, H, annotations);
}
function percentile(arr, p) { if (!arr.length) return null; const s = [...arr].sort((a, b) => a - b); return s[Math.floor(p * (s.length - 1))]; }

async function refreshSeries() {
  series = await (await fetch(`/api/series?minutes=${minutes}`)).json();
  chart($("#c-ping"), drawPing); chart($("#c-modem"), drawModem); chart($("#c-wifi"), drawWifi, true);
  renderAnns(series.annotations);
  renderOverview();
}
$("#range").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  minutes = +b.dataset.min;
  document.querySelectorAll("#range button").forEach((x) => x.classList.toggle("on", x === b));
  refreshSeries();
};

// ---------------------------------------------------------------- reports
function renderAnns(list) {
  $("#anns").innerHTML = list.length ? list.slice().reverse().map((a) =>
    `<div class="ann" data-id="${esc(a.id)}"><span class="when">${fmtHM(a.ts_ms)}</span>`
    + `<span><span class="cat">${esc(CATEGORY[a.category] || a.category)}</span>`
    + `<span class="meta">${[a.duration_s ? `about ${a.duration_s} s` : "", APP[a.call_app] || a.call_app || "", a.note || ""].filter(Boolean).map(esc).join(" · ")}</span></span>`
    + `<span class="go">See what we found →</span></div>`).join("")
    : `<p class="empty">Nothing reported in this period. When something goes wrong, press the green button. The time is saved straight away.</p>`;
  document.querySelectorAll(".ann").forEach((el) => el.onclick = () => showReport(el.dataset.id));
}

const BTN_LABEL = "Something went wrong just now";
$("#problem-now").onclick = () => {
  if (pendingTs) return;
  pendingTs = Date.now();  // captured NOW; the form is filled in afterwards
  $("#problem-now").textContent = `Time saved: ${fmtT(pendingTs)}`; $("#problem-now").classList.add("saved");
  $("#problem-hint").textContent = "Tell us a little more below if you can, then press Save.";
  $("#ann-form").hidden = false;
};
$("#ann-cancel").onclick = () => {
  pendingTs = null; $("#ann-form").hidden = true;
  $("#problem-now").textContent = BTN_LABEL; $("#problem-now").classList.remove("saved");
  $("#problem-hint").textContent = "Press it the moment a call stutters or drops. The time is saved straight away; details are optional.";
};
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

// ---------------------------------------------------------------- what we found
// The analysis speaks engineer (that wording goes into the exports for the
// provider). On screen, lead with what it means for the person.
function plainVerdict(a) {
  const code = a.fault_domain.code, h = a.overall.hypothesis || "";
  if (code === "gap") return ["We can't tell what happened", "Nothing was being measured at that moment. This Mac may have been asleep, or the monitor wasn't running."];
  if (code === "access_line") return ["The problem was outside your home", "Your router answered normally, but the internet didn't. That points to the cable line or your provider."];
  if (code === "lan_wifi_gateway") return ["The problem was inside your home", "Even your own router stopped answering properly, so look at the Wi-Fi or the router first."];
  if (code === "single_destination") return ["Only one internet service was affected", "Your connection itself was fine; one destination had trouble."];
  if (code === "artifact") return ["Probably nothing serious", "Your router answered slowly, but the internet was fine. Routers often put these checks last in line."];
  if (h.startsWith("The computer")) return ["This Mac was overloaded", "The network looked fine, but this Mac was very busy at that moment."];
  if (h.startsWith("RF layer")) return ["A small blip on the cable line", "The modem noticed something, but your connection didn't measurably suffer."];
  return ["Nothing we measure changed at that moment", "Your connection looked normal. The cause may be the call service itself, or the problem was too short to catch."];
}
async function showReport(id) {
  const a = await (await fetch(`/api/incidents/${encodeURIComponent(id)}`)).json();
  $("#report").hidden = false;
  $("#rep-id").textContent = `· ${CATEGORY[a.annotation.category] || a.annotation.category || "report"} at ${fmtT(a.annotation.ts_ms)}`;
  const o = a.overall;
  $("#rep-exports").innerHTML = status?.demo ? `<span class="muted">Downloads are off in demo mode</span>`
    : `<a href="/api/incidents/${id}/summary.md" target="_blank">Summary</a> <a href="/api/incidents/${id}/bundle.zip">Full package (.zip)</a> <a href="/api/incidents/${id}/data.json" target="_blank">Raw data</a>`;
  const f = (s) => s ? `${s.loss_pct}% lost · ${s.rtt_med ?? "–"} ms typical, ${s.rtt_p95 ?? "–"} ms slowest` : "no data";
  const probes = Object.entries(a.probes).map(([t, p]) =>
    `<tr><td>${esc(targetName(t))}</td><td>${f(p.baseline || p.before)}</td><td><b>${f(p.during)}</b></td><td>${f(p.after)}</td></tr>`).join("");
  const deltas = a.channel_deltas.filter((d) => d.uncorrected !== 0 && d.uncorrected != null || d.t3_timeouts > 0 || d.t4_timeouts > 0 || d.corrected === "reset");
  const [lead, why] = plainVerdict(a);
  const conf = {high: "Confidence: high", medium: "Confidence: medium", low: "Confidence: low", none: "Not enough data"};
  $("#rep-body").innerHTML = `
    <p class="finding-lead">${esc(lead)}</p>
    <p>${esc(why)}</p>
    <p class="footnote"><span class="badge ${esc(o.confidence)}">${conf[o.confidence] || esc(o.confidence)}</span>
      Technical verdict: ${esc(o.hypothesis)} · ${esc(a.fault_domain.label)}${o.layers_agreeing.length ? ` · ${o.layers_agreeing.map(esc).join(" and ")} agree` : ""}${a.fault_domain.evidence.length ? ` · ${esc(a.fault_domain.evidence.join("; "))}` : ""}</p>
    ${a.findings.map((x) => `<div class="finding ${esc(x.confidence)}"><b>${esc(x.hypothesis)}</b> <span class="badge ${esc(x.confidence)}">${esc(x.confidence)}</span>
      <ul>${x.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}${x.contradictions.map((c) => `<li class="muted">Against this: ${esc(c)}</li>`).join("")}<li class="muted">Next step: ${esc(x.next_step)}</li></ul></div>`).join("")}
    ${a.missing_signals.length ? `<p class="muted">Not measured this time:</p><ul class="muted">${a.missing_signals.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""}
    <details><summary>Measurement details</summary>
      <table><tr><th>Measured to</th><th>Normally</th><th>During the problem</th><th>Afterwards</th></tr>${probes}</table>
      <p class="footnote">Normally ${a.windows.baseline.start.slice(11, 19)}–${a.windows.baseline.end.slice(11, 19)} UTC · during ${a.windows.during.start.slice(11, 19)}–${a.windows.during.end.slice(11, 19)} UTC</p>
      ${a.channel_deltas.length ? `<p class="footnote">Modem: ${a.channel_deltas.length} channels compared. ${deltas.length ? "Channels that changed: " + deltas.map((d) => `${esc(d.direction.slice(0, 2))}${esc(d.channel_id)} unc ${esc(d.uncorrected ?? "–")}${d.t3_timeouts ? " T3+" + esc(d.t3_timeouts) : ""}${d.t4_timeouts ? " T4+" + esc(d.t4_timeouts) : ""}`).join(", ") : "No counter moved."}</p>` : ""}
    </details>`;
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
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => series && refreshSeries());
})();
