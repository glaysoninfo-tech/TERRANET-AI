/* TERRANET AI — IHC Web Operacional (PIT-001) */

const state = {
  profiles: [],
  activeProfile: null,
  sandbox: null,
  devices: [],
  telemetry: [],
  commands: [],
  audit: [],
  positions: [],
  track: [],
};

const $ = (id) => document.getElementById(id);

function toast(msg, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("toast-stack").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (state.activeProfile && !options.noAuth) {
    headers["Authorization"] = `Bearer ${state.activeProfile.api_key}`;
  }
  const resp = await fetch(path, { ...options, headers });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    const detail = data?.detail || resp.statusText;
    const err = new Error(`HTTP ${resp.status}: ${detail}`);
    err.status = resp.status;
    err.detail = detail;
    throw err;
  }
  return data;
}

function fmtTime(isoStr) {
  if (!isoStr) return "—";
  return isoStr.replace("T", " ").slice(11, 19);
}

function badgeClass(status) {
  const s = String(status || "").toUpperCase();
  if (["ACTIVE", "VERIFIED", "OK", "ONLINE", "NORMAL", "APPLIED"].includes(s)) return "badge-ok";
  if (["REJECTED", "EXPIRED", "FAILED", "CRITICAL", "ALARM", "OFFLINE", "DECOMMISSIONED"].includes(s)) return "badge-danger";
  if (["DEGRADED", "MAINTENANCE", "AUTHORIZED", "DISPATCHED", "UNKNOWN", "PARTIAL"].includes(s)) return "badge-warn";
  return "badge-info";
}

function renderTiles(containerId, items) {
  $(containerId).innerHTML = items
    .map(
      ([label, val]) =>
        `<div class="metric-tile"><div class="m-label">${label}</div><div class="m-val">${val}</div></div>`
    )
    .join("");
}

/* ---------------- H01 SVG CHART ---------------- */
function renderH01Chart(events) {
  const box = $("h01-chart");
  const pts = events
    .filter((e) => e.event_type === "telemetry" && e.measurements?.water_level_m != null)
    .slice(0, 24)
    .reverse();

  if (!pts.length) {
    box.innerHTML = `<div class="muted" style="padding:70px 12px;text-align:center;">Sem medições H01 visíveis para este cliente.</div>`;
    return;
  }

  const w = 540, h = 170, padL = 38, padR = 16, padT = 14, padB = 24;
  const maxVal = Math.max(5.0, ...pts.map((p) => p.measurements.water_level_m + 0.4));
  const minVal = 0;
  const xStep = pts.length > 1 ? (w - padL - padR) / (pts.length - 1) : 0;
  const yOf = (v) => padT + ((maxVal - v) / (maxVal - minVal)) * (h - padT - padB);

  const thresholdY = yOf(4.0);
  const hystY = yOf(3.7);
  const poly = pts
    .map((p, i) => `${(padL + i * xStep).toFixed(1)},${yOf(p.measurements.water_level_m).toFixed(1)}`)
    .join(" ");

  const circles = pts
    .map((p, i) => {
      const cx = (padL + i * xStep).toFixed(1);
      const cy = yOf(p.measurements.water_level_m).toFixed(1);
      const col = p.device_id.endsWith("-B") ? "#a78bfa" : "#38bdf8";
      return `<circle cx="${cx}" cy="${cy}" r="3.8" fill="${col}"><title>${p.device_id}: ${p.measurements.water_level_m} m (${fmtTime(p.observed_at)})</title></circle>`;
    })
    .join("");

  box.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="170">
      <line x1="${padL}" y1="${thresholdY}" x2="${w - padR}" y2="${thresholdY}" stroke="#ef4444" stroke-dasharray="5 4" stroke-width="1.3"/>
      <text x="${w - padR - 4}" y="${thresholdY - 4}" text-anchor="end" fill="#f87171" font-size="10">Limiar cheia 4.0 m</text>
      <line x1="${padL}" y1="${hystY}" x2="${w - padR}" y2="${hystY}" stroke="#f59e0b" stroke-dasharray="3 4" stroke-width="1"/>
      <text x="${w - padR - 4}" y="${hystY + 11}" text-anchor="end" fill="#fbbf24" font-size="9">Histerese 3.7 m</text>
      <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${h - padB}" stroke="#243452"/>
      <line x1="${padL}" y1="${h - padB}" x2="${w - padR}" y2="${h - padB}" stroke="#243452"/>
      <text x="${padL - 5}" y="${padT + 8}" text-anchor="end" fill="#8da2c0" font-size="10">${maxVal.toFixed(1)}m</text>
      <text x="${padL - 5}" y="${h - padB}" text-anchor="end" fill="#8da2c0" font-size="10">0m</text>
      <polyline fill="none" stroke="#38bdf8" stroke-width="2" points="${poly}"/>
      ${circles}
      <text x="${padL + 4}" y="${h - 6}" fill="#38bdf8" font-size="10">● Alfa (JSON cm)</text>
      <text x="${padL + 115}" y="${h - 6}" fill="#a78bfa" font-size="10">● Beta (Binário mm)</text>
    </svg>`;
}

/* ---------------- V01 GEO MAP (SVG) ---------------- */
function renderGeoMap(track, positions) {
  const box = $("v01-map");
  if (!track.length) {
    box.innerHTML = `<div class="muted" style="padding:105px 12px;text-align:center;">Sem posições GNSS visíveis para este cliente.</div>`;
    return;
  }
  const w = 520, h = 250, pad = 34;
  const lats = track.map((p) => p.lat);
  const lons = track.map((p) => p.lon);
  const minLat = Math.min(...lats) - 0.002;
  const maxLat = Math.max(...lats) + 0.002;
  const minLon = Math.min(...lons) - 0.002;
  const maxLon = Math.max(...lons) + 0.002;

  const xOf = (lon) => pad + ((lon - minLon) / (maxLon - minLon || 1)) * (w - 2 * pad);
  const yOf = (lat) => h - pad - ((lat - minLat) / (maxLat - minLat || 1)) * (h - 2 * pad);

  const line = track.map((p) => `${xOf(p.lon).toFixed(1)},${yOf(p.lat).toFixed(1)}`).join(" ");
  const last = track[track.length - 1];
  const lx = xOf(last.lon).toFixed(1), ly = yOf(last.lat).toFixed(1);

  const dots = track
    .slice(0, -1)
    .map(
      (p) =>
        `<circle cx="${xOf(p.lon).toFixed(1)}" cy="${yOf(p.lat).toFixed(1)}" r="3.2" fill="#38bdf8" opacity="0.75"><title>${p.lat.toFixed(5)}, ${p.lon.toFixed(5)} · ${p.speed_kmh ?? 0} km/h</title></circle>`
    )
    .join("");

  box.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="250">
      <defs>
        <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
          <path d="M 32 0 L 0 0 0 32" fill="none" stroke="#182640" stroke-width="1"/>
        </pattern>
      </defs>
      <rect width="${w}" height="${h}" fill="url(#grid)" rx="6"/>
      <polyline fill="none" stroke="#22c55e" stroke-width="2.5" stroke-dasharray="6 3" points="${line}"/>
      ${dots}
      <circle cx="${lx}" cy="${ly}" r="11" fill="rgba(34,197,94,0.22)"/>
      <circle cx="${lx}" cy="${ly}" r="5.5" fill="#22c55e" stroke="#fff" stroke-width="1.5"/>
      <text x="${Number(lx) + 10}" y="${Number(ly) - 6}" fill="#e6eefb" font-size="11" font-weight="700">TN-TRACK-V01 (${last.speed_kmh ?? 0} km/h)</text>
      <text x="10" y="16" fill="#8da2c0" font-size="10">WGS84 · PostGIS/Haversine · ${track.length} pontos</text>
      <text x="10" y="${h - 8}" fill="#8da2c0" font-size="10">Lat ${last.lat.toFixed(5)} / Lon ${last.lon.toFixed(5)}</text>
    </svg>`;
}

/* ---------------- REFRESH ALL ---------------- */
async function refresh() {
  if (!state.activeProfile) return;
  try {
    const [sandbox, devices, telemetry, commands, audit, positions] = await Promise.all([
      api("/api/v1/sandbox/status", { noAuth: true }),
      api("/api/v1/devices"),
      api("/api/v1/telemetry?limit=80"),
      api("/api/v1/commands?limit=50"),
      api("/api/v1/audit?limit=60"),
      api("/api/v1/geospatial/positions"),
    ]);
    state.sandbox = sandbox;
    state.devices = devices;
    state.telemetry = telemetry;
    state.commands = commands;
    state.audit = audit;
    state.positions = positions;

    const hasTrack = devices.some((d) => d.device_id === "TN-TRACK-V01");
    state.track = hasTrack ? await api("/api/v1/geospatial/tracks/TN-TRACK-V01?limit=50") : [];
    render();
  } catch (err) {
    toast(err.message, "danger");
  }
}

function render() {
  const sb = state.sandbox;
  const prof = state.activeProfile;
  const isC1 = prof.tenant_id === "CLIENTE-001";

  // Topbar WAN & Outbox
  $("wan-dot").className = `dot ${sb.wan_online ? "dot-ok" : "dot-danger"}`;
  $("wan-label").textContent = sb.wan_online ? "WAN Online" : "WAN Offline (EDGE Autônomo)";
  $("outbox-count").textContent = sb.edge_pending;
  $("edge-status-badge").textContent = sb.wan_online ? "ONLINE" : "OFFLINE";
  $("edge-status-badge").className = `badge ${sb.wan_online ? "badge-ok" : "badge-danger"}`;

  // KPIs
  $("kpi-tenant").textContent = prof.tenant_id;
  $("kpi-role").textContent = `${prof.username} · função: ${prof.role.toUpperCase()}`;
  $("kpi-devices").textContent = state.devices.length;
  $("kpi-devices-sub").textContent = state.devices.map((d) => d.device_id).join(", ") || "Nenhum";

  const hydroEvents = state.telemetry.filter((e) => e.measurements?.water_level_m != null);
  const latestHydro = hydroEvents[0];
  $("kpi-level").textContent = latestHydro ? `${latestHydro.measurements.water_level_m.toFixed(2)} m` : "—";
  const alarmActive = isC1 && sb.rule_h01_state?.active;
  $("h01-alarm-badge").textContent = !isC1 ? "ISOLADO" : alarmActive ? "ALARME CHEIA" : "NORMAL";
  $("h01-alarm-badge").className = `badge ${!isC1 ? "badge-info" : alarmActive ? "badge-danger" : "badge-ok"}`;

  const plc = sb.plc;
  $("kpi-pump").textContent = !isC1 ? "—" : plc.pump_running ? "LIGADA" : "PARADA";
  $("kpi-pump-sub").textContent = !isC1 ? "Outro cliente" : plc.interlocks_ok ? "Permissivos OK" : `Bloqueio: ${plc.interlock_reason}`;
  $("p01-state-badge").textContent = !isC1 ? "ISOLADO" : plc.pump_running ? "LIGADA" : "PARADA";
  $("p01-state-badge").className = `badge ${!isC1 ? "badge-info" : plc.pump_running ? "badge-ok" : "badge-warn"}`;

  $("kpi-events").textContent = `${sb.core_total_events} / ${sb.edge_total_events}`;
  $("kpi-outbox-sub").textContent = `${sb.edge_pending} pendentes · ${sb.edge_local_alarms} alarmes locais`;

  // H01 chart & metrics
  renderH01Chart(hydroEvents);
  renderTiles("h01-metrics", [
    ["Último Nível", latestHydro ? `${latestHydro.measurements.water_level_m.toFixed(2)} m` : "—"],
    ["Bateria", latestHydro?.measurements?.battery_voltage_v != null ? `${latestHydro.measurements.battery_voltage_v.toFixed(2)} V` : "—"],
    ["Qualidade", latestHydro?.quality?.water_level_m || "—"],
    ["Origem", latestHydro?.device_id || "—"],
  ]);

  // P01 metrics & interlocks
  renderTiles("p01-metrics", [
    ["Pressão", isC1 ? `${plc.discharge_pressure_bar.toFixed(2)} bar` : "—"],
    ["Corrente", isC1 ? `${plc.motor_current_a.toFixed(1)} A` : "—"],
    ["Vazão", isC1 ? `${plc.flow_rate_m3h.toFixed(1)} m³/h` : "—"],
    ["Sucção", isC1 ? `${plc.suction_level_m.toFixed(2)} m` : "—"],
    ["Acionamentos", isC1 ? String(plc.physical_actuations) : "—"],
  ]);
  $("il-dryrun").checked = plc.dry_run_interlock_ok;
  $("il-remote").checked = plc.remote_mode_enabled;
  $("il-overload").checked = plc.overload_fault;
  $("il-estop").checked = plc.emergency_stop_active;

  // V01 map & telemetry
  renderGeoMap(state.track, state.positions);
  const lastPos = state.track[state.track.length - 1];
  renderTiles("v01-metrics", [
    ["Latitude", lastPos ? lastPos.lat.toFixed(5) : "—"],
    ["Longitude", lastPos ? lastPos.lon.toFixed(5) : "—"],
    ["Velocidade", lastPos ? `${lastPos.speed_kmh ?? 0} km/h` : "—"],
    ["Pontos", String(state.track.length)],
  ]);
  $("v01-history").innerHTML = state.track
    .slice(-6)
    .reverse()
    .map(
      (p) =>
        `<tr><td>${fmtTime(p.observed_at)}</td><td>${p.lat.toFixed(5)}</td><td>${p.lon.toFixed(5)}</td><td>${p.speed_kmh ?? 0} km/h</td></tr>`
    )
    .join("");

  // Outbox metrics
  renderTiles("outbox-metrics", [
    ["PENDING", sb.outbox.PENDING || 0],
    ["RETRY", sb.outbox.RETRY || 0],
    ["SYNCED", sb.outbox.SYNCED || 0],
    ["FAILED", sb.outbox.FAILED || 0],
  ]);

  // Core events table
  $("core-events-body").innerHTML = state.telemetry
    .slice(0, 25)
    .map((e) => {
      const summary =
        e.event_type === "event"
          ? `${e.attributes?.rule_id}: ${e.attributes?.alarm_status}`
          : e.event_type === "command_result"
            ? `${e.attributes?.command_id} → ${e.attributes?.state}`
            : Object.entries(e.measurements || {})
                .slice(0, 3)
                .map(([k, v]) => `${k}=${v}`)
                .join(", ");
      return `<tr>
        <td><code>${e.event_id.slice(0, 28)}</code></td>
        <td>${e.device_id}</td>
        <td><span class="badge ${badgeClass(e.event_type === "event" ? "ALARM" : "OK")}">${e.event_type}</span></td>
        <td>${summary || "—"}</td>
        <td>${fmtTime(e.observed_at)}</td>
      </tr>`;
    })
    .join("");

  // Devices table
  $("devices-body").innerHTML = state.devices
    .map((d) => {
      const nextState = d.operational_status === "ACTIVE" ? "MAINTENANCE" : d.operational_status === "MAINTENANCE" ? "ACTIVE" : null;
      const actionBtn = nextState
        ? `<button class="btn btn-ghost btn-sm" onclick="transitionDevice('${d.device_id}','${nextState}')">→ ${nextState}</button>`
        : "—";
      return `<tr>
        <td><strong>${d.device_id}</strong></td>
        <td>${d.site_id}</td>
        <td>${d.category}</td>
        <td>${d.manufacturer} ${d.model}</td>
        <td><code>${d.adapter_id}</code></td>
        <td><span class="badge ${badgeClass(d.operational_status)}">${d.operational_status}</span></td>
        <td>${fmtTime(d.last_seen_at)}</td>
        <td>${actionBtn}</td>
      </tr>`;
    })
    .join("");

  // Commands table
  $("commands-body").innerHTML = state.commands
    .map(
      (c) => `<tr>
        <td><code>${c.command_id}</code></td>
        <td>${c.device_id}</td>
        <td>${c.operation}</td>
        <td>${c.requested_by}</td>
        <td><span class="badge ${badgeClass(c.state)}">${c.state}</span></td>
        <td>${c.reason || "—"}</td>
      </tr>`
    )
    .join("");

  // Audit table
  $("audit-body").innerHTML = state.audit
    .map(
      (a) => `<tr>
        <td>${fmtTime(a.at)}</td>
        <td>${a.actor}</td>
        <td><code>${a.action}</code></td>
        <td>${a.target}</td>
        <td><span class="badge ${badgeClass(a.outcome)}">${a.outcome}</span></td>
      </tr>`
    )
    .join("");
}

window.transitionDevice = async (deviceId, nextState) => {
  try {
    await api(`/api/v1/devices/${deviceId}/transition`, {
      method: "POST",
      body: JSON.stringify({ state: nextState, reason: "alteração pela IHC" }),
    });
    toast(`${deviceId} → ${nextState}`, "ok");
    await refresh();
  } catch (err) {
    toast(`Transição negada: ${err.detail || err.message}`, "danger");
  }
};

async function sendPumpCommand(operation) {
  const ttl = Number($("p01-ttl").value) || 30;
  $("p01-cmd-feedback").textContent = "Emitindo comando no TERRANET CORE...";
  try {
    const cmd = await api("/api/v1/commands", {
      method: "POST",
      body: JSON.stringify({
        device_id: "TN-PUMP-P01",
        operation,
        ttl_s: ttl,
        require_edge_online: true,
      }),
    });
    const dispatch = await api("/api/v1/sandbox/edge/dispatch-commands", {
      method: "POST",
      noAuth: true,
    });
    const out = dispatch.outcomes?.[0];
    if (out) {
      const ok = out.state === "VERIFIED";
      $("p01-cmd-feedback").textContent = `${cmd.command_id}: ${out.state} (${out.reason})`;
      toast(`Comando ${operation}: ${out.state} — ${out.reason}`, ok ? "ok" : "warn");
    } else {
      $("p01-cmd-feedback").textContent = `${cmd.command_id}: ${cmd.state}`;
      toast(`Comando ${cmd.command_id} emitido (${cmd.state})`, "info");
    }
  } catch (err) {
    $("p01-cmd-feedback").textContent = `Bloqueado: ${err.detail || err.message}`;
    toast(`Comando bloqueado (${err.status}): ${err.detail || err.message}`, "danger");
  }
  await refresh();
}

async function init() {
  // Tabs
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`tab-${btn.dataset.tab}`).classList.add("active");
    });
  });

  // Load demo profiles
  const { profiles } = await api("/api/v1/sandbox/profiles", { noAuth: true });
  state.profiles = profiles;
  state.activeProfile = profiles[0];
  $("profile-select").innerHTML = profiles
    .map((p, idx) => `<option value="${idx}">${p.label}</option>`)
    .join("");
  $("profile-select").addEventListener("change", (e) => {
    state.activeProfile = state.profiles[Number(e.target.value)];
    toast(`Sessão alterada para ${state.activeProfile.label}`, "info");
    refresh();
  });

  // H01 simulator
  $("h01-slider").addEventListener("input", (e) => {
    $("h01-slider-val").textContent = `${Number(e.target.value).toFixed(2)} m`;
  });
  $("btn-send-uplink").addEventListener("click", async () => {
    const res = await api("/api/v1/sandbox/uplink", {
      method: "POST",
      noAuth: true,
      body: JSON.stringify({
        manufacturer: $("h01-mfg").value,
        water_level_m: Number($("h01-slider").value),
        duplicate: false,
      }),
    });
    toast(
      `Uplink processado (+${res.new_local_events} evento local · ${res.pending_outbox} pendente(s) no Outbox)`,
      "ok"
    );
    await refresh();
  });
  $("btn-send-dup").addEventListener("click", async () => {
    const res = await api("/api/v1/sandbox/uplink", {
      method: "POST",
      noAuth: true,
      body: JSON.stringify({
        manufacturer: $("h01-mfg").value,
        water_level_m: Number($("h01-slider").value),
        duplicate: true,
      }),
    });
    toast(
      res.duplicate_detected
        ? `PIT-T04: Uplink duplicado (${res.deduplication_id}) ignorado — 0 duplicatas!`
        : `Novo uplink enviado (${res.deduplication_id})`,
      res.duplicate_detected ? "ok" : "info"
    );
    await refresh();
  });

  // P01 interlocks & commands
  const syncInterlocks = async () => {
    await api("/api/v1/sandbox/plc/interlocks", {
      method: "POST",
      noAuth: true,
      body: JSON.stringify({
        dry_run_interlock_ok: $("il-dryrun").checked,
        remote_mode_enabled: $("il-remote").checked,
        overload_fault: $("il-overload").checked,
        emergency_stop_active: $("il-estop").checked,
      }),
    });
    toast("Intertravamentos físicos do CLP atualizados", "info");
    await refresh();
  };
  ["il-dryrun", "il-remote", "il-overload", "il-estop"].forEach((id) =>
    $(id).addEventListener("change", syncInterlocks)
  );
  $("btn-start-pump").addEventListener("click", () => sendPumpCommand("start_pump"));
  $("btn-stop-pump").addEventListener("click", () => sendPumpCommand("stop_pump"));
  $("btn-expired-cmd").addEventListener("click", async () => {
    const res = await api("/api/v1/sandbox/test-expired-command", {
      method: "POST",
      noAuth: true,
      body: JSON.stringify({ device_id: "TN-PUMP-P01", operation: "start_pump" }),
    });
    toast(`PIT-T11: ${res.command_id} → ${res.state} (${res.reason})`, "warn");
    await refresh();
  });

  // V01 track step
  $("btn-track-step").addEventListener("click", async () => {
    const res = await api("/api/v1/sandbox/track/step", { method: "POST", noAuth: true });
    toast(`V01 GNSS recebido (${res.lat.toFixed(5)}, ${res.lon.toFixed(5)} · ${res.speed_kmh} km/h)`, "ok");
    await refresh();
  });

  // EDGE & Outbox controls
  $("btn-wan-off").addEventListener("click", async () => {
    await api("/api/v1/sandbox/edge/wan", { method: "POST", noAuth: true, body: JSON.stringify({ online: false }) });
    toast("Internet derrubada: EDGE operando em modo autônomo local (PIT-T06)", "warn");
    await refresh();
  });
  $("btn-wan-on").addEventListener("click", async () => {
    await api("/api/v1/sandbox/edge/wan", { method: "POST", noAuth: true, body: JSON.stringify({ online: true }) });
    toast("Internet restabelecida — pronto para sincronizar o Outbox", "ok");
    await refresh();
  });
  $("btn-sync-now").addEventListener("click", async () => {
    try {
      const res = await api("/api/v1/sandbox/edge/sync", {
        method: "POST",
        noAuth: true,
        body: JSON.stringify({ simulate_drop_after_commit: false }),
      });
      toast(`Sincronização concluída · pendentes no Outbox: ${res.pending_outbox}`, "ok");
    } catch (err) {
      toast(err.detail || err.message, "danger");
    }
    await refresh();
  });
  $("btn-sync-drop").addEventListener("click", async () => {
    try {
      const res = await api("/api/v1/sandbox/edge/sync", {
        method: "POST",
        noAuth: true,
        body: JSON.stringify({ simulate_drop_after_commit: true }),
      });
      toast(
        `PIT-T08: Persistido no CORE (${res.core_total_events}), ACK perdido! Pendentes no EDGE: ${res.pending_outbox}. Clique em Sincronizar Outbox para validar idempotência.`,
        "warn"
      );
    } catch (err) {
      toast(err.detail || err.message, "danger");
    }
    await refresh();
  });
  $("btn-refresh-events").addEventListener("click", refresh);

  await refresh();
}

init();
