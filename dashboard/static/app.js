const runsBody = document.getElementById("runsBody");
const jobsBody = document.getElementById("jobsBody");
const scriptText = document.getElementById("scriptText");
const scriptPath = document.getElementById("scriptPath");
const triggerInfo = document.getElementById("triggerInfo");
const jobLog = document.getElementById("jobLog");
const modeSelect = document.getElementById("modeSelect");
const providerSelect = document.getElementById("providerSelect");
const triggerForm = document.getElementById("triggerForm");
const refreshBtn = document.getElementById("refreshBtn");
const toast = document.getElementById("toast");

let selectedJobId = null;

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.style.background = isError ? "#7f1d1d" : "#0f172a";
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 1800);
}

function statusChip(value) {
  const normalized = (value || "unknown").toLowerCase();
  const safe =
    normalized === "completed" || normalized === "success"
      ? "success"
      : normalized === "running"
      ? "running"
      : normalized === "failed"
      ? "failed"
      : normalized === "pending"
      ? "pending"
      : "pending";
  return `<span class="status-chip status-${safe}">${value || "unknown"}</span>`;
}

function safeText(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

async function fetchJson(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    throw new Error(`Request failed: ${resp.status}`);
  }
  return resp.json();
}

function renderTriggerInfo(overview) {
  const trigger = overview.triggers || {};
  const workflow = trigger.github_workflow || {};
  const webhook = trigger.local_webhook || {};
  const localCron = trigger.local_cron || {};

  const cronText = Array.isArray(workflow.schedule_cron_utc) && workflow.schedule_cron_utc.length
    ? workflow.schedule_cron_utc.join(", ")
    : "No cron configured";
  const localCronText = Array.isArray(localCron.entries) && localCron.entries.length
    ? localCron.entries.join("\n")
    : "No local cron entry found";

  triggerInfo.innerHTML = `
    <div class="meta-row">
      <div class="meta-key">GitHub Workflow</div>
      <div class="meta-value">${safeText(workflow.name || "")}</div>
    </div>
    <div class="meta-row">
      <div class="meta-key">Cloud Cron (UTC)</div>
      <div class="meta-value">${safeText(cronText)}</div>
    </div>
    <div class="meta-row">
      <div class="meta-key">Local Webhook</div>
      <div class="meta-value">${safeText(webhook.path || "")}</div>
    </div>
    <div class="meta-row">
      <div class="meta-key">Local Cron</div>
      <div class="meta-value">${safeText(localCronText)}</div>
    </div>
  `;
}

function renderRuns(data) {
  const runs = data.runs || [];
  if (!runs.length) {
    runsBody.innerHTML = `<tr><td colspan="7">No runs yet.</td></tr>`;
    return;
  }
  runsBody.innerHTML = runs
    .map(
      (run) => `
      <tr>
        <td class="run-id">${safeText(run.run_id)}</td>
        <td>${statusChip(run.status)}</td>
        <td>${safeText(run.scene_count)}</td>
        <td>${statusChip(run.cloud_status || run.cloud_provider || "pending")}</td>
        <td>${safeText(run.started_at || "")}</td>
        <td>${safeText(run.ended_at || "")}</td>
        <td title="${safeText(run.cloud_destination || "")}">${safeText(
        (run.cloud_destination || "").slice(0, 78)
      )}</td>
      </tr>
    `
    )
    .join("");
}

function renderJobs(data) {
  const jobs = data.jobs || [];
  if (!jobs.length) {
    jobsBody.innerHTML = `<tr><td colspan="6">No trigger jobs yet.</td></tr>`;
    return;
  }
  jobsBody.innerHTML = jobs
    .map((job) => {
      const selectedClass = selectedJobId === job.id ? ' style="background:#fff7ed;"' : "";
      return `
      <tr data-job-id="${safeText(job.id)}"${selectedClass}>
        <td class="run-id">${safeText(job.id)}</td>
        <td>${safeText(job.mode)}</td>
        <td>${safeText(job.provider)}</td>
        <td>${statusChip(job.status)}</td>
        <td>${safeText(job.requested_at || "")}</td>
        <td>${safeText(job.finished_at || "")}</td>
      </tr>
    `;
    })
    .join("");

  jobsBody.querySelectorAll("tr[data-job-id]").forEach((row) => {
    row.addEventListener("click", async () => {
      const jobId = row.getAttribute("data-job-id");
      selectedJobId = jobId;
      await loadJobLog(jobId);
      await refreshJobsOnly();
    });
  });
}

async function loadJobLog(jobId) {
  try {
    const data = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}/log`);
    jobLog.textContent = data.log || "No log output yet.";
  } catch (err) {
    jobLog.textContent = `Could not load log: ${err.message}`;
  }
}

async function refreshOverview() {
  const data = await fetchJson("/api/overview");
  renderTriggerInfo(data);
  const script = data.script_panel || {};
  scriptPath.textContent = script.script_path || "Script path unavailable";
  scriptText.textContent = script.script_text || "No script content found.";
}

async function refreshRunsOnly() {
  const data = await fetchJson("/api/runs");
  renderRuns(data);
}

async function refreshJobsOnly() {
  const data = await fetchJson("/api/jobs");
  renderJobs(data);
}

async function refreshAll() {
  try {
    await Promise.all([refreshOverview(), refreshRunsOnly(), refreshJobsOnly()]);
    if (selectedJobId) {
      await loadJobLog(selectedJobId);
    }
  } catch (err) {
    showToast(`Refresh failed: ${err.message}`, true);
  }
}

triggerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const dryRun = modeSelect.value === "dry";
  const provider = providerSelect.value;
  try {
    const data = await fetchJson("/api/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun, provider }),
    });
    selectedJobId = data.id;
    showToast(`Trigger started: ${data.id}`);
    await refreshAll();
    await loadJobLog(data.id);
  } catch (err) {
    showToast(`Trigger failed: ${err.message}`, true);
  }
});

refreshBtn.addEventListener("click", async () => {
  await refreshAll();
  showToast("Dashboard refreshed");
});

refreshAll();
setInterval(refreshJobsOnly, 7000);
setInterval(refreshRunsOnly, 12000);
