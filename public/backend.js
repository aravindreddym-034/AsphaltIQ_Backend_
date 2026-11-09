"use strict";

// Optional server connection. Existing local inspection continues to work.
let activeRecording = null;
let savedOffset = 0;
let savedTotal = 0;
let backendBusy = false;
const serverControls = ["saveServer", "refreshServer", "openServer", "deleteServer",
  "previousServer", "nextServer", "rerunServer", "exportServer"];
$("apiBase").value = location.protocol === "file:" ? "http://127.0.0.1:8000" : location.origin;

function backendButtons() {
  serverControls.forEach(id => $(id).disabled = backendBusy);
  ["rerunServer", "exportServer"].forEach(id => $(id).disabled = backendBusy || !activeRecording);
  ["openServer", "deleteServer"].forEach(id => $(id).disabled = backendBusy || !$("savedRecordings").value);
  $("previousServer").disabled = backendBusy || savedOffset === 0;
  $("nextServer").disabled = backendBusy || savedOffset + 50 >= savedTotal;
}

async function api(path, options = {}, asBlob = false) {
  const base = new URL($("apiBase").value.trim());
  if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.search || base.hash)
    throw new Error("Enter a backend HTTP or HTTPS address without credentials, query, or fragment.");
  const key = $("teamKey").value.trim();
  if (!key) throw new Error("Enter your team access key first.");
  const response = await fetch(base.href.replace(/\/$/, "") + path, {
    ...options, headers: {"X-Team-Key": key, ...options.headers}
  });
  if (!response.ok) {
    let message = `Server returned ${response.status}.`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return asBlob ? response.blob() : response.json();
}

async function backendAction(action) {
  if (backendBusy) return;
  backendBusy = true;
  backendButtons();
  $("load").disabled = true;
  $("detect").disabled = true;
  $("backendStatus").textContent = "Contacting backend…";
  try {
    await action();
  } catch (error) {
    $("backendStatus").textContent = "Could not complete backend action: " + error.message;
  } finally {
    backendBusy = false;
    $("load").disabled = false;
    $("detect").disabled = false;
    backendButtons();
  }
}

function thresholdForServer() {
  const entered = $("serverThreshold").value.trim();
  const value = entered === "" ? null : Number(entered);
  if (value !== null && (!Number.isFinite(value) || value <= 0))
    throw new Error("Use a positive RMS threshold, or clear it for automatic selection.");
  return value;
}

function serverDetectionText(detection) {
  if (detection.status !== "complete") return detection.message;
  const located = detection.candidates.filter(e => e.fix).length;
  return `Backend threshold: ${detection.threshold.toFixed(2)} m/s² (${detection.threshold_mode}).\n` +
    `${detection.candidates.length} unverified candidate events; ${located} have GPS locations.\n` +
    "Saved on the backend. These events have not been validated as potholes.";
}

function showServerRecord(record) {
  ++loadVersion;
  clearCandidates();
  samples = record.samples;
  fixes = record.fixes;
  matches = samples.map(sample => nearestFix(sample.t));
  activeRecording = record.id;
  const summary = record.summary;
  $("sampleCount").textContent = samples.length.toLocaleString();
  $("gpsCount").textContent = fixes.length.toLocaleString();
  $("matchedCount").textContent = summary.matched_readings.toLocaleString();
  $("accuracy").value = summary.max_gps_error_m;
  $("threshold").value = record.detection.threshold_mode === "manual" ? record.detection.threshold : "";
  $("serverThreshold").value = $("threshold").value;
  $("recordingName").value = record.name;
  $("slider").max = samples.length - 1;
  $("slider").value = 0;
  $("results").hidden = false;
  prepareChart();
  setupMap();
  showReading();
  candidates = record.detection.candidates;
  if (record.detection.status === "complete") renderCandidates();
  $("eventStatus").textContent = serverDetectionText(record.detection);
  if (window.L && fixes.length) $("mapStatus").textContent = "Saved route and unverified candidate events loaded.";
  $("status").textContent = `Opened saved recording: ${record.name}.\n` +
    `${summary.acceleration_readings.toLocaleString()} readings; ${summary.accepted_gps_fixes} accepted GPS fixes.\n` +
    `${summary.rejected_acceleration_rows} acceleration rows and ${summary.rejected_gps_rows} GPS rows excluded.\n` +
    "Nearest GPS fix within 2 seconds, inside the accepted GPS span. No position extrapolation.";
}

async function refreshRecordings(selectedId) {
  const page = await api(`/api/recordings?limit=50&offset=${savedOffset}`);
  savedTotal = page.total;
  const select = $("savedRecordings");
  select.replaceChildren();
  page.items.forEach(record => {
    const option = document.createElement("option");
    option.value = record.id;
    option.textContent = `${record.name} · ${record.candidate_events} candidates · ${new Date(record.created_at).toLocaleString()}`;
    select.appendChild(option);
  });
  if (selectedId && page.items.some(r => r.id === selectedId)) select.value = selectedId;
  $("savedPage").textContent = savedTotal ?
    `Showing ${savedOffset + 1}–${savedOffset + page.items.length} of ${savedTotal} saved recordings.` :
    "No recordings saved yet.";
}

$("saveServer").onclick = () => backendAction(async () => {
  const accel = $("accelFile").files[0];
  const gps = $("gpsFile").files[0];
  if (!accel || !gps) throw new Error("Choose both CSV files in section 1 first.");
  if (accel.size > 20 * 1024 * 1024 || gps.size > 20 * 1024 * 1024)
    throw new Error("Each CSV must be at most 20 MB.");
  const error = Number($("accuracy").value);
  if (!Number.isFinite(error) || error <= 0 || error > 10000) throw new Error("Enter a valid GPS error limit.");
  const form = new FormData();
  form.append("acceleration", accel);
  form.append("location", gps);
  form.append("name", $("recordingName").value.trim() || "Phone recording");
  form.append("max_gps_error_m", String(error));
  const threshold = thresholdForServer();
  if (threshold !== null) form.append("threshold", String(threshold));
  const saved = await api("/api/recordings", {method: "POST", body: form});
  // Set immediately so a failed refresh does not lose the known created record ID.
  activeRecording = saved.id;
  showServerRecord(await api(`/api/recordings/${saved.id}/data`));
  savedOffset = 0;
  await refreshRecordings(saved.id);
  $("backendStatus").textContent = `Saved and opened “${saved.name}”. Teammates connected to this backend can reopen it.`;
});

$("refreshServer").onclick = () => backendAction(async () => {
  savedOffset = 0;
  await refreshRecordings(activeRecording);
  $("backendStatus").textContent = "Connected. Saved recordings refreshed.";
});
$("openServer").onclick = () => backendAction(async () => {
  const id = $("savedRecordings").value;
  if (!id) throw new Error("Choose a saved recording first.");
  showServerRecord(await api(`/api/recordings/${id}/data`));
  $("backendStatus").textContent = "Saved recording opened. Graph and map use the stored results.";
});
$("deleteServer").onclick = () => {
  const id = $("savedRecordings").value;
  if (!id || !confirm("Delete this saved recording and its candidate results from the backend?")) return;
  backendAction(async () => {
    await api(`/api/recordings/${id}`, {method: "DELETE"});
    if (activeRecording === id) activeRecording = null;
    savedOffset = 0;
    await refreshRecordings();
    $("backendStatus").textContent = "Recording deleted from the backend. Any currently displayed copy remains local.";
  });
};
$("previousServer").onclick = () => backendAction(async () => {
  savedOffset = Math.max(0, savedOffset - 50);
  await refreshRecordings();
  $("backendStatus").textContent = "Previous recordings loaded.";
});
$("nextServer").onclick = () => backendAction(async () => {
  savedOffset += 50;
  await refreshRecordings();
  $("backendStatus").textContent = "Next recordings loaded.";
});
$("savedRecordings").addEventListener("change", backendButtons);
$("rerunServer").onclick = () => backendAction(async () => {
  if (!activeRecording) throw new Error("Open or save a backend recording first.");
  await api(`/api/recordings/${activeRecording}/detect`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({threshold: thresholdForServer()})
  });
  showServerRecord(await api(`/api/recordings/${activeRecording}/data`));
  await refreshRecordings(activeRecording);
  $("backendStatus").textContent = "Candidate detection rerun and saved on the backend.";
});
$("exportServer").onclick = () => backendAction(async () => {
  const blob = await api(`/api/recordings/${activeRecording}/events.csv`, {}, true);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "AsphaltIQ_saved_candidates.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  $("backendStatus").textContent = "Saved candidate CSV exported.";
});
$("load").addEventListener("click", () => {
  activeRecording = null;
  backendButtons();
  $("backendStatus").textContent = "Local inspection mode. Use Save recording to backend to upload and store the selected files.";
});
$("detect").addEventListener("click", () => {
  if (activeRecording) $("backendStatus").textContent =
    "Local candidate preview changed. Click Run and save on backend to update the stored results.";
});
$("serverThreshold").addEventListener("input", () => $("threshold").value = $("serverThreshold").value);
$("threshold").addEventListener("input", () => $("serverThreshold").value = $("threshold").value);
backendButtons();
