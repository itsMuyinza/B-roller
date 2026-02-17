const modeSelect = document.getElementById("modeSelect");
const providerSelect = document.getElementById("providerSelect");
const refreshBtn = document.getElementById("refreshBtn");

const genCharacterBtn = document.getElementById("genCharacterBtn");
const genAllImagesBtn = document.getElementById("genAllImagesBtn");
const genAllVideosBtn = document.getElementById("genAllVideosBtn");
const runFullTriggerBtn = document.getElementById("runFullTriggerBtn");
const downloadLatestPayloadBtn = document.getElementById("downloadLatestPayloadBtn");

const scenesBody = document.getElementById("scenesBody");
const scenesCards = document.getElementById("scenesCards");
const sceneJobsBody = document.getElementById("sceneJobsBody");
const triggerJobsBody = document.getElementById("triggerJobsBody");
const runsBody = document.getElementById("runsBody");

const characterBox = document.getElementById("characterBox");
const characterNameInput = document.getElementById("characterNameInput");
const characterPromptInput = document.getElementById("characterPromptInput");
const characterNotesInput = document.getElementById("characterNotesInput");
const characterRefsInput = document.getElementById("characterRefsInput");
const styleDescriptionInput = document.getElementById("styleDescriptionInput");
const useStyleRefsCheckbox = document.getElementById("useStyleRefsCheckbox");
const saveCharacterConfigBtn = document.getElementById("saveCharacterConfigBtn");
const regenPromptsBtn = document.getElementById("regenPromptsBtn");
const characterPromptPreview = document.getElementById("characterPromptPreview");
const triggerInfo = document.getElementById("triggerInfo");

const scriptPath = document.getElementById("scriptPath");
const scriptEditor = document.getElementById("scriptEditor");
const saveScriptBtn = document.getElementById("saveScriptBtn");

const triggerLog = document.getElementById("triggerLog");
const toast = document.getElementById("toast");

let selectedTriggerJobId = null;
let pollInFlight = false;
let warnedNoWaveSpeed = false;

function isDryRun() {
  return modeSelect.value === "dry";
}

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.style.borderColor = isError ? "#7f1d1d" : "#2b3a57";
  toast.style.background = isError ? "#2b1313" : "#101a30";
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2200);
}

function isSimulatedUrl(url) {
  return typeof url === "string" && url.startsWith("https://dry-run.local/");
}

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function statusChip(status) {
  const normalized = (status || "unknown").toLowerCase();
  const safe = ["completed", "success", "running", "failed", "pending"].includes(normalized)
    ? normalized
    : "unknown";
  return `<span class="status-chip status-${safe}">${esc(status || "unknown")}</span>`;
}

function attachMediaFallbacks(root) {
  if (!root) return;

  root.querySelectorAll("img.preview, img.character-preview").forEach((img) => {
    if (img.dataset.fallbackBound === "1") return;
    img.dataset.fallbackBound = "1";
    img.addEventListener(
      "error",
      () => {
        const placeholder = document.createElement("div");
        placeholder.className = "preview-placeholder";
        placeholder.textContent = img.classList.contains("character-preview")
          ? "Character image unavailable"
          : "Image unavailable";
        img.replaceWith(placeholder);
      },
      { once: true }
    );
  });

  root.querySelectorAll("video.preview").forEach((video) => {
    if (video.dataset.fallbackBound === "1") return;
    video.dataset.fallbackBound = "1";
    video.addEventListener(
      "error",
      () => {
        const placeholder = document.createElement("div");
        placeholder.className = "preview-placeholder";
        placeholder.textContent = "Video unavailable";
        video.replaceWith(placeholder);
      },
      { once: true }
    );
  });
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function downloadFromApi(path, fallbackFilename) {
  const response = await fetch(path);
  const contentType = response.headers.get("content-type") || "";
  if (!response.ok) {
    if (contentType.includes("application/json")) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `Download failed: HTTP ${response.status}`);
    }
    throw new Error(`Download failed: HTTP ${response.status}`);
  }
  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  const header = response.headers.get("content-disposition") || "";
  const match = header.match(/filename=\"?([^\";]+)\"?/i);
  link.download = match?.[1] || fallbackFilename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}

function renderTriggerInfo(overview) {
  const triggers = overview.triggers || {};
  const gh = triggers.github_workflow || {};
  const webhook = triggers.local_webhook || {};
  const localCron = triggers.local_cron || {};

  const cron = Array.isArray(gh.schedule_cron_utc) && gh.schedule_cron_utc.length
    ? gh.schedule_cron_utc.join(", ")
    : "No cron schedule";
  const localCronText = Array.isArray(localCron.entries) && localCron.entries.length
    ? localCron.entries.join("\n")
    : "No local cron entries";

  triggerInfo.innerHTML = `
    <div class="meta-item">
      <div class="meta-key">GitHub Workflow</div>
      <div class="meta-value">${esc(gh.name || "")}</div>
    </div>
    <div class="meta-item">
      <div class="meta-key">Cloud Cron (UTC)</div>
      <div class="meta-value">${esc(cron)}</div>
    </div>
    <div class="meta-item">
      <div class="meta-key">Local Webhook</div>
      <div class="meta-value">${esc(webhook.path || "")}</div>
    </div>
    <div class="meta-item">
      <div class="meta-key">Local Cron</div>
      <div class="meta-value">${esc(localCronText)}</div>
    </div>
  `;
}

function renderCharacter(character) {
  const status = character?.status || "pending";
  const imageUrl = character?.image_url || "";
  const simulated = isSimulatedUrl(imageUrl);
  const taskId = character?.task_id || "-";
  const lastError = character?.last_error || "";

  characterBox.innerHTML = `
    <div class="kv">
      <div class="k">Status</div>
      <div class="v">${statusChip(status)}</div>
    </div>
    <div class="kv">
      <div class="k">Task ID</div>
      <div class="v">${esc(taskId)}</div>
    </div>
    ${
      imageUrl
        ? simulated
          ? `<div class="preview-placeholder">Simulated character preview (dry run)</div>`
          : `<img class="character-preview" src="${esc(imageUrl)}" alt="Character model" />`
        : `<div class="preview-placeholder">No character model image yet</div>`
    }
    ${lastError ? `<div class="kv"><div class="k">Last Error</div><div class="v">${esc(lastError)}</div></div>` : ""}
  `;
  attachMediaFallbacks(characterBox);
}

function renderCharacterConfig(config) {
  if (!config) return;
  if (document.activeElement !== characterNameInput) {
    characterNameInput.value = config.name || "";
  }
  if (document.activeElement !== characterPromptInput) {
    characterPromptInput.value = config.character_model_prompt || "";
  }
  if (document.activeElement !== characterNotesInput) {
    characterNotesInput.value = config.consistency_notes || "";
  }
  if (document.activeElement !== characterRefsInput) {
    characterRefsInput.value = (config.style_reference_images || []).join("\n");
  }
  if (document.activeElement !== styleDescriptionInput) {
    styleDescriptionInput.value = config.style_description || "";
  }
  useStyleRefsCheckbox.checked = Boolean(config.use_style_reference_images);
  characterPromptPreview.innerHTML = `
    <div class="meta-item">
      <div class="meta-key">Style Guardrail</div>
      <div class="meta-value">${esc(config.style_guardrail || "")}</div>
    </div>
    <div class="meta-item">
      <div class="meta-key">Effective Character Prompt</div>
      <div class="meta-value">${esc(config.effective_prompt || "")}</div>
    </div>
  `;
}

function scenePreview(url, type) {
  if (!url) return `<div class="preview-placeholder">No ${type}</div>`;
  if (isSimulatedUrl(url)) return `<div class="preview-placeholder">Simulated ${type} (dry run)</div>`;
  if (type === "image") {
    return `<img class="preview" src="${esc(url)}" alt="Scene image" loading="lazy" />`;
  }
  return `<video class="preview" src="${esc(url)}" controls muted preload="none"></video>`;
}

function sceneActionsMarkup(canDownloadImage, canDownloadVideo) {
  return `
    <div class="actions-cell">
      <button class="btn btn-sm btn-muted" data-action="save">Save Prompt</button>
      <button class="btn btn-sm btn-primary" data-action="image">Generate Image</button>
      <button class="btn btn-sm btn-primary" data-action="video">Generate Video</button>
      <button class="btn btn-sm btn-glass" data-action="download-image" ${canDownloadImage ? "" : "disabled"}>Download Image</button>
      <button class="btn btn-sm btn-glass" data-action="download-video" ${canDownloadVideo ? "" : "disabled"}>Download Video</button>
    </div>
  `;
}

function attachSceneEntryHandlers(entries) {
  entries.forEach((entry) => {
    const sceneId = entry.getAttribute("data-scene-id");
    const saveBtn = entry.querySelector('[data-action="save"]');
    const imageBtn = entry.querySelector('[data-action="image"]');
    const videoBtn = entry.querySelector('[data-action="video"]');
    const downloadImageBtn = entry.querySelector('[data-action="download-image"]');
    const downloadVideoBtn = entry.querySelector('[data-action="download-video"]');

    saveBtn?.addEventListener("click", async () => {
      const narration = entry.querySelector('[data-field="narration"]').value;
      const imagePrompt = entry.querySelector('[data-field="image_prompt"]').value;
      const motionPrompt = entry.querySelector('[data-field="motion_prompt"]').value;
      try {
        await requestJson(`/api/scenes/${encodeURIComponent(sceneId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            narration,
            image_prompt: imagePrompt,
            motion_prompt: motionPrompt,
          }),
        });
        showToast(`Saved ${sceneId}`);
        await refreshScenes();
      } catch (err) {
        showToast(`Save failed (${sceneId}): ${err.message}`, true);
      }
    });

    imageBtn?.addEventListener("click", async () => {
      try {
        await requestJson(`/api/scenes/${encodeURIComponent(sceneId)}/generate-image`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dry_run: isDryRun() }),
        });
        showToast(`Image job started for ${sceneId}${isDryRun() ? " (dry simulation)" : " (live WaveSpeed)"}`);
        await Promise.all([refreshSceneJobs(), refreshScenes(), refreshCharacter()]);
      } catch (err) {
        showToast(`Image trigger failed (${sceneId}): ${err.message}`, true);
      }
    });

    videoBtn?.addEventListener("click", async () => {
      try {
        await requestJson(`/api/scenes/${encodeURIComponent(sceneId)}/generate-video`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dry_run: isDryRun() }),
        });
        showToast(`Video job started for ${sceneId}${isDryRun() ? " (dry simulation)" : " (live WaveSpeed)"}`);
        await Promise.all([refreshSceneJobs(), refreshScenes()]);
      } catch (err) {
        showToast(`Video trigger failed (${sceneId}): ${err.message}`, true);
      }
    });

    downloadImageBtn?.addEventListener("click", async () => {
      if (downloadImageBtn.hasAttribute("disabled")) return;
      try {
        await downloadFromApi(`/api/scenes/${encodeURIComponent(sceneId)}/download/image`, `${sceneId}_image`);
        showToast(`Downloaded image for ${sceneId}`);
      } catch (err) {
        showToast(`Image download failed (${sceneId}): ${err.message}`, true);
      }
    });

    downloadVideoBtn?.addEventListener("click", async () => {
      if (downloadVideoBtn.hasAttribute("disabled")) return;
      try {
        await downloadFromApi(`/api/scenes/${encodeURIComponent(sceneId)}/download/video`, `${sceneId}_video`);
        showToast(`Downloaded video for ${sceneId}`);
      } catch (err) {
        showToast(`Video download failed (${sceneId}): ${err.message}`, true);
      }
    });
  });
}

function renderScenes(items) {
  if (!items.length) {
    scenesBody.innerHTML = `<tr><td colspan="8">No scenes found.</td></tr>`;
    if (scenesCards) {
      scenesCards.innerHTML = `<div class="scenes-empty">No scenes found.</div>`;
    }
    return;
  }

  scenesBody.innerHTML = items
    .map((scene) => {
      const canDownloadImage = Boolean(scene.image_url) && !isSimulatedUrl(scene.image_url);
      const canDownloadVideo = Boolean(scene.video_url) && !isSimulatedUrl(scene.video_url);
      return `
      <tr data-scene-id="${esc(scene.scene_id)}">
        <td>${esc(scene.position)}</td>
        <td>
          <div class="scene-id">${esc(scene.scene_id)}</div>
          <div class="scene-sub">Updated: ${esc(scene.updated_at || "")}</div>
          ${scene.last_error ? `<div class="scene-sub" style="color:#fda4af">${esc(scene.last_error)}</div>` : ""}
        </td>
        <td>
          <textarea class="cell-editor" data-field="narration">${esc(scene.narration || "")}</textarea>
        </td>
        <td>
          <textarea class="cell-editor" data-field="image_prompt">${esc(scene.image_prompt || "")}</textarea>
        </td>
        <td>
          <textarea class="cell-editor" data-field="motion_prompt">${esc(scene.motion_prompt || "")}</textarea>
        </td>
        <td>
          ${statusChip(scene.image_status || "pending")}
          ${scenePreview(scene.image_url, "image")}
        </td>
        <td>
          ${statusChip(scene.video_status || "pending")}
          ${scenePreview(scene.video_url, "video")}
        </td>
        <td>
          ${sceneActionsMarkup(canDownloadImage, canDownloadVideo)}
        </td>
      </tr>
    `;
    })
    .join("");

  if (scenesCards) {
    scenesCards.innerHTML = items
      .map((scene) => {
        const canDownloadImage = Boolean(scene.image_url) && !isSimulatedUrl(scene.image_url);
        const canDownloadVideo = Boolean(scene.video_url) && !isSimulatedUrl(scene.video_url);
        return `
        <article class="scene-card" data-scene-id="${esc(scene.scene_id)}">
          <div class="scene-card-head">
            <div class="scene-card-title">#${esc(scene.position)} ${esc(scene.scene_id)}</div>
            <div class="scene-sub">Updated: ${esc(scene.updated_at || "")}</div>
            ${scene.last_error ? `<div class="scene-sub scene-error">${esc(scene.last_error)}</div>` : ""}
          </div>
          <label class="scene-field">
            <span>Narration</span>
            <textarea class="cell-editor" data-field="narration">${esc(scene.narration || "")}</textarea>
          </label>
          <label class="scene-field">
            <span>Image Prompt</span>
            <textarea class="cell-editor" data-field="image_prompt">${esc(scene.image_prompt || "")}</textarea>
          </label>
          <label class="scene-field">
            <span>Motion Prompt</span>
            <textarea class="cell-editor" data-field="motion_prompt">${esc(scene.motion_prompt || "")}</textarea>
          </label>
          <div class="scene-media-grid">
            <div class="scene-media-box">
              <div class="scene-media-head"><span>Image</span>${statusChip(scene.image_status || "pending")}</div>
              ${scenePreview(scene.image_url, "image")}
            </div>
            <div class="scene-media-box">
              <div class="scene-media-head"><span>Video</span>${statusChip(scene.video_status || "pending")}</div>
              ${scenePreview(scene.video_url, "video")}
            </div>
          </div>
          ${sceneActionsMarkup(canDownloadImage, canDownloadVideo)}
        </article>
      `;
      })
      .join("");
  }

  const tableEntries = Array.from(scenesBody.querySelectorAll("tr[data-scene-id]"));
  const cardEntries = scenesCards ? Array.from(scenesCards.querySelectorAll("article[data-scene-id]")) : [];
  attachSceneEntryHandlers([...tableEntries, ...cardEntries]);
  attachMediaFallbacks(scenesBody);
  if (scenesCards) {
    attachMediaFallbacks(scenesCards);
  }
}

function renderSceneJobs(data) {
  const jobs = data.jobs || [];
  if (!jobs.length) {
    sceneJobsBody.innerHTML = `<tr><td colspan="8">No scene jobs yet.</td></tr>`;
    return;
  }

  sceneJobsBody.innerHTML = jobs
    .map(
      (job) => `
      <tr>
        <td>${esc(job.id)}</td>
        <td>${esc(job.scene_id || "-")}</td>
        <td>${esc(job.stage)}</td>
        <td>${esc(job.mode)}</td>
        <td>${statusChip(job.status)}</td>
        <td>${esc(job.requested_at || "")}</td>
        <td>${esc(job.finished_at || "")}</td>
        <td>${esc(job.error || "")}</td>
      </tr>
    `
    )
    .join("");
}

function renderRuns(data) {
  const runs = data.runs || [];
  if (!runs.length) {
    runsBody.innerHTML = `<tr><td colspan="3">No runs yet.</td></tr>`;
    return;
  }

  runsBody.innerHTML = runs
    .slice(0, 12)
    .map(
      (run) => `
      <tr>
        <td title="${esc(run.run_id)}">
          <button class="btn-link" data-run-download="${esc(run.run_id)}">${esc((run.run_id || "").slice(0, 16))}</button>
        </td>
        <td>${statusChip(run.status)}</td>
        <td>${statusChip(run.cloud_status || "pending")}</td>
      </tr>
    `
    )
    .join("");

  runsBody.querySelectorAll("[data-run-download]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const runId = btn.getAttribute("data-run-download");
      try {
        await downloadFromApi(`/api/runs/${encodeURIComponent(runId)}/download`, `${runId}.json`);
        showToast(`Downloaded run payload: ${runId}`);
      } catch (err) {
        showToast(`Run download failed: ${err.message}`, true);
      }
    });
  });
}

async function loadTriggerLog(jobId) {
  try {
    const data = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/log`);
    triggerLog.textContent = data.log || "No log output yet.";
  } catch (err) {
    triggerLog.textContent = `Could not load log: ${err.message}`;
  }
}

function renderTriggerJobs(data) {
  const jobs = data.jobs || [];
  if (!jobs.length) {
    triggerJobsBody.innerHTML = `<tr><td colspan="6">No trigger jobs yet.</td></tr>`;
    return;
  }

  triggerJobsBody.innerHTML = jobs
    .map((job) => {
      const selected = selectedTriggerJobId === job.id ? ' style="background:rgba(255,138,61,0.1)"' : "";
      return `
      <tr data-job-id="${esc(job.id)}"${selected}>
        <td>${esc(job.id)}</td>
        <td>${esc(job.mode)}</td>
        <td>${esc(job.provider)}</td>
        <td>${statusChip(job.status)}</td>
        <td>${esc(job.requested_at || "")}</td>
        <td>${esc(job.finished_at || "")}</td>
      </tr>
    `;
    })
    .join("");

  triggerJobsBody.querySelectorAll("tr[data-job-id]").forEach((row) => {
    row.addEventListener("click", async () => {
      selectedTriggerJobId = row.getAttribute("data-job-id");
      await loadTriggerLog(selectedTriggerJobId);
      await refreshTriggerJobs();
    });
  });
}

async function refreshOverview() {
  const data = await requestJson("/api/overview");
  renderTriggerInfo(data);
  renderCharacter(data.character || {});
  const runtime = data.runtime || {};
  const wavespeedConfigured = Boolean(runtime.wavespeed_configured);
  const liveOption = modeSelect.querySelector('option[value="live"]');
  if (liveOption) {
    liveOption.disabled = !wavespeedConfigured;
  }
  if (!wavespeedConfigured && modeSelect.value === "live") {
    modeSelect.value = "dry";
    if (!warnedNoWaveSpeed) {
      showToast("WaveSpeed key is missing on this deployment. Switched to dry run.", true);
      warnedNoWaveSpeed = true;
    }
  }

  if (runtime.serverless) {
    runFullTriggerBtn.disabled = true;
    runFullTriggerBtn.title =
      "Full trigger is disabled on Vercel serverless. Use scene-level buttons or local/GitHub workflow.";
  } else {
    runFullTriggerBtn.disabled = false;
    runFullTriggerBtn.title = "";
  }
  const script = data.script_panel || {};
  scriptPath.textContent = script.script_path || "Script path unavailable";
  if (document.activeElement !== scriptEditor) {
    scriptEditor.value = script.script_text || "";
  }
}

async function refreshCharacterConfig() {
  const data = await requestJson("/api/character/config");
  renderCharacterConfig(data);
}

async function refreshScenes() {
  const data = await requestJson("/api/scenes");
  renderScenes(data.scenes || []);
}

async function refreshSceneJobs() {
  const data = await requestJson("/api/scene-jobs");
  renderSceneJobs(data);
}

async function refreshRuns() {
  const data = await requestJson("/api/runs");
  renderRuns(data);
}

async function refreshTriggerJobs() {
  const data = await requestJson("/api/jobs");
  renderTriggerJobs(data);
}

async function refreshCharacter() {
  const data = await requestJson("/api/character");
  renderCharacter(data);
}

async function refreshAll() {
  try {
    await Promise.all([
      refreshOverview(),
      refreshCharacterConfig(),
      refreshScenes(),
      refreshSceneJobs(),
      refreshRuns(),
      refreshTriggerJobs(),
    ]);
    if (selectedTriggerJobId) {
      await loadTriggerLog(selectedTriggerJobId);
    }
  } catch (err) {
    showToast(`Refresh failed: ${err.message}`, true);
  }
}

refreshBtn.addEventListener("click", async () => {
  await refreshAll();
  showToast("Dashboard refreshed");
});

genCharacterBtn.addEventListener("click", async () => {
  try {
    await requestJson("/api/character/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: isDryRun() }),
    });
    showToast(`Character generation started${isDryRun() ? " (dry simulation)" : " (live WaveSpeed)"}`);
    await Promise.all([refreshCharacter(), refreshSceneJobs()]);
  } catch (err) {
    showToast(`Character trigger failed: ${err.message}`, true);
  }
});

saveCharacterConfigBtn.addEventListener("click", async () => {
  try {
    const refs = characterRefsInput.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    const data = await requestJson("/api/character/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: characterNameInput.value,
        character_model_prompt: characterPromptInput.value,
        consistency_notes: characterNotesInput.value,
        style_description: styleDescriptionInput.value,
        use_style_reference_images: useStyleRefsCheckbox.checked,
        style_reference_images: refs,
      }),
    });
    const normalized = data.normalized_prompts || {};
    showToast(
      `Character settings saved. Prompt updates: ${normalized.payload_updates || 0}/${normalized.db_updates || 0}`
    );
    await Promise.all([refreshCharacterConfig(), refreshScenes()]);
  } catch (err) {
    showToast(`Character settings save failed: ${err.message}`, true);
  }
});

regenPromptsBtn.addEventListener("click", async () => {
  try {
    const refs = characterRefsInput.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    const data = await requestJson("/api/character/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: characterNameInput.value,
        style_description: styleDescriptionInput.value,
        use_style_reference_images: useStyleRefsCheckbox.checked,
        style_reference_images: refs,
      }),
    });
    const normalized = data.normalized_prompts || {};
    showToast(`Scene prompts normalized for ${characterNameInput.value || "character"} (${normalized.db_updates || 0})`);
    await Promise.all([refreshCharacterConfig(), refreshScenes()]);
  } catch (err) {
    showToast(`Normalize prompts failed: ${err.message}`, true);
  }
});

genAllImagesBtn.addEventListener("click", async () => {
  try {
    const data = await requestJson("/api/scenes/generate-images", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: isDryRun(), only_missing: true }),
    });
    const count = (data.launched || []).length;
    showToast(`Started ${count} image jobs${isDryRun() ? " (dry simulation)" : " (live WaveSpeed)"}`);
    await Promise.all([refreshSceneJobs(), refreshScenes(), refreshCharacter()]);
  } catch (err) {
    showToast(`Image batch failed: ${err.message}`, true);
  }
});

genAllVideosBtn.addEventListener("click", async () => {
  try {
    const data = await requestJson("/api/scenes/generate-videos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: isDryRun(), only_missing: true }),
    });
    const count = (data.launched || []).length;
    showToast(`Started ${count} video jobs${isDryRun() ? " (dry simulation)" : " (live WaveSpeed)"}`);
    await Promise.all([refreshSceneJobs(), refreshScenes()]);
  } catch (err) {
    showToast(`Video batch failed: ${err.message}`, true);
  }
});

runFullTriggerBtn.addEventListener("click", async () => {
  try {
    const data = await requestJson("/api/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dry_run: isDryRun(),
        provider: providerSelect.value,
      }),
    });
    selectedTriggerJobId = data.id;
    showToast(`Full trigger started: ${data.id}`);
    await refreshTriggerJobs();
    await loadTriggerLog(data.id);
  } catch (err) {
    showToast(`Full trigger failed: ${err.message}`, true);
  }
});

downloadLatestPayloadBtn.addEventListener("click", async () => {
  try {
    await downloadFromApi("/api/runs/latest/download", "latest_payload.json");
    showToast("Latest payload downloaded");
  } catch (err) {
    showToast(`Payload download failed: ${err.message}`, true);
  }
});

saveScriptBtn.addEventListener("click", async () => {
  try {
    await requestJson("/api/script", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script_text: scriptEditor.value }),
    });
    showToast("Script saved");
    await refreshOverview();
  } catch (err) {
    showToast(`Script save failed: ${err.message}`, true);
  }
});

refreshAll();
setInterval(async () => {
  if (document.hidden || pollInFlight) return;
  pollInFlight = true;
  const active = document.activeElement;
  const editing = active && active.tagName === "TEXTAREA" && active.classList.contains("cell-editor");
  try {
    if (!editing) {
      await refreshScenes();
    }
    await refreshSceneJobs();
    await refreshTriggerJobs();
    await refreshRuns();
    await refreshCharacter();
    await refreshCharacterConfig();
  } catch (err) {
    console.error(err);
  } finally {
    pollInFlight = false;
  }
}, 8000);
