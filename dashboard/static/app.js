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
const auditCharacterBtn = document.getElementById("auditCharacterBtn");
const autoBindCharacterBtn = document.getElementById("autoBindCharacterBtn");
const characterPromptPreview = document.getElementById("characterPromptPreview");
const characterAuditBox = document.getElementById("characterAuditBox");
const characterRegistryBox = document.getElementById("characterRegistryBox");
const triggerInfo = document.getElementById("triggerInfo");

const scriptPath = document.getElementById("scriptPath");
const scriptEditor = document.getElementById("scriptEditor");
const saveScriptBtn = document.getElementById("saveScriptBtn");

const triggerLog = document.getElementById("triggerLog");
const toast = document.getElementById("toast");
const mediaModal = document.getElementById("mediaModal");
const mediaModalClose = document.getElementById("mediaModalClose");
const mediaModalImage = document.getElementById("mediaModalImage");
const mediaModalVideo = document.getElementById("mediaModalVideo");
const mediaModalTitle = document.getElementById("mediaModalTitle");
const textModal = document.getElementById("textModal");
const textModalClose = document.getElementById("textModalClose");
const textModalApply = document.getElementById("textModalApply");
const textModalInput = document.getElementById("textModalInput");
const textModalTitle = document.getElementById("textModalTitle");

let selectedTriggerJobId = null;
let pollInFlight = false;
let warnedNoWaveSpeed = false;
let activeExpandedTextarea = null;

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
  const normalizedRaw = (status || "unknown").toLowerCase();
  const normalized =
    normalizedRaw === "verified" || normalizedRaw === "reused"
      ? "completed"
      : normalizedRaw === "needs_review"
        ? "pending"
        : normalizedRaw;
  const safe = ["completed", "success", "running", "failed", "pending"].includes(normalized)
    ? normalized
    : "unknown";
  return `<span class="status-chip status-${safe}">${esc(status || "unknown")}</span>`;
}

function autoResizeTextarea(node, minHeight = 88, maxHeight = 320) {
  if (!node) return;
  const min = Number(node.dataset.minHeight || minHeight);
  const max = Number(node.dataset.maxHeight || maxHeight);
  node.style.height = "auto";
  const next = Math.max(min, Math.min(node.scrollHeight, max));
  node.style.height = `${next}px`;
}

function bindAutoResizeTextarea(node, minHeight = 88, maxHeight = 320) {
  if (!node || node.dataset.autoResizeBound === "1") return;
  node.dataset.autoResizeBound = "1";
  node.dataset.minHeight = String(minHeight);
  node.dataset.maxHeight = String(maxHeight);
  const apply = () => autoResizeTextarea(node, minHeight, maxHeight);
  node.addEventListener("input", apply);
  node.addEventListener("focus", apply);
  requestAnimationFrame(apply);
}

function closeTextModal() {
  if (!textModal) return;
  textModal.classList.remove("open");
  textModal.setAttribute("aria-hidden", "true");
  activeExpandedTextarea = null;
  if (textModalInput) {
    textModalInput.value = "";
  }
}

function applyTextModalChanges() {
  if (!activeExpandedTextarea || !textModalInput) return;
  activeExpandedTextarea.value = textModalInput.value;
  activeExpandedTextarea.dispatchEvent(new Event("input", { bubbles: true }));
  closeTextModal();
}

function openTextModal(textarea, label) {
  if (!textModal || !textModalInput) return;
  activeExpandedTextarea = textarea;
  textModal.classList.add("open");
  textModal.setAttribute("aria-hidden", "false");
  textModalInput.value = textarea.value || "";
  if (textModalTitle) {
    textModalTitle.textContent = label || "Expanded Editor";
  }
  requestAnimationFrame(() => {
    textModalInput.focus();
    textModalInput.setSelectionRange(textModalInput.value.length, textModalInput.value.length);
  });
}

function bindExpandableTextarea(node, label) {
  if (!node || node.dataset.expandableBound === "1") return;
  node.dataset.expandableBound = "1";
  node.title = "Double-click to expand";
  node.addEventListener("dblclick", () => {
    openTextModal(node, label);
  });
  node.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      openTextModal(node, label);
    }
  });
}

function initializeLongTextEditors() {
  const staticTextareas = [
    [characterPromptInput, "Character Model Prompt", 88, 360],
    [characterNotesInput, "Character Consistency Notes", 88, 320],
    [characterRefsInput, "Style Reference URLs", 88, 320],
    [styleDescriptionInput, "Style Description", 88, 240],
    [scriptEditor, "Script Panel", 260, 740],
  ];
  staticTextareas.forEach(([node, label, minHeight, maxHeight]) => {
    if (!node) return;
    bindAutoResizeTextarea(node, minHeight, maxHeight);
    bindExpandableTextarea(node, label);
  });
}

function closeMediaModal() {
  if (!mediaModal) return;
  mediaModal.classList.remove("open");
  mediaModal.setAttribute("aria-hidden", "true");
  mediaModal.dataset.mediaType = "";

  if (mediaModalImage) {
    mediaModalImage.removeAttribute("src");
    mediaModalImage.style.display = "none";
  }
  if (mediaModalVideo) {
    mediaModalVideo.pause();
    mediaModalVideo.removeAttribute("src");
    mediaModalVideo.load();
    mediaModalVideo.style.display = "none";
  }
}

function openMediaModal(url, type, label) {
  if (!mediaModal || !url) return;
  const mediaType = type === "video" ? "video" : "image";
  mediaModal.classList.add("open");
  mediaModal.setAttribute("aria-hidden", "false");
  mediaModal.dataset.mediaType = mediaType;

  if (mediaModalTitle) {
    mediaModalTitle.textContent = label || (mediaType === "video" ? "Video Preview" : "Image Preview");
  }

  if (mediaType === "image") {
    if (mediaModalVideo) {
      mediaModalVideo.pause();
      mediaModalVideo.removeAttribute("src");
      mediaModalVideo.load();
      mediaModalVideo.style.display = "none";
    }
    if (mediaModalImage) {
      mediaModalImage.src = url;
      mediaModalImage.style.display = "block";
    }
    return;
  }

  if (mediaModalImage) {
    mediaModalImage.removeAttribute("src");
    mediaModalImage.style.display = "none";
  }
  if (mediaModalVideo) {
    mediaModalVideo.src = url;
    mediaModalVideo.style.display = "block";
    mediaModalVideo.play().catch(() => {});
  }
}

function attachMediaPopupTriggers(root) {
  if (!root) return;
  root.querySelectorAll(".media-pop-trigger").forEach((node) => {
    if (node.dataset.popupBound === "1") return;
    node.dataset.popupBound = "1";

    const open = () => {
      openMediaModal(node.dataset.mediaUrl || "", node.dataset.mediaType || "image", node.dataset.mediaLabel || "");
    };
    node.addEventListener("click", open);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
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
  const source = character?.source || "pending";
  const registryId = character?.registry_id || "";
  const audit = character?.audit || {};
  const auditStatus = audit?.status || "pending";
  const auditScore = Number(audit?.score);

  characterBox.innerHTML = `
    <div class="kv">
      <div class="k">Status</div>
      <div class="v">${statusChip(status)}</div>
    </div>
    <div class="kv">
      <div class="k">Task ID</div>
      <div class="v">${esc(taskId)}</div>
    </div>
    <div class="kv">
      <div class="k">Source</div>
      <div class="v">${esc(source)}${registryId ? ` (${esc(registryId)})` : ""}</div>
    </div>
    <div class="kv">
      <div class="k">Identity Audit</div>
      <div class="v">${statusChip(auditStatus)}${Number.isFinite(auditScore) ? ` <span class="audit-score-inline">${esc(auditScore.toFixed(2))}</span>` : ""}</div>
    </div>
    ${
      imageUrl
        ? simulated
          ? `<div class="preview-placeholder">Simulated character preview (dry run)</div>`
          : `
            <div class="media-pop-trigger character-media-trigger" role="button" tabindex="0" data-media-type="image" data-media-url="${esc(imageUrl)}" data-media-label="Character Model">
              <img class="character-preview" src="${esc(imageUrl)}" alt="Character model" />
              <span class="media-pop-hint">Open</span>
            </div>
          `
        : `<div class="preview-placeholder">No character model image yet</div>`
    }
    ${lastError ? `<div class="kv"><div class="k">Last Error</div><div class="v">${esc(lastError)}</div></div>` : ""}
  `;
  attachMediaFallbacks(characterBox);
  attachMediaPopupTriggers(characterBox);
}

function formatAuditScore(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : "-";
}

function prettyAutoBindReason(reason) {
  const map = {
    no_story_character_detected: "No character name detected in current story/script.",
    no_registry_match: "No saved model in registry for current character.",
    character_image_already_set: "Character image already loaded for this story.",
    character_generation_running: "Character generation is currently running.",
    auto_reuse_disabled: "Auto-reuse is disabled in character identity settings.",
  };
  return map[reason] || reason || "No saved character model was applied.";
}

function renderCharacterAudit(auditPayload, options = {}) {
  if (!characterAuditBox) return;
  const audit = auditPayload && typeof auditPayload === "object" ? auditPayload : {};
  const targetName = options.targetName || audit.target_name || "Not detected";
  const status = audit.status || "pending";
  const score = formatAuditScore(audit.score);
  const requestedAt = audit.requested_at || "";
  const sourceErrors = Array.isArray(audit.source_errors) ? audit.source_errors : [];
  const sourcesUsed = Array.isArray(audit.sources_used) ? audit.sources_used : [];
  const selected = Array.isArray(audit.selected_reference_images) ? audit.selected_reference_images : [];
  const review = Array.isArray(audit.review_reference_images) ? audit.review_reference_images : [];
  const chosenImage = selected[0] || review[0] || "";
  const candidates = Array.isArray(audit.candidates) ? audit.candidates.slice(0, 6) : [];

  characterAuditBox.innerHTML = `
    <div class="meta-item">
      <div class="meta-key">Character Audit</div>
      <div class="meta-value">
        <div>${statusChip(status)} <span class="audit-score-inline">Score ${esc(score)}</span></div>
        <div class="audit-subrow">Target: ${esc(targetName)}</div>
        ${requestedAt ? `<div class="audit-subrow">Updated: ${esc(requestedAt)}</div>` : ""}
        ${sourcesUsed.length ? `<div class="audit-subrow">Sources: ${esc(sourcesUsed.join(", "))}</div>` : ""}
      </div>
    </div>
    ${
      chosenImage
        ? `
          <div class="audit-preview">
            <div class="audit-preview-label">${selected.length ? "Verified Match" : "Review Candidate"}</div>
            <div class="media-pop-trigger" role="button" tabindex="0" data-media-type="image" data-media-url="${esc(chosenImage)}" data-media-label="Audit Candidate">
              <img class="preview" src="${esc(chosenImage)}" alt="Audit candidate image" loading="lazy" />
              <span class="media-pop-hint">Open</span>
            </div>
          </div>
        `
        : `<div class="preview-placeholder">No audited reference image selected yet</div>`
    }
    ${
      candidates.length
        ? `
          <div class="audit-candidates-list">
            ${candidates
              .map((candidate, idx) => {
                const imageUrl = candidate.image_url || "";
                return `
                  <div class="audit-candidate">
                    <div class="audit-candidate-head">
                      <span>#${idx + 1}</span>
                      <span>${esc(candidate.source || "source")}</span>
                      <span>${esc(formatAuditScore(candidate.score))}</span>
                    </div>
                    <div class="audit-candidate-title">${esc(candidate.title || "")}</div>
                    ${candidate.summary ? `<div class="audit-candidate-summary">${esc(candidate.summary)}</div>` : ""}
                    ${
                      imageUrl
                        ? `
                          <div class="media-pop-trigger audit-candidate-thumb" role="button" tabindex="0" data-media-type="image" data-media-url="${esc(imageUrl)}" data-media-label="Audit Candidate ${idx + 1}">
                            <img class="preview" src="${esc(imageUrl)}" alt="Audit candidate ${idx + 1}" loading="lazy" />
                            <span class="media-pop-hint">Open</span>
                          </div>
                        `
                        : ""
                    }
                    ${
                      candidate.source_url
                        ? `<a class="audit-link" href="${esc(candidate.source_url)}" target="_blank" rel="noreferrer noopener">Open source</a>`
                        : ""
                    }
                  </div>
                `;
              })
              .join("")}
          </div>
        `
        : ""
    }
    ${
      sourceErrors.length
        ? `
          <div class="audit-errors">
            ${sourceErrors.map((item) => `<div class="audit-error-line">${esc(item)}</div>`).join("")}
          </div>
        `
        : ""
    }
  `;
  attachMediaFallbacks(characterAuditBox);
  attachMediaPopupTriggers(characterAuditBox);
}

function renderCharacterRegistry(registryPayload) {
  if (!characterRegistryBox) return;
  const payload = registryPayload && typeof registryPayload === "object" ? registryPayload : {};
  const count = Number(payload.count) || 0;
  const items = Array.isArray(payload.items) ? payload.items.slice(0, 8) : [];

  characterRegistryBox.innerHTML = `
    <div class="meta-item">
      <div class="meta-key">Character Registry</div>
      <div class="meta-value">${esc(String(count))} saved model${count === 1 ? "" : "s"}</div>
    </div>
    ${
      items.length
        ? `
          <div class="registry-list">
            ${items
              .map((item) => {
                const imageUrl = item.image_url || "";
                const name = item.name || "Unnamed";
                return `
                  <div class="registry-item">
                    <div class="registry-title">${esc(name)}</div>
                    <div class="registry-sub">Score ${esc(formatAuditScore(item.audit_score))} • ${esc(item.audit_status || "unknown")}</div>
                    ${item.last_used_at ? `<div class="registry-sub">Used: ${esc(item.last_used_at)}</div>` : ""}
                    ${
                      imageUrl
                        ? `
                          <div class="media-pop-trigger registry-thumb" role="button" tabindex="0" data-media-type="image" data-media-url="${esc(imageUrl)}" data-media-label="${esc(name)}">
                            <img class="preview" src="${esc(imageUrl)}" alt="${esc(name)}" loading="lazy" />
                            <span class="media-pop-hint">Open</span>
                          </div>
                        `
                        : `<div class="preview-placeholder">No image</div>`
                    }
                    <button class="btn btn-sm btn-muted registry-use-btn" data-registry-name="${esc(name)}">Use This Model</button>
                  </div>
                `;
              })
              .join("")}
          </div>
        `
        : `<div class="preview-placeholder">No saved character models yet</div>`
    }
  `;

  characterRegistryBox.querySelectorAll(".registry-use-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const name = button.getAttribute("data-registry-name") || "";
      try {
        await requestJson("/api/character/config", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        const bound = await requestJson("/api/character/auto-bind", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ force: true }),
        });
        if (bound?.bound) {
          showToast(`Loaded saved model for ${name}`);
        } else {
          showToast(prettyAutoBindReason(bound?.reason), true);
        }
        await Promise.all([refreshCharacter(), refreshCharacterConfig(), refreshCharacterAudit(), refreshCharacterRegistry()]);
      } catch (err) {
        showToast(`Could not apply saved model: ${err.message}`, true);
      }
    });
  });

  attachMediaFallbacks(characterRegistryBox);
  attachMediaPopupTriggers(characterRegistryBox);
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
  const identity = config.character_identity || {};
  const sources = Array.isArray(identity.sources) ? identity.sources : [];
  const registryMatch = config.registry_match || null;
  characterPromptPreview.innerHTML = `
    <div class="meta-item">
      <div class="meta-key">Style Guardrail</div>
      <div class="meta-value">${esc(config.style_guardrail || "")}</div>
    </div>
    <div class="meta-item">
      <div class="meta-key">Effective Character Prompt</div>
      <div class="meta-value">${esc(config.effective_prompt || "")}</div>
    </div>
    <div class="meta-item">
      <div class="meta-key">Story Character Detection</div>
      <div class="meta-value">
        <div>Configured: ${esc(config.name || "None")}</div>
        <div>Inferred: ${esc(config.inferred_target_name || "None")}</div>
        <div>Audit: ${identity.audit_enabled ? "enabled" : "disabled"} • Min score ${esc(formatAuditScore(identity.min_confidence_score))}</div>
        <div>Sources: ${esc(sources.join(", ") || "none")}</div>
        <div>Auto-reuse saved model: ${identity.auto_reuse_saved_model ? "on" : "off"}</div>
        ${
          registryMatch
            ? `<div class="registry-match-line">Registry match available: ${esc(registryMatch.name || "")} (${esc(formatAuditScore(registryMatch.audit_score))})</div>`
            : ""
        }
      </div>
    </div>
  `;

  [characterPromptInput, characterNotesInput, characterRefsInput, styleDescriptionInput].forEach((node) =>
    autoResizeTextarea(node, 88, 360)
  );
}

function scenePreview(url, type) {
  if (!url) return `<div class="preview-placeholder">No ${type}</div>`;
  if (isSimulatedUrl(url)) return `<div class="preview-placeholder">Simulated ${type} (dry run)</div>`;
  if (type === "image") {
    return `
      <div class="media-pop-trigger" role="button" tabindex="0" data-media-type="image" data-media-url="${esc(url)}" data-media-label="Scene Image">
        <img class="preview" src="${esc(url)}" alt="Scene image" loading="lazy" />
        <span class="media-pop-hint">Open</span>
      </div>
    `;
  }
  return `
    <div class="media-pop-trigger" role="button" tabindex="0" data-media-type="video" data-media-url="${esc(url)}" data-media-label="Scene Video">
      <video class="preview" src="${esc(url)}" muted preload="metadata"></video>
      <span class="media-pop-hint">Play</span>
    </div>
  `;
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
    entry.querySelectorAll("textarea.cell-editor").forEach((textarea) => {
      const field = (textarea.getAttribute("data-field") || "prompt").replaceAll("_", " ");
      const label = `${sceneId || "Scene"} · ${field}`;
      bindAutoResizeTextarea(textarea, 92, 260);
      bindExpandableTextarea(textarea, label);
    });
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
  attachMediaPopupTriggers(scenesBody);
  if (scenesCards) {
    attachMediaFallbacks(scenesCards);
    attachMediaPopupTriggers(scenesCards);
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
  renderCharacterAudit(data.character_audit_state || data.character?.audit || {}, {
    targetName: data.character_audit_state?.target_name || data.character?.audit?.target_name || "",
  });
  renderCharacterRegistry(data.character_registry || {});
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
    autoResizeTextarea(scriptEditor, 260, 740);
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
  renderCharacterAudit(data.audit || {}, { targetName: data.audit?.target_name || "" });
}

async function refreshCharacterAudit() {
  const data = await requestJson("/api/character/audit");
  renderCharacterAudit(data.audit || {}, { targetName: data.target_name || data.audit?.target_name || "" });
}

async function refreshCharacterRegistry() {
  const data = await requestJson("/api/character/registry");
  renderCharacterRegistry(data);
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
      refreshCharacterAudit(),
      refreshCharacterRegistry(),
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
    const result = await requestJson("/api/character/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: isDryRun() }),
    });
    if (String(result?.task_id || "").startsWith("registry-")) {
      showToast("Loaded saved character model from registry");
    } else {
      showToast(`Character generation started${isDryRun() ? " (dry simulation)" : " (live WaveSpeed)"}`);
    }
    await Promise.all([refreshCharacter(), refreshSceneJobs(), refreshCharacterAudit(), refreshCharacterRegistry()]);
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
    await Promise.all([refreshCharacterConfig(), refreshScenes(), refreshCharacterAudit(), refreshCharacterRegistry()]);
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
    await Promise.all([refreshCharacterConfig(), refreshScenes(), refreshCharacterAudit(), refreshCharacterRegistry()]);
  } catch (err) {
    showToast(`Normalize prompts failed: ${err.message}`, true);
  }
});

auditCharacterBtn?.addEventListener("click", async () => {
  try {
    const cfg = await requestJson("/api/character/config");
    const identity = cfg.character_identity || {};
    const minScore = Number(identity.min_confidence_score);
    const sources = Array.isArray(identity.sources) ? identity.sources : undefined;
    const targetName = (characterNameInput.value || cfg.inferred_target_name || cfg.name || "").trim();
    const data = await requestJson("/api/character/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_name: targetName,
        min_confidence_score: Number.isFinite(minScore) ? minScore : 0.6,
        sources,
      }),
    });
    renderCharacterAudit(data || {}, { targetName: targetName || data.target_name || "" });
    const label = data.status === "verified" ? "verified" : data.status || "done";
    showToast(`Character audit ${label}. Score ${formatAuditScore(data.score)}`);
    await refreshCharacterRegistry();
  } catch (err) {
    showToast(`Character audit failed: ${err.message}`, true);
  }
});

autoBindCharacterBtn?.addEventListener("click", async () => {
  try {
    const result = await requestJson("/api/character/auto-bind", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: true }),
    });
    if (result?.bound) {
      const record = result.registry_record || {};
      showToast(`Loaded saved model: ${record.name || result.target_name || "character"}`);
    } else {
      showToast(prettyAutoBindReason(result?.reason), true);
    }
    await Promise.all([refreshCharacter(), refreshCharacterConfig(), refreshCharacterAudit(), refreshCharacterRegistry()]);
  } catch (err) {
    showToast(`Auto-load failed: ${err.message}`, true);
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

mediaModalClose?.addEventListener("click", closeMediaModal);
mediaModal?.querySelectorAll("[data-media-close='true']").forEach((node) => {
  node.addEventListener("click", closeMediaModal);
});
textModalClose?.addEventListener("click", closeTextModal);
textModalApply?.addEventListener("click", applyTextModalChanges);
textModal?.querySelectorAll("[data-text-close='true']").forEach((node) => {
  node.addEventListener("click", closeTextModal);
});
textModalInput?.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    applyTextModalChanges();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && mediaModal?.classList.contains("open")) {
    closeMediaModal();
    return;
  }
  if (event.key === "Escape" && textModal?.classList.contains("open")) {
    closeTextModal();
  }
});

initializeLongTextEditors();
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
    await refreshCharacterAudit();
    await refreshCharacterRegistry();
  } catch (err) {
    console.error(err);
  } finally {
    pollInFlight = false;
  }
}, 8000);
