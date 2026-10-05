const state = {
  config: {},
  models: [],
  guides: [],
  selectedGuides: new Set(),
  workflow: null,
  workflows: [],
  imageToolWorkflows: [],
  imageToolResults: [],
  recycleBin: null,
  references: [],
  selectedReferenceIds: new Set(),
  activeBatchId: null,
  liveBatchId: null,
  selectedBatchId: null,
  selectedGalleryBatchId: null,
  selectedWallBatchId: null,
  selectedPostprocessBatchId: null,
  askPromptId: null,
  currentBatch: null,
  galleryItems: [],
  selectedGalleryIndex: null,
  wallItems: [],
  selectedWallIndex: null,
  postprocessImages: [],
  selectedMetadataAssetId: null,
  leaderboard: { guides: [], summary: {} },
  batches: [],
  activeTimer: null,
  statusTimer: null,
};

document.documentElement.dataset.appBuild = "20260817-leaderboardsort1";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function savedReferenceSelection() {
  try {
    return new Set(JSON.parse(localStorage.getItem("wildcatSelectedReferences") || "[]"));
  } catch {
    return new Set();
  }
}

function persistReferenceSelection() {
  localStorage.setItem("wildcatSelectedReferences", JSON.stringify([...state.selectedReferenceIds]));
}

function referenceMediaUrl(reference) {
  return `/reference-media/${encodeURIComponent(reference.id)}?v=${encodeURIComponent(reference.created_at || "")}`;
}

function renderPromptEstimate() {
  const field = $("#promptsPerGuide");
  const output = $("#effectivePromptCount");
  if (!field || !output) return;
  const base = Math.max(1, Number(field.value) || 1);
  const references = state.selectedReferenceIds.size;
  const total = base + references;
  output.textContent = references
    ? `${base} base + ${references} reference = ${total} total per guide`
    : `${base} total per guide`;
}

function renderReferenceTray() {
  const tray = $("#referenceTray");
  if (!tray) return;
  const ready = state.references.filter((item) => item.processed_path);
  const validIds = new Set(ready.map((item) => item.id));
  state.selectedReferenceIds = new Set([...state.selectedReferenceIds].filter((id) => validIds.has(id)));
  persistReferenceSelection();
  tray.innerHTML = ready.length
    ? ready.map((item) => {
      const selected = state.selectedReferenceIds.has(item.id);
      return `<article class="reference-card ${selected ? "is-selected" : ""}">
        <label class="reference-preview">
          <input type="checkbox" data-reference-select="${escapeHtml(item.id)}" ${selected ? "checked" : ""}>
          <img src="${referenceMediaUrl(item)}" loading="lazy" width="${item.width || 1}" height="${item.height || 1}" alt="Cleaned reference ${escapeHtml(item.name)}">
          <span>${item.media_type === "video" ? "VIDEO STILL" : "PHOTO"}</span>
        </label>
        <div><strong title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong><small>${item.width} × ${item.height}${item.remove_ui ? " · UI ignored in prompt" : " · original framing"}</small></div>
        <button type="button" data-delete-reference="${escapeHtml(item.id)}" aria-label="Remove ${escapeHtml(item.name)}">×</button>
      </article>`;
    }).join("")
    : '<p class="empty-note">No reference media yet. Your text brief can still run by itself.</p>';
  $$('[data-reference-select]', tray).forEach((input) => input.addEventListener("change", () => {
    input.checked ? state.selectedReferenceIds.add(input.dataset.referenceSelect) : state.selectedReferenceIds.delete(input.dataset.referenceSelect);
    persistReferenceSelection();
    renderReferenceTray();
  }));
  $$('[data-delete-reference]', tray).forEach((button) => button.addEventListener("click", () => deleteReference(button.dataset.deleteReference)));
  const selectedCount = state.selectedReferenceIds.size;
  const summaryCount = $("#referenceSummaryCount");
  if (summaryCount) summaryCount.textContent = ready.length
    ? `${selectedCount} selected · adds ${selectedCount} prompt${selectedCount === 1 ? "" : "s"} per guide`
    : "No references";
  setMessage(
    "#referenceMessage",
    ready.length
      ? `${selectedCount} of ${ready.length} selected. Every selected image or still is sent separately, so all of them are used.`
      : "",
  );
  renderPromptEstimate();
}

function canvasForSource(source, width, height) {
  const scale = Math.min(1, 1800 / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.fillStyle = "#000";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas;
}

function cleanedCanvas(sourceCanvas) {
  const scale = Math.min(1, 1400 / Math.max(sourceCanvas.width, sourceCanvas.height));
  const output = document.createElement("canvas");
  output.width = Math.max(1, Math.round(sourceCanvas.width * scale));
  output.height = Math.max(1, Math.round(sourceCanvas.height * scale));
  const context = output.getContext("2d");
  context.fillStyle = "#000";
  context.fillRect(0, 0, output.width, output.height);
  context.drawImage(sourceCanvas, 0, 0, output.width, output.height);
  return output;
}

async function imageCanvas(file) {
  const bitmap = await createImageBitmap(file);
  try { return canvasForSource(bitmap, bitmap.width, bitmap.height); } finally { bitmap.close(); }
}

async function videoStillCanvas(file) {
  const url = URL.createObjectURL(file);
  const video = document.createElement("video");
  video.preload = "metadata";
  video.muted = true;
  video.playsInline = true;
  try {
    video.src = url;
    await new Promise((resolve, reject) => {
      video.addEventListener("loadedmetadata", resolve, { once: true });
      video.addEventListener("error", () => reject(new Error(`This browser cannot decode ${file.name}.`)), { once: true });
    });
    const target = Number.isFinite(video.duration) && video.duration > .2 ? Math.min(video.duration - .05, video.duration * .5) : 0;
    if (target > 0) {
      video.currentTime = target;
      await new Promise((resolve, reject) => {
        video.addEventListener("seeked", resolve, { once: true });
        video.addEventListener("error", () => reject(new Error(`Could not extract a still from ${file.name}.`)), { once: true });
      });
    }
    return canvasForSource(video, video.videoWidth, video.videoHeight);
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function uploadReferenceFile(file) {
  const mediaType = file.type.startsWith("video/") ? "video" : "image";
  const sourceResponse = await fetch(`/api/references/source?name=${encodeURIComponent(file.name)}&media_type=${mediaType}`, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  const sourcePayload = await sourceResponse.json().catch(() => ({}));
  if (!sourceResponse.ok) throw new Error(sourcePayload.error || `Could not archive ${file.name}.`);
  const referenceId = sourcePayload.reference.id;
  try {
    const sourceCanvas = mediaType === "video" ? await videoStillCanvas(file) : await imageCanvas(file);
    const removeUi = $("#removeReferenceUi").checked;
    const processed = cleanedCanvas(sourceCanvas);
    const payload = await api("/api/references/processed", {
      method: "POST",
      body: JSON.stringify({
        reference_id: referenceId,
        data_url: processed.toDataURL("image/jpeg", .86),
        width: processed.width,
        height: processed.height,
        remove_ui: removeUi,
      }),
    });
    state.references = payload.references || state.references;
    state.selectedReferenceIds.add(referenceId);
    persistReferenceSelection();
  } catch (error) {
    await api(`/api/references/${referenceId}`, { method: "DELETE" }).catch(() => {});
    throw error;
  }
}

async function addReferenceFiles(files) {
  const accepted = [...files].filter((file) => file.type.startsWith("image/") || file.type.startsWith("video/"));
  if (!accepted.length) {
    setMessage("#referenceMessage", "Choose photo or video files.", "error");
    return;
  }
  const dropZone = $("#referenceDropZone");
  dropZone.classList.add("is-processing");
  let completed = 0;
  try {
    for (const file of accepted) {
      setMessage("#referenceMessage", `Processing ${completed + 1} of ${accepted.length}: ${file.name}…`);
      await uploadReferenceFile(file);
      completed += 1;
      renderReferenceTray();
    }
    setMessage(
      "#referenceMessage",
      `${completed} reference${completed === 1 ? "" : "s"} ready and selected. Each adds one prompt per guide.`,
      "success",
    );
  } catch (error) {
    setMessage("#referenceMessage", `${completed} completed. ${error.message}`, "error");
  } finally {
    dropZone.classList.remove("is-processing");
    $("#referenceFiles").value = "";
  }
}

async function deleteReference(referenceId) {
  const reference = state.references.find((item) => item.id === referenceId);
  if (!reference || !confirm(`Remove temporary reference "${reference.name}" from WildCat Export Edition?`)) return;
  try {
    const payload = await api(`/api/references/${referenceId}`, { method: "DELETE" });
    state.references = payload.references || [];
    state.selectedReferenceIds.delete(referenceId);
    renderReferenceTray();
  } catch (error) { toast(error.message, true); }
}

async function clearReferences() {
  if (!state.references.length || !confirm(`Remove all ${state.references.length} temporary references?`)) return;
  for (const reference of [...state.references]) {
    try { await api(`/api/references/${reference.id}`, { method: "DELETE" }); } catch (error) { toast(error.message, true); }
  }
  state.references = [];
  state.selectedReferenceIds.clear();
  renderReferenceTray();
}

let toastTimer;
function toast(message, isError = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("is-error", isError);
  element.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("is-visible"), 4200);
}

function setMessage(selector, message, type = "") {
  const element = $(selector);
  element.textContent = message;
  element.classList.toggle("is-error", type === "error");
  element.classList.toggle("is-success", type === "success");
}

function switchTab(name) {
  $$(".nav-tab").forEach((button) => button.classList.toggle("is-active", button.dataset.tab === name));
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("is-active", panel.dataset.panel === name));
  if (name === "library") {
    refreshBatches();
    if (state.selectedBatchId) openBatch(state.selectedBatchId);
  }
  if (name === "leaderboard") loadLeaderboard();
  if (name === "results") loadImageToolResults();
  if (name === "recycle") loadRecycleBin();
  if (name === "civitai") renderCivitaiQueue();
  if (name === "gallery") {
    refreshBatches("").then(() => {
      const batchId = state.selectedGalleryBatchId
        || state.selectedBatchId
        || state.batches.find((batch) => batch.asset_count > 0)?.id;
      if (batchId) openGalleryBatch(batchId);
    });
  }
  if (name === "wall") {
    refreshBatches("").then(() => {
      const batchId = state.selectedWallBatchId
        || state.selectedGalleryBatchId
        || state.selectedBatchId
        || state.batches.find((batch) => batch.asset_count > 0)?.id;
      if (batchId) openWallBatch(batchId);
    });
  }
  if (name === "postprocess") {
    refreshBatches("").then(() => {
      const batchId = state.selectedPostprocessBatchId
        || state.selectedGalleryBatchId
        || state.selectedBatchId
        || state.batches.find((batch) => batch.asset_count > 0)?.id;
      if (batchId) openPostprocessBatch(batchId);
    });
  }
}

function leaderboardNumber(value) {
  return new Intl.NumberFormat().format(Number(value || 0));
}

function leaderboardSummaryMarkup(summary) {
  const average = summary.average_rating == null ? "—" : Number(summary.average_rating).toFixed(2);
  return `<div class="leaderboard-stat-grid">
    <article><span>Ranked guides</span><strong>${leaderboardNumber(summary.ranked_guides)}</strong><small>of ${leaderboardNumber(summary.total_guides)} markdown files</small></article>
    <article><span>Your overall average</span><strong class="is-gold">${average}${summary.average_rating == null ? "" : " ★"}</strong><small>across all rated images</small></article>
    <article><span>Ratings recorded</span><strong>${leaderboardNumber(summary.rated_images)}</strong><small>of ${leaderboardNumber(summary.total_images)} guide outputs</small></article>
    <article><span>Batches included</span><strong>${leaderboardNumber(summary.batch_count)}</strong><small>all saved batches with guides</small></article>
  </div>`;
}

function orderedLeaderboardGuides() {
  const query = ($("#leaderboardSearch")?.value || "").trim().toLocaleLowerCase();
  const sort = $("#leaderboardSort")?.value || "score";
  const guides = (state.leaderboard.guides || []).filter((item) => item.guide.toLocaleLowerCase().includes(query));
  return [...guides].sort((left, right) => {
    if (sort === "coverage") return Number(right.rating_coverage || 0) - Number(left.rating_coverage || 0) || right.rated_images - left.rated_images || (right.average_rating || 0) - (left.average_rating || 0) || left.guide.localeCompare(right.guide);
    if (sort === "five-stars") return Number(right.ratings?.["5"] || 0) - Number(left.ratings?.["5"] || 0) || Number(right.rating_coverage || 0) - Number(left.rating_coverage || 0) || right.rated_images - left.rated_images || left.guide.localeCompare(right.guide);
    if (sort === "rated") return right.rated_images - left.rated_images || (right.average_rating || 0) - (left.average_rating || 0) || left.guide.localeCompare(right.guide);
    if (sort === "images") return right.total_images - left.total_images || right.rated_images - left.rated_images || left.guide.localeCompare(right.guide);
    if (sort === "posted") return (Number(right.posted_images || 0) - Number(left.posted_images || 0)) || right.total_images - left.total_images || right.rated_images - left.rated_images || left.guide.localeCompare(right.guide);
    if (sort === "batches") return right.batch_count - left.batch_count || right.total_images - left.total_images || left.guide.localeCompare(right.guide);
    if (sort === "name") return left.guide.localeCompare(right.guide);
    return (left.rank == null) - (right.rank == null) || (left.rank || 0) - (right.rank || 0) || left.guide.localeCompare(right.guide);
  });
}

function leaderboardRowMarkup(item, orderedIndex = 0) {
  const rated = item.average_rating != null;
  const score = rated ? Number(item.average_rating).toFixed(2) : "Unrated";
  const coverage = Number(item.rating_coverage || 0);
  const fiveStars = Number(item.ratings?.["5"] || 0);
  const displayRank = ($("#leaderboardSort")?.value || "score") === "score" ? item.rank : orderedIndex + 1;
  return `<article class="leaderboard-row ${rated ? "is-ranked" : "is-unrated"}">
    <div class="leaderboard-rank">${displayRank == null ? "—" : `<span>#</span>${displayRank}`}</div>
    <div class="leaderboard-guide">
      <strong title="${escapeHtml(item.guide)}">${escapeHtml(item.guide)}</strong>
      <div class="leaderboard-coverage"><i style="width:${Math.min(100, coverage)}%"></i></div>
      <small>${coverage}% of outputs rated</small>
    </div>
    <div class="leaderboard-score"><strong>${score}${rated ? " ★" : ""}</strong><small>${fiveStars} five-star</small></div>
    <div class="leaderboard-metric"><strong>${leaderboardNumber(item.rated_images)} / ${leaderboardNumber(item.total_images)}</strong><small>rated images</small></div>
    <div class="leaderboard-metric"><strong>${leaderboardNumber(item.posted_images || 0)}</strong><small>posted to Civitai</small></div>
    <div class="leaderboard-metric"><strong>${leaderboardNumber(item.batch_count)}</strong><small>batches</small></div>
  </article>`;
}

function renderLeaderboard() {
  $("#leaderboardSummary").innerHTML = leaderboardSummaryMarkup(state.leaderboard.summary || {});
  const guides = orderedLeaderboardGuides();
  $("#leaderboardTable").innerHTML = guides.length
    ? guides.map((item, index) => leaderboardRowMarkup(item, index)).join("")
    : '<div class="gallery-empty"><strong>No matching markdown guides.</strong><p>Try a different search.</p></div>';
}

function leaderboardFromBatchPayloads(batches) {
  const grouped = new Map();
  const allBatches = new Set();
  batches.forEach((batch) => {
    (batch.prompts || []).forEach((prompt) => {
      const guide = String(prompt.guide || "").trim().replace(/\s+/g, " ");
      if (!guide) return;
      const imageAssets = (prompt.assets || []).filter((asset) => asset.kind === "images");
      if (!imageAssets.length) return;
      const key = guide.toLocaleLowerCase();
      const record = grouped.get(key) || {
        guide,
        total_images: 0,
        rated_images: 0,
        points: 0,
        batches: new Set(),
        ratings: { "1": 0, "2": 0, "3": 0, "4": 0, "5": 0 },
        last_generated: "",
      };
      imageAssets.forEach((asset) => {
        record.total_images += 1;
        record.batches.add(batch.id);
        allBatches.add(batch.id);
        record.last_generated = record.last_generated > asset.created_at ? record.last_generated : asset.created_at;
        const rating = Number(asset.rating || 0);
        if (rating >= 1 && rating <= 5) {
          record.rated_images += 1;
          record.points += rating;
          record.ratings[String(rating)] += 1;
        }
      });
      grouped.set(key, record);
    });
  });
  const guides = [...grouped.values()].map((record) => ({
    guide: record.guide,
    average_rating: record.rated_images ? Math.round((record.points / record.rated_images) * 1000) / 1000 : null,
    rated_images: record.rated_images,
    total_images: record.total_images,
    rating_coverage: record.total_images ? Math.round((record.rated_images / record.total_images) * 1000) / 10 : 0,
    batch_count: record.batches.size,
    ratings: record.ratings,
    last_generated: record.last_generated,
  })).sort((left, right) => (
    (left.average_rating == null) - (right.average_rating == null)
    || (right.average_rating || 0) - (left.average_rating || 0)
    || right.rated_images - left.rated_images
    || left.guide.localeCompare(right.guide)
  ));
  let rank = 0;
  guides.forEach((item) => {
    item.rank = item.average_rating == null ? null : ++rank;
  });
  const ratedImages = guides.reduce((total, item) => total + item.rated_images, 0);
  const points = guides.reduce((total, item) => total + Object.entries(item.ratings).reduce((sum, [stars, count]) => sum + Number(stars) * count, 0), 0);
  return {
    guides,
    summary: {
      total_guides: guides.length,
      ranked_guides: rank,
      batch_count: allBatches.size,
      total_images: guides.reduce((total, item) => total + item.total_images, 0),
      rated_images: ratedImages,
      average_rating: ratedImages ? Math.round((points / ratedImages) * 1000) / 1000 : null,
    },
  };
}

async function loadLeaderboardFromExistingBatchApi() {
  if (!state.batches.length) {
    const payload = await api("/api/batches");
    state.batches = payload.batches || [];
  }
  const summaries = state.batches.filter((batch) => batch.asset_count > 0);
  const batches = [];
  for (let index = 0; index < summaries.length; index += 6) {
    const group = await Promise.all(
      summaries.slice(index, index + 6).map((batch) => api(`/api/batches/${batch.id}`))
    );
    batches.push(...group.map((payload) => payload.batch));
  }
  return leaderboardFromBatchPayloads(batches);
}

async function loadLeaderboard() {
  $("#leaderboardTable").innerHTML = '<div class="gallery-empty"><strong>Combining ratings from every batch…</strong></div>';
  try {
    try {
      state.leaderboard = await api("/api/leaderboard");
    } catch {
      state.leaderboard = await loadLeaderboardFromExistingBatchApi();
    }
    renderLeaderboard();
  } catch (error) {
    $("#leaderboardTable").innerHTML = `<div class="gallery-empty"><strong>Could not load the leaderboard</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function modelOptions(selected = "") {
  const options = ['<option value="">Choose a model…</option>'];
  for (const model of state.models) {
    const size = model.size_bytes ? ` · ${(model.size_bytes / 1e9).toFixed(1)} GB` : "";
    const vision = model.vision ? " · vision" : "";
    const quant = model.quantization ? ` · ${model.quantization}` : "";
    options.push(`<option value="${escapeHtml(model.id)}" ${model.id === selected ? "selected" : ""}>${escapeHtml(model.name)}${escapeHtml(quant + size + vision)}</option>`);
  }
  return options.join("");
}

function visionModelOptions(selected = "") {
  const visionModels = state.models.filter((model) => model.vision);
  const options = ['<option value="">Choose a vision model…</option>'];
  for (const model of visionModels) {
    const size = model.size_bytes ? ` · ${(model.size_bytes / 1e9).toFixed(1)} GB` : "";
    const quant = model.quantization ? ` · ${model.quantization}` : "";
    options.push(`<option value="${escapeHtml(model.id)}" ${model.id === selected ? "selected" : ""}>${escapeHtml(model.name)}${escapeHtml(quant + size)}</option>`);
  }
  return options.join("");
}

async function loadModels() {
  const select = $("#promptModel");
  select.innerHTML = '<option value="">Loading models…</option>';
  try {
    const payload = await api("/api/models");
    state.models = payload.models || [];
    select.innerHTML = modelOptions(state.config.prompt_model || "");
  } catch (error) {
    select.innerHTML = state.models.length
      ? modelOptions(state.config.prompt_model || "")
      : '<option value="">Model list deferred while another job owns the GPU lane</option>';
    console.warn(error);
  }
}

function renderGuides() {
  const grid = $("#guideGrid");
  if (!state.guides.length) {
    grid.innerHTML = '<p class="empty-note">No WildCat Markdown guides yet. Add one or more MD files.</p>';
    $("#guideCount").textContent = "0 selected";
    return;
  }
  grid.innerHTML = state.guides.map((guide) => `
    <article class="guide-card" title="${escapeHtml(guide.name)}">
      <label class="guide-select">
        <input type="checkbox" data-guide-id="${escapeHtml(guide.id)}" ${state.selectedGuides.has(guide.id) ? "checked" : ""}>
        <span><strong>${escapeHtml(guide.name)}</strong><small>${Math.max(1, Math.round(Number(guide.size || String(guide.content || "").length) / 1000))}k characters</small></span>
      </label>
      <button class="guide-delete" type="button" data-delete-guide="${escapeHtml(guide.id)}">Delete</button>
    </article>`).join("");
  $$('[data-guide-id]', grid).forEach((input) => input.addEventListener("change", () => {
    const guideId = input.dataset.guideId;
    input.checked ? state.selectedGuides.add(guideId) : state.selectedGuides.delete(guideId);
    $("#guideCount").textContent = `${state.selectedGuides.size} selected`;
  }));
  $$('[data-delete-guide]', grid).forEach((button) => button.addEventListener("click", () => deleteGuide(button.dataset.deleteGuide)));
  $("#guideCount").textContent = `${state.selectedGuides.size} selected`;
}

async function loadGuides() {
  $("#guideGrid").innerHTML = '<p class="empty-note">Loading your guide library…</p>';
  try {
    const payload = await api("/api/guides");
    state.guides = payload.guides || [];
    const available = new Set(state.guides.map((guide) => guide.id));
    state.selectedGuides = new Set([...state.selectedGuides].filter((guideId) => available.has(guideId)));
    renderGuides();
  } catch (error) {
    $("#guideGrid").innerHTML = `<p class="empty-note">${escapeHtml(error.message)}</p>`;
  }
}

async function uploadGuides(event) {
  const files = [...(event.target.files || [])].filter((file) => file.name.toLocaleLowerCase().endsWith(".md"));
  if (!files.length) {
    setMessage("#guideMessage", "Choose one or more .md files.", "error");
    return;
  }
  let saved = 0;
  try {
    for (const file of files) {
      setMessage("#guideMessage", `Adding ${saved + 1} of ${files.length}: ${file.name}…`);
      const payload = await api("/api/guides", {
        method: "POST",
        body: JSON.stringify({ name: file.name, content: await file.text() }),
      });
      state.guides = payload.guides || state.guides;
      if (payload.guide?.id) state.selectedGuides.add(payload.guide.id);
      saved += 1;
    }
    renderGuides();
    setMessage("#guideMessage", `${saved} Markdown guide${saved === 1 ? "" : "s"} saved in WildCat and selected.`, "success");
  } catch (error) {
    setMessage("#guideMessage", `${saved} saved. ${error.message}`, "error");
  } finally {
    event.target.value = "";
  }
}

async function deleteGuide(guideId) {
  const guide = state.guides.find((item) => item.id === guideId);
  if (!guide || !confirm(`Delete WildCat guide "${guide.name}"? Queued jobs already contain their own copy.`)) return;
  try {
    const payload = await api(`/api/guides/${guideId}`, { method: "DELETE" });
    state.guides = payload.guides || [];
    state.selectedGuides.delete(guideId);
    renderGuides();
    setMessage("#guideMessage", `${guide.name} deleted.`, "success");
  } catch (error) {
    setMessage("#guideMessage", error.message, "error");
  }
}

function activeWorkflowEntry() {
  const selectedId = $("#workflowLibrary")?.value || state.config.active_workflow_id || "";
  return state.workflows.find((item) => item.id === selectedId) || state.workflows[0] || null;
}

function renderWorkflowChoices(selectedId = state.config.active_workflow_id || "") {
  const options = state.workflows.length
    ? state.workflows.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")
    : '<option value="">No saved workflows</option>';
  ["#batchWorkflow", "#workflowLibrary"].forEach((selector) => {
    const select = $(selector);
    select.innerHTML = options;
    if (state.workflows.some((item) => item.id === selectedId)) select.value = selectedId;
  });
  $("#deleteWorkflow").disabled = !state.workflows.length;
}

function renderWorkflow(analysis = state.workflow) {
  state.workflow = analysis;
  const badge = $("#workflowBadge");
  const summary = $("#workflowSummary");
  const form = $("#mappingForm");
  if (!analysis) {
    badge.textContent = "No workflow";
    badge.classList.remove("is-ready");
    summary.innerHTML = "<p>No API workflow uploaded yet.</p>";
    form.classList.add("is-hidden");
    return;
  }
  badge.textContent = state.config.workflow_name || "Workflow ready";
  badge.classList.add("is-ready");
  summary.innerHTML = `<p><strong>${escapeHtml(state.config.workflow_name || "API workflow")}</strong><br>${analysis.node_count} nodes · ${analysis.fields.length} editable prompt fields · ${analysis.seed_fields.length} seed fields</p>`;
  form.classList.remove("is-hidden");
  const positiveSelected = new Set(state.config.workflow_positive_fields || analysis.recommended_positive || []);
  const negativeSelected = new Set(state.config.workflow_negative_fields || analysis.recommended_negative || []);
  const fieldMarkup = (field, role, checked) => `
    <label class="mapping-item">
      <input type="checkbox" name="${role}" value="${escapeHtml(field.field_id)}" ${checked ? "checked" : ""}>
      <span><strong>${escapeHtml(field.title)} · ${escapeHtml(field.input)}</strong><small>${escapeHtml(field.sample || "empty text field")}</small></span>
    </label>`;
  $("#positiveFields").innerHTML = analysis.fields.map((field) => fieldMarkup(field, "positive", positiveSelected.has(field.field_id))).join("") || '<p class="empty-note">No text prompt fields detected.</p>';
  $("#negativeFields").innerHTML = analysis.fields.map((field) => fieldMarkup(field, "negative", negativeSelected.has(field.field_id))).join("") || '<p class="empty-note">No text prompt fields detected.</p>';
}

function imageToolLabel(kind) {
  return kind === "upscale" ? "Upscaling" : "Editing";
}

function renderImageToolWorkflows() {
  const list = $("#imageToolWorkflowList");
  if (!list) return;
  list.innerHTML = state.imageToolWorkflows.length
    ? state.imageToolWorkflows.map((entry) => `
      <article class="image-tool-workflow-row">
        <div><span>${imageToolLabel(entry.kind)}</span><strong>${escapeHtml(entry.name)}</strong><small>${entry.node_count || "?"} nodes · ${(entry.image_inputs || []).length || entry.image_fields?.length || 0} image input(s)${entry.kind === "edit" ? ` · ${entry.positive_fields?.length || 0} instruction field(s)` : ""}</small></div>
        <button class="danger-button" type="button" data-delete-image-tool="${escapeHtml(entry.id)}">Delete API</button>
      </article>`).join("")
    : '<p class="empty-note">No editing or upscaling APIs uploaded yet.</p>';
  $$("[data-delete-image-tool]", list).forEach((button) => button.addEventListener("click", () => {
    deleteImageToolWorkflow(button.dataset.deleteImageTool);
  }));
}

function imageToolSelectMarkup(kind, id) {
  const entries = state.imageToolWorkflows.filter((entry) => entry.kind === kind);
  return `<label><span>${imageToolLabel(kind)} API</span><select id="${id}" ${entries.length ? "" : "disabled"}>
    ${entries.length
      ? entries.map((entry) => `<option value="${escapeHtml(entry.id)}">${escapeHtml(entry.name)}</option>`).join("")
      : `<option value="">Upload an ${kind === "edit" ? "editing" : "upscaling"} API in Settings</option>`}
  </select></label>`;
}

function populateSettings() {
  const config = state.config;
  $("#lmUrl").value = config.lm_url || "";
  $("#lmStudioExe").value = config.lm_studio_exe || "";
  $("#lmToken").value = "";
  $("#lmToken").placeholder = config.lm_has_api_token ? "Saved token (unchanged)" : "No token required by default";
  $("#comfyUrl").value = config.comfy_url || "";
  $("#comfyPath").value = config.comfy_path || "";
  $("#contextLength").value = config.lm_context_length || 16384;
  $("#comfyTimeout").value = config.comfy_job_timeout_minutes || 180;
  $("#autoStartApps").checked = Boolean(config.auto_start_apps);
  const civToken = $("#civitaiToken");
  if (civToken) {
    civToken.value = "";
    civToken.placeholder = config.civitai_has_api_token ? "Saved key (unchanged)" : "Paste your Civitai API key";
  }
  const civUser = $("#civitaiUsername");
  if (civUser) civUser.value = config.civitai_username || "";
  updateCivitaiStatus();
}

function updateCivitaiStatus(counts) {
  const config = state.config || {};
  const parts = [];
  parts.push(config.civitai_has_api_token ? "key saved" : "no key");
  if (config.civitai_username) parts.push(`@${config.civitai_username}`);
  if (config.civitai_last_sync) parts.push(`last sync ${String(config.civitai_last_sync).replace("T", " ").slice(0, 16)}`);
  const text = counts
    ? `${counts.posted_images ?? 0} / ${counts.total_images ?? 0} images marked posted`
    : parts.join(" · ");
  ["#civitaiStatus", "#civitaiStatusQueue"].forEach((selector) => {
    const el = $(selector);
    if (el) el.textContent = text;
  });
}

async function saveCivitaiSettings(event) {
  event.preventDefault();
  try {
    const payload = await api("/api/civitai/settings", {
      method: "POST",
      body: JSON.stringify({
        api_token: $("#civitaiToken").value.trim() || "__KEEP__",
        username: $("#civitaiUsername").value.trim(),
      }),
    });
    state.config.civitai_has_api_token = true;
    state.config.civitai_username = payload.username || state.config.civitai_username;
    $("#civitaiToken").value = "";
    $("#civitaiToken").placeholder = "Saved key (unchanged)";
    updateCivitaiStatus();
    setMessage("#civitaiMessage", `Key verified for @${payload.username}.`, "success");
    toast("Civitai key saved.");
  } catch (error) { setMessage("#civitaiMessage", error.message, "error"); }
}

async function syncCivitai() {
  const button = $("#civitaiSync");
  const original = button?.textContent || "Sync";
  if (button) { button.disabled = true; button.textContent = "Syncing…"; }
  setMessage("#civitaiMessage", "Walking your Civitai posts and matching against the archive…");
  setMessage("#civitaiQueueMessage", "Walking your Civitai posts and matching against the archive…");
  try {
    const payload = await api("/api/civitai/sync", { method: "POST", body: "{}" });
    state.config.civitai_last_sync = new Date().toISOString();
    updateCivitaiStatus({ posted_images: payload.posted_images, total_images: payload.total_images });
    setMessage("#civitaiMessage", `Scanned ${payload.civitai_images_seen} Civitai images across ${payload.pages} page${payload.pages === 1 ? "" : "s"} — matched ${payload.newly_matched} archive image${payload.newly_matched === 1 ? "" : "s"} (${payload.posted_images}/${payload.total_images} now marked posted). Fresh uploads can take a few minutes to appear in Civitai's search — re-run sync if a new post isn't marked yet.`, "success");
    setMessage("#civitaiQueueMessage", `Synced: ${payload.posted_images}/${payload.total_images} posted (${payload.newly_matched} newly matched).`, "success");
    toast(`Civitai sync: ${payload.posted_images}/${payload.total_images} posted.`);
    await refreshBatches();
  } catch (error) {
    setMessage("#civitaiMessage", error.message, "error");
    setMessage("#civitaiQueueMessage", error.message, "error");
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

async function postAssetToCivitai(index) {
  const item = state.galleryItems[index];
  if (!item) return;
  civitaiQueueAdd(item);
}

async function postBatchToCivitai(batchId) {
  const batch = state.currentBatch;
  if (!batch || batch.id !== batchId) return;
  const unposted = (batch.prompts || []).flatMap((p) => (p.assets || []).filter((a) => !a.civitai_posted).map((a) => a.id));
  if (!unposted.length) { toast("Every image in this batch is already marked as posted."); return; }
  if (!confirm(`Add ${unposted.length} unposted image${unposted.length === 1 ? "" : "s"} from this batch to the Civitai post queue?`)) return;
  loadCivitaiQueue();
  let added = 0;
  for (const assetId of unposted) {
    if (state.civitaiQueue.some((entry) => entry.asset_id === assetId)) continue;
    const asset = (batch.prompts || []).flatMap((p) => p.assets || []).find((a) => a.id === assetId);
    if (!asset) continue;
    state.civitaiQueue.push({ asset_id: asset.id, batch_id: asset.batch_id, filename: asset.filename, local_path: asset.local_path, width: asset.width, height: asset.height });
    added++;
  }
  saveCivitaiQueue();
  renderCivitaiQueue();
  toast(`Added ${added} image${added === 1 ? "" : "s"} to the Civitai post queue.`);
  switchTab("civitai");
}

async function markCivitaiPosted(index, posted) {
  const item = state.galleryItems[index];
  if (!item) return;
  try {
    if (posted) {
      const url = prompt("Civitai post URL (e.g. https://civitai.red/posts/12345):", item.asset.civitai_url || "");
      if (url === null) return;
      await api(`/api/assets/${item.asset.id}/civitai-mark`, { method: "POST", body: JSON.stringify({ posted: true, url }) });
    } else {
      if (!confirm("Remove the Civitai posted mark from this image?")) return;
      await api(`/api/assets/${item.asset.id}/civitai-mark`, { method: "POST", body: JSON.stringify({ posted: false }) });
    }
    toast(posted ? "Marked as posted." : "Civitai mark removed.");
    showImageDetail(index);
  } catch (error) { toast(error.message, true); }
}

function serviceChip(selector, online, detail, busy = false) {
  const chip = $(selector);
  chip.classList.toggle("is-online", online && !busy);
  chip.classList.toggle("is-busy", online && busy);
  chip.classList.toggle("is-offline", !online);
  $("small", chip).textContent = detail;
}

async function refreshStatus() {
  try {
    const payload = await api("/api/status");
    const { lm, comfy, guides, gpu } = payload.services;
    const lmLoaded = lm.online ? (lm.loaded || []).length : 0;
    serviceChip("#lmStatus", lm.online, lm.online ? (lmLoaded ? `${lmLoaded} model loaded` : "no model loaded") : "offline", lmLoaded > 0);
    const comfyBusy = comfy.online && (comfy.queue_running || comfy.queue_pending);
    serviceChip("#comfyStatus", comfy.online, comfy.online ? (comfyBusy ? `${comfy.queue_running} running · ${comfy.queue_pending} queued` : "queue idle") : "offline", comfyBusy);
    serviceChip("#guideStatus", true, `${guides?.count || 0} MD files`);
    $("#vramFree").textContent = gpu ? `${(gpu.free_mb / 1024).toFixed(1)} GB` : "—";
    const queueCount = $("#queueCount");
    if (queueCount) queueCount.textContent = `${payload.queued ?? 0} queued`;
    state.queuedCount = Number(payload.queued || 0);
    $("#stopEntireQueue").disabled = !(payload.active?.running || state.queuedCount > 0);
    const backendActiveId = payload.active?.batch_id || null;
    if (backendActiveId) {
      state.activeBatchId = backendActiveId;
      startActivePolling();
    } else if (state.activeBatchId) {
      // Recover terminal state if the batch finished while a request or tab was unavailable.
      refreshActive();
    }
  } catch (error) {
    console.warn(error);
  }
}

function batchStatusClass(status) {
  if (["complete", "complete_with_errors"].includes(status)) return "is-complete";
  if (["running", "queued", "pausing", "paused"].includes(status)) return "is-running";
  return "";
}

function renderLive(batch, active, activeInfo = null) {
  state.liveBatchId = batch.id;
  const titleStatus = batch.status.replaceAll("_", " ");
  document.title = `${titleStatus[0].toUpperCase()}${titleStatus.slice(1)}: ${batch.title} - WildCat Export Edition`;
  $("#liveTitle").textContent = batch.title;
  const status = $("#liveState");
  status.textContent = batch.status.replaceAll("_", " ");
  status.className = `state-pill ${batchStatusClass(batch.status)}`;
  $("#livePhase").textContent = batch.error || batch.phase || "Working…";
  const total = Number(batch.total_runs || 0);
  const complete = Number(batch.completed_runs || 0);
  const percentage = total ? Math.min(100, (complete / total) * 100) : (["running", "queued"].includes(batch.status) ? 8 : 0);
  $("#progressBar").style.width = `${percentage}%`;
  $("#progressText").textContent = batch.status === "queued" && batch.queue_position
    ? `queue #${batch.queue_position}`
    : (total ? `${complete} / ${total} runs` : `${batch.total_prompts || 0} prompts`);
  const controllable = active && ["queued", "running", "pausing", "paused"].includes(batch.status);
  const resumable = (active && batch.status === "paused")
    || (!active && ["queued", "interrupted", "failed", "complete_with_errors", "cancelled"].includes(batch.status));
  const cancelable = controllable
    || (!active && ["queued", "interrupted", "failed", "paused"].includes(batch.status));
  const currentPrompt = active && activeInfo?.current_prompt;
  const promptActionPending = currentPrompt?.action_pending;
  const canControlPrompt = Boolean(currentPrompt && !promptActionPending);
  $("#retryPrompt").disabled = !canControlPrompt;
  $("#regeneratePrompt").disabled = !canControlPrompt;
  $("#skipPrompt").disabled = !canControlPrompt;
  $("#currentPromptState").textContent = currentPrompt
    ? `Current prompt #${currentPrompt.position}, run ${currentPrompt.run_number}/${currentPrompt.target_runs}${promptActionPending ? ` · ${promptActionPending} requested…` : ""}`
    : "No ComfyUI prompt is running.";
  $("#currentPromptState").classList.toggle("is-active", Boolean(currentPrompt));
  $("#pauseBatch").disabled = !(active && batch.status === "running");
  $("#resumeBatch").disabled = !resumable;
  $("#resumeBatch").hidden = !resumable;
  $("#pauseBatch").hidden = resumable;
  $("#cancelBatch").disabled = !cancelable;
  $("#jobControlHelp").textContent = batch.status === "pausing"
    ? "Pausing… the current image is interrupted and will retry when you resume."
    : batch.status === "paused"
      ? "Paused. Resume retries the interrupted image and continues this job."
      : batch.status === "cancelled"
        ? "Cancelled. Resume continues unfinished work. Saved images are kept."
        : ["complete", "complete_with_errors"].includes(batch.status)
          ? "Finished. Your saved images are in Image library."
          : "Pause interrupts the current image. Cancel keeps saved images; queued jobs continue.";
  $("#stopEntireQueue").disabled = !(active || Number(activeInfo?.queued_count ?? state.queuedCount ?? 0) > 0);
  const events = batch.events || [];
  $("#eventLog").innerHTML = events.length ? events.map((event) => {
    const date = new Date(event.created_at);
    return `<p class="${escapeHtml(event.level)}"><time>${Number.isNaN(date.valueOf()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>${escapeHtml(event.message)}</p>`;
  }).join("") : "<p>Waiting for the first event…</p>";
}

async function refreshActive() {
  if (!state.activeBatchId) return;
  try {
    const payload = await api(`/api/batches/${state.activeBatchId}`);
    renderLive(payload.batch, payload.active?.batch_id === state.activeBatchId, payload.active);
    if (!["queued", "running", "pausing", "paused"].includes(payload.batch.status)) {
      clearInterval(state.activeTimer);
      state.activeTimer = null;
      state.activeBatchId = null;
      await refreshStatus();
      await refreshBatches();
      const successful = ["complete", "complete_with_errors"].includes(payload.batch.status);
      document.title = successful ? `Complete: ${payload.batch.title} - WildCat Export Edition` : "WildCat Export Edition";
      setMessage(
        "#batchMessage",
        successful
          ? `Last batch finished: ${payload.batch.title} (${payload.batch.completed_runs}/${payload.batch.total_runs} runs).`
          : `Last batch ended: ${payload.batch.status.replaceAll("_", " ")}.`,
        successful ? "success" : "error",
      );
      toast(payload.batch.status === "complete" ? "Batch complete. Both model families are unloaded." : `Batch ended: ${payload.batch.status.replaceAll("_", " ")}`,
        ["failed", "cancelled"].includes(payload.batch.status));
    }
  } catch (error) {
    // A transient API error must not permanently stop completion detection.
    console.warn(error);
    $("#livePhase").textContent = "Connection interrupted; completion monitoring will retry automatically.";
  }
}

function startActivePolling() {
  if (!state.activeTimer) {
    refreshActive();
    state.activeTimer = setInterval(refreshActive, 2000);
  }
}

async function recoverWorkflowState() {
  if (state.workflow && state.workflows.length) return true;
  try {
    const payload = await api("/api/bootstrap");
    state.config = payload.config || state.config;
    state.workflows = payload.workflows || [];
    state.workflow = payload.workflow || null;
    renderWorkflowChoices(state.config.active_workflow_id);
    renderWorkflow(state.workflow);
    return Boolean(state.workflow && state.workflows.length);
  } catch (error) {
    console.warn(error);
    return false;
  }
}

async function showLatestBatch() {
  const latest = state.batches[0];
  if (!latest) return;
  try {
    const payload = await api(`/api/batches/${latest.id}`);
    renderLive(payload.batch, false, payload.active);
  } catch (error) {
    console.warn(error);
  }
}

async function startBatch(event) {
  event.preventDefault();
  setMessage("#batchMessage", "");
  if (!(await recoverWorkflowState())) {
    setMessage("#batchMessage", "Upload and map a ComfyUI API workflow in Connections & workflow first.", "error");
    switchTab("settings");
    return;
  }
  const source = $('input[name="source"]:checked').value;
  let body;
  if (source === "guides") {
    const guides = state.guides
      .filter((guide) => state.selectedGuides.has(guide.id))
      .map((guide) => ({ id: guide.id, name: guide.name, content: guide.content }));
    body = {
      source,
      workflow_id: $("#batchWorkflow").value,
      title: $("#batchTitle").value.trim(),
      brief: $("#batchBrief").value.trim(),
      model: $("#promptModel").value,
      guides,
      prompts_per_guide: Number($("#promptsPerGuide").value),
      images_per_prompt: Number($("#imagesPerPrompt").value),
      word_limit: $("#wordLimit").value,
      temperature: Number($("#temperature").value),
      negative_enabled: $("#negativeEnabled").checked,
      reference_ids: [...state.selectedReferenceIds],
      reference_mode: $('input[name="referenceMode"]:checked')?.value || "inspiration",
      queue_only: $("#queueOnly")?.checked || false,
    };
    if (body.reference_ids.length) {
      const chosenModel = state.models.find((model) => model.id === body.model);
      if (chosenModel && !chosenModel.vision) {
        setMessage("#batchMessage", "Choose a vision-capable prompt backend model when reference media is selected.", "error");
        return;
      }
    }
  } else {
    body = {
      source,
      workflow_id: $("#batchWorkflow").value,
      title: $("#pasteTitle").value.trim(),
      brief: "Imported prompt list",
      pasted_prompts: $("#pastedPrompts").value,
      images_per_prompt: Number($("#pasteImagesPerPrompt").value),
      queue_only: $("#queueOnly")?.checked || false,
    };
  }
  const button = $("#startBatch");
  button.disabled = true;
  button.textContent = "Starting relay…";
  try {
    const payload = await api("/api/batches", { method: "POST", body: JSON.stringify(body) });
    if (payload.reference_count) {
      state.references = payload.references || [];
      state.selectedReferenceIds.clear();
      persistReferenceSelection();
      renderReferenceTray();
    }
    state.activeBatchId = payload.active_batch_id || (payload.queued ? null : payload.batch_id);
    if (payload.queued) {
      const referenceNote = payload.reference_count
        ? ` ${payload.reference_count} temporary references are secured for this job; ${payload.effective_prompts_per_guide} prompts per guide.`
        : "";
      const queueNote = body.queue_only && !payload.active_batch_id
        ? "Start it from Image library when you're ready."
        : "It will start after earlier jobs finish.";
      setMessage("#batchMessage", `Saved as queue #${payload.queue_position || "—"}. ${queueNote}${referenceNote}`, "success");
      toast("Batch added to the automatic queue.");
    } else {
      const referenceNote = payload.reference_count
        ? ` ${payload.reference_count} temporary references will be used separately and deleted after completion.`
        : "";
      setMessage("#batchMessage", `Relay started. You can leave this page open and follow the live job.${referenceNote}`, "success");
    }
    document.title = "Running - WildCat Export Edition";
    if (state.activeBatchId) startActivePolling();
    refreshBatches();
    refreshStatus();
  } catch (error) {
    setMessage("#batchMessage", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Start the full relay";
  }
}

function batchListMarkup(batch) {
  const date = new Date(batch.created_at);
  return `<a class="batch-list-item ${batch.id === state.selectedBatchId ? "is-active" : ""}" data-batch-id="${batch.id}" href="/?batch=${encodeURIComponent(batch.id)}">
    <strong>${escapeHtml(batch.title)}</strong>
    <span><em>${escapeHtml(batch.status.replaceAll("_", " "))}${batch.queue_position ? ` #${batch.queue_position}` : ""}</em><em>${batch.asset_count || 0} outputs · ${Number.isNaN(date.valueOf()) ? "" : date.toLocaleDateString()}</em></span>
  </a>`;
}

async function refreshBatches(query = $("#librarySearch")?.value || "") {
  try {
    const payload = await api(`/api/batches?q=${encodeURIComponent(query)}`);
    state.batches = payload.batches || [];
    $("#batchList").innerHTML = state.batches.length ? state.batches.map(batchListMarkup).join("") : '<p class="empty-note">No matching batches.</p>';
    renderGalleryBatchChoices();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderGalleryBatchChoices() {
  const batches = state.batches.filter((batch) => batch.asset_count > 0);
  const options = batches.length
    ? batches.map((batch) => `<option value="${escapeHtml(batch.id)}">${escapeHtml(batch.title)} · ${batch.asset_count} image${batch.asset_count === 1 ? "" : "s"}</option>`).join("")
    : '<option value="">No batches with images yet</option>';
  const selections = [
    ["#galleryBatchSelect", state.selectedGalleryBatchId || state.selectedBatchId || batches[0]?.id],
    ["#wallBatchSelect", state.selectedWallBatchId || state.selectedGalleryBatchId || state.selectedBatchId || batches[0]?.id],
    ["#postprocessBatchSelect", state.selectedPostprocessBatchId || state.selectedGalleryBatchId || state.selectedBatchId || batches[0]?.id],
  ];
  for (const [selector, preferred] of selections) {
    const select = $(selector);
    if (!select) continue;
    select.innerHTML = options;
    if (preferred && batches.some((batch) => batch.id === preferred)) select.value = preferred;
  }
}

function mediaUrl(path) {
  return `/media/${encodeURIComponent(path)}`;
}

function assetWorkflowLabel(asset) {
  const provenance = asset.source?.vram_relay;
  if (!provenance) return "Workflow not recorded";
  const inferred = provenance.inferred_from_metadata ? " - found in PNG metadata" : "";
  return `${provenance.workflow_name || provenance.workflow_file || "Unknown workflow"}${inferred}`;
}

function assetGeometry(asset) {
  const width = Math.max(1, Number(asset.width) || 1);
  const height = Math.max(1, Number(asset.height) || 1);
  return { width, height, ratio: `${width} / ${height}` };
}

function assetMarkup(asset) {
  const url = mediaUrl(asset.local_path);
  const geometry = assetGeometry(asset);
  const provenance = asset.source?.vram_relay;
  const inferred = provenance?.inferred_from_metadata ? " · found in PNG metadata" : "";
  const workflowInfo = provenance
    ? `<figcaption><strong>API workflow:</strong> ${escapeHtml(provenance.workflow_name || provenance.workflow_file || "Unknown")}${inferred}</figcaption>`
    : "<figcaption><strong>API workflow:</strong> not recorded for this older image</figcaption>";
  if (["images", "gifs"].includes(asset.kind)) {
    return `<figure class="asset-card"><a href="${url}" target="_blank" rel="noreferrer"><img src="${url}" loading="lazy" width="${geometry.width}" height="${geometry.height}" alt="Generated output ${escapeHtml(asset.filename)}"></a>${workflowInfo}</figure>`;
  }
  return `<figure class="asset-card"><a href="${url}" target="_blank" rel="noreferrer">Open ${escapeHtml(asset.kind)} output</a>${workflowInfo}</figure>`;
}

function orderedPromptAssets(prompt) {
  const assets = [...(prompt.assets || [])];
  const assetIds = new Set(assets.map((asset) => asset.id));
  const children = new Map();
  for (const asset of assets) {
    const parentId = asset.source?.vram_relay?.source_asset_id;
    if (!parentId || !assetIds.has(parentId)) continue;
    const group = children.get(parentId) || [];
    group.push(asset);
    children.set(parentId, group);
  }
  const ordered = [];
  const visited = new Set();
  const appendBranch = (asset) => {
    if (!asset || visited.has(asset.id)) return;
    visited.add(asset.id);
    ordered.push(asset);
    (children.get(asset.id) || []).forEach(appendBranch);
  };
  assets
    .filter((asset) => !assetIds.has(asset.source?.vram_relay?.source_asset_id))
    .forEach(appendBranch);
  assets.forEach(appendBranch);
  return ordered;
}

function civitaiTileBadge(asset) {
  return asset?.civitai_posted ? '<span class="civitai-tile-badge" title="Posted to Civitai">✓ CIVITAI</span>' : "";
}

async function quickPostToCivitai(index) {
  const item = state.galleryItems[index];
  if (!item) return;
  civitaiQueueAdd(item);
}

function loadCivitaiQueue() {
  try { state.civitaiQueue = JSON.parse(localStorage.getItem("civitaiPostQueue") || "[]") || []; }
  catch (_) { state.civitaiQueue = []; }
  if (!Array.isArray(state.civitaiQueue)) state.civitaiQueue = [];
}

function saveCivitaiQueue() {
  try { localStorage.setItem("civitaiPostQueue", JSON.stringify(state.civitaiQueue)); } catch (_) {}
}

function civitaiQueueAdd(item) {
  const asset = item.asset;
  if (!asset) return;
  if (asset.civitai_posted) { toast("Already posted to Civitai."); return; }
  loadCivitaiQueue();
  if (state.civitaiQueue.some((entry) => entry.asset_id === asset.id)) {
    toast("Already in the Civitai post queue.");
    renderCivitaiQueue();
    return;
  }
  state.civitaiQueue.push({
    asset_id: asset.id,
    batch_id: asset.batch_id,
    filename: asset.filename,
    local_path: asset.local_path,
    width: asset.width,
    height: asset.height,
  });
  saveCivitaiQueue();
  renderCivitaiQueue();
  toast(`Added to Civitai post queue (${state.civitaiQueue.length} queued).`);
}

function civitaiQueueRemove(assetId) {
  state.civitaiQueue = state.civitaiQueue.filter((entry) => entry.asset_id !== assetId);
  saveCivitaiQueue();
  renderCivitaiQueue();
}

function civitaiQueueClear() {
  if (!state.civitaiQueue.length) return;
  if (!confirm(`Remove all ${state.civitaiQueue.length} image${state.civitaiQueue.length === 1 ? "" : "s"} from the queue?`)) return;
  state.civitaiQueue = [];
  saveCivitaiQueue();
  renderCivitaiQueue();
}

function renderCivitaiQueue() {
  const view = $("#civitaiQueueView");
  const count = $("#civitaiQueueCount");
  const publishButton = $("#civitaiQueuePublish");
  if (!view) return;
  loadCivitaiQueue();
  if (count) count.textContent = `${state.civitaiQueue.length} image${state.civitaiQueue.length === 1 ? "" : "s"} queued`;
  if (publishButton) publishButton.disabled = !state.civitaiQueue.length;
  if (!state.civitaiQueue.length) {
    if (view) view.innerHTML = '<div class="gallery-empty"><strong>The post queue is empty.</strong><p>Use the "Post to Civitai" button on any image in Gallery view to stage it here.</p></div>';
    return;
  }
  if (view) {
    view.innerHTML = state.civitaiQueue.map((entry) => `
      <figure class="civitai-queue-item" data-queue-asset="${escapeHtml(entry.asset_id)}">
        <img src="${mediaUrl(entry.local_path)}" loading="lazy" alt="${escapeHtml(entry.filename)}">
        <button class="civitai-queue-remove" type="button" data-queue-remove="${escapeHtml(entry.asset_id)}" aria-label="Remove from queue">✕</button>
      </figure>`).join("");
    $$("[data-queue-remove]", view).forEach((button) => button.addEventListener("click", () => civitaiQueueRemove(button.dataset.queueRemove)));
  }
}

async function publishCivitaiQueue() {
  loadCivitaiQueue();
  const queued = state.civitaiQueue.slice();
  if (!queued.length) return;
  const chunkSize = Math.max(1, Math.min(50, parseInt($("#civitaiChunkSize")?.value || "20", 10) || 20));
  const chunks = [];
  for (let i = 0; i < queued.length; i += chunkSize) chunks.push(queued.slice(i, i + chunkSize));
  if (!confirm(`Publish ${queued.length} image${queued.length === 1 ? "" : "s"} to Civitai?\n\nThis creates ${chunks.length} PUBLISHED post${chunks.length === 1 ? "" : "s"} (up to ${chunkSize} images each) with no title or description.\n\nThis cannot be undone from WildCat — review drafts manually if you prefer.`)) return;
  const button = $("#civitaiQueuePublish");
  const original = button?.textContent || "Publish";
  if (button) { button.disabled = true; button.textContent = "Publishing…"; }
  setMessage("#civitaiQueueMessage", `Publishing ${queued.length} image${queued.length === 1 ? "" : "s"} as ${chunks.length} post${chunks.length === 1 ? "" : "s"}…`);
  const created = [];
  const warnings = [];
  try {
    for (let i = 0; i < chunks.length; i++) {
      const chunk = chunks[i];
      setMessage("#civitaiQueueMessage", `Publishing post ${i + 1} of ${chunks.length} (${chunk.length} images)…`);
      const payload = await api("/api/civitai/post", {
        method: "POST",
        body: JSON.stringify({ asset_ids: chunk.map((entry) => entry.asset_id), publish: true, mode: "single_post" }),
      });
      (payload.posts || []).forEach((post) => { if (post.url) created.push(post.url); });
      if (payload.warnings?.length) {
        warnings.push(...payload.warnings);
        setMessage("#civitaiQueueMessage", `Post ${i + 1}: ${payload.warnings[0]}`, "error");
      }
    }
    state.civitaiQueue = [];
    saveCivitaiQueue();
    renderCivitaiQueue();
    const links = created.filter(Boolean);
    $("#civitaiQueueResults").innerHTML = links.length
      ? `<div class="civitai-results"><strong>${links.length} post${links.length === 1 ? "" : "s"} published:</strong>${links.map((link) => `<a href="${escapeHtml(link)}" target="_blank" rel="noreferrer">${escapeHtml(link)}</a>`).join("")}</div>`
      : "";
    toast(`${links.length} Civitai post${links.length === 1 ? "" : "s"} published.`);
    if (links.length && confirm("Open the first published post now?")) window.open(links[0], "_blank", "noopener");
    await refreshBatches();
  } catch (error) {
    setMessage("#civitaiQueueMessage", error.message, "error");
  } finally {
    if (publishButton) publishButton.disabled = !state.civitaiQueue.length;
  }
}

function assetCarouselMarkup(prompt) {
  const assets = orderedPromptAssets(prompt);
  if (!assets.length) return '<div class="asset-grid"><p class="empty-note">No saved output for this prompt yet.</p></div>';
  const slides = assets.map((asset, index) => {
    const url = mediaUrl(asset.local_path);
    const geometry = assetGeometry(asset);
    const provenance = asset.source?.vram_relay || {};
    const kind = provenance.image_tool_kind;
    const typeLabel = kind === "edit" ? "Edited image" : kind === "upscale" ? "Upscaled image" : "Original output";
    const instruction = kind === "edit" && provenance.edit_instruction
      ? `<span class="library-carousel-instruction">Edit: ${escapeHtml(provenance.edit_instruction)}</span>`
      : "";
    const visual = ["images", "gifs"].includes(asset.kind)
      ? `<a href="${url}" target="_blank" rel="noreferrer"><img src="${url}" loading="lazy" width="${geometry.width}" height="${geometry.height}" alt="${escapeHtml(typeLabel)} ${escapeHtml(asset.filename)}"></a>`
      : `<a class="library-carousel-file" href="${url}" target="_blank" rel="noreferrer">Open ${escapeHtml(asset.kind)} output</a>`;
    return `<figure class="library-carousel-slide" data-carousel-slide="${index}" ${index ? "hidden" : ""}>
      <span class="library-carousel-type">${escapeHtml(typeLabel)}</span>
      ${civitaiTileBadge(asset)}
      ${visual}
      <figcaption><strong>${escapeHtml(asset.filename)}</strong><span>${geometry.width} × ${geometry.height} · ${escapeHtml(assetWorkflowLabel(asset))}</span>${instruction}</figcaption>
    </figure>`;
  }).join("");
  const controls = assets.length > 1
    ? `<div class="library-carousel-controls" aria-label="Image navigation">
        <button class="secondary-button" type="button" data-carousel-previous aria-label="Previous image">‹ Previous</button>
        <span data-carousel-position>1 / ${assets.length}</span>
        <button class="secondary-button" type="button" data-carousel-next aria-label="Next image">Next ›</button>
      </div>`
    : '<div class="library-carousel-controls library-carousel-single"><span>1 / 1</span></div>';
  return `<div class="asset-grid library-asset-carousel" data-library-carousel data-current-index="0" tabindex="0" aria-label="Prompt output images. Use the previous and next buttons or arrow keys."><div class="library-carousel-stage">${slides}</div>${controls}</div>`;
}

function updateLibraryCarousel(carousel, requestedIndex) {
  const slides = $$('[data-carousel-slide]', carousel);
  if (!slides.length) return;
  const index = ((requestedIndex % slides.length) + slides.length) % slides.length;
  carousel.dataset.currentIndex = String(index);
  slides.forEach((slide, slideIndex) => { slide.hidden = slideIndex !== index; });
  const position = $('[data-carousel-position]', carousel);
  if (position) position.textContent = `${index + 1} / ${slides.length}`;
}

function bindLibraryCarousels(root = document) {
  $$('[data-library-carousel]', root).forEach((carousel) => {
    const move = (amount) => updateLibraryCarousel(carousel, Number(carousel.dataset.currentIndex || 0) + amount);
    $('[data-carousel-previous]', carousel)?.addEventListener("click", () => move(-1));
    $('[data-carousel-next]', carousel)?.addEventListener("click", () => move(1));
    carousel.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft") { event.preventDefault(); move(-1); }
      if (event.key === "ArrowRight") { event.preventDefault(); move(1); }
    });
  });
}

function imageItems(batch) {
  return (batch.prompts || []).flatMap((prompt) =>
    (prompt.assets || [])
      .filter((asset) => ["images", "gifs"].includes(asset.kind))
      .map((asset) => ({ prompt, asset }))
  );
}

function starRatingMarkup(item, index) {
  const current = Number(item.asset.rating || 0);
  return `<div class="star-rating" aria-label="Rate image ${item.prompt.position}">
    ${[1, 2, 3, 4, 5].map((rating) => `<button type="button" data-rating-index="${index}" data-rating="${rating}" aria-label="${rating} star${rating === 1 ? "" : "s"}" aria-pressed="${rating <= current}" class="${rating <= current ? "is-active" : ""}" title="${rating === current ? "Click again to clear rating" : `Rate ${rating} stars`}">★</button>`).join("")}
  </div>`;
}

function guideRankingsMarkup() {
  const guides = new Map();
  for (const { prompt, asset } of state.galleryItems) {
    const guide = prompt.guide || "Imported prompts";
    const record = guides.get(guide) || { guide, total: 0, rated: 0, points: 0 };
    record.total += 1;
    const rating = Number(asset.rating || 0);
    if (rating > 0) {
      record.rated += 1;
      record.points += rating;
    }
    guides.set(guide, record);
  }
  const ranked = [...guides.values()].sort((left, right) => {
    const leftAverage = left.rated ? left.points / left.rated : -1;
    const rightAverage = right.rated ? right.points / right.rated : -1;
    return rightAverage - leftAverage || right.rated - left.rated || left.guide.localeCompare(right.guide);
  });
  return `<details class="guide-rankings" open>
    <summary><span><strong>Markdown-guide rankings</strong><small>Average of your starred images</small></span></summary>
    <div class="guide-ranking-list">
      ${ranked.map((item, position) => {
        const average = item.rated ? (item.points / item.rated).toFixed(2) : "Unrated";
        return `<div class="guide-ranking-row"><b>${item.rated ? `${position + 1}.` : "—"}</b><strong title="${escapeHtml(item.guide)}">${escapeHtml(item.guide)}</strong><span>${average}${item.rated ? " ★" : ""}</span><small>${item.rated}/${item.total} rated</small></div>`;
      }).join("") || '<p class="empty-note">No markdown guides in this batch.</p>'}
    </div>
  </details>`;
}

function galleryAssetMarkup(item, index) {
  const { asset, prompt } = item;
  const url = mediaUrl(asset.local_path);
  const geometry = assetGeometry(asset);
  return `<article class="gallery-feed-card">
      <button class="gallery-feed-image" type="button" data-gallery-index="${index}" style="aspect-ratio:${geometry.ratio}" aria-label="View details for image ${prompt.position}">
        <img src="${url}" loading="lazy" width="${geometry.width}" height="${geometry.height}" alt="Generated output ${escapeHtml(asset.filename)}">
        <span class="gallery-number">#${prompt.position}</span>
        ${civitaiTileBadge(asset)}
      </button>
      <div class="gallery-feed-meta">
        <strong>${escapeHtml(prompt.guide || "Imported prompt")}</strong>
        <span>${escapeHtml(assetWorkflowLabel(asset))}</span>
        <div class="gallery-card-actions">${starRatingMarkup(item, index)}${
          asset.civitai_posted
            ? `<a class="gallery-civitai-button is-posted" href="${escapeHtml(asset.civitai_url || "#")}" target="_blank" rel="noreferrer" title="Open the Civitai post">Posted ✓</a>`
            : `<button class="gallery-civitai-button" type="button" data-civitai-post-index="${index}" title="Create a draft post on Civitai">Post to Civitai</button>`
        }<button class="gallery-delete-button" type="button" data-delete-gallery-index="${index}">Recycle image</button></div>
      </div>
    </article>`;
}

function renderGalleryBatch(batch) {
  state.currentBatch = batch;
  state.selectedGalleryBatchId = batch.id;
  state.galleryItems = imageItems(batch);
  renderGalleryBatchChoices();
  const view = $("#galleryView");
  view.innerHTML = `
    <div class="gallery-batch-heading">
      <div><p class="step-label">${escapeHtml(batch.status.replaceAll("_", " "))}</p><h2>${escapeHtml(batch.title)}</h2></div>
      <span>${state.galleryItems.length} image${state.galleryItems.length === 1 ? "" : "s"} · click any image for details</span>
    </div>
    ${state.galleryItems.length ? `<div id="guideRankings">${guideRankingsMarkup()}</div>` : ""}
    ${state.galleryItems.length
      ? `<div class="gallery-feed">${state.galleryItems.map(galleryAssetMarkup).join("")}</div>`
      : '<div class="gallery-empty"><strong>No saved images in this batch yet.</strong><p>Finished ComfyUI outputs will appear here automatically.</p></div>'}`;
  $$("[data-gallery-index]", view).forEach((button) => button.addEventListener("click", () => {
    showImageDetail(Number(button.dataset.galleryIndex));
  }));
  $$("[data-delete-gallery-index]", view).forEach((button) => button.addEventListener("click", () => {
    deleteGalleryImage(Number(button.dataset.deleteGalleryIndex));
  }));
  $$("[data-civitai-post-index]", view).forEach((button) => button.addEventListener("click", () => {
    quickPostToCivitai(Number(button.dataset.civitaiPostIndex));
  }));
  bindRatingButtons(view);
}

async function representativePixel(asset) {
  const image = new Image();
  image.decoding = "async";
  image.src = mediaUrl(asset.local_path);
  if (image.decode) await image.decode();
  else await new Promise((resolve, reject) => {
    image.addEventListener("load", resolve, { once: true });
    image.addEventListener("error", reject, { once: true });
  });
  const canvas = document.createElement("canvas");
  canvas.width = 1;
  canvas.height = 1;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(image, 0, 0, 1, 1);
  return [...context.getImageData(0, 0, 1, 1).data];
}

async function deleteGalleryImage(index) {
  const item = state.galleryItems[index];
  if (!item) return;
  const message = `Recycle "${item.asset.filename}"? WildCat will permanently remove its archived copy and keep only one color pixel in the recycle collage. The original ComfyUI output is not deleted.`;
  if (!confirm(message)) return;
  try {
    const pixel = await representativePixel(item.asset);
    await api(`/api/assets/${item.asset.id}`, {
      method: "DELETE",
      body: JSON.stringify({ pixel }),
    });
    closeImageDetail();
    await openGalleryBatch(item.asset.batch_id);
    toast("Image recycled. One representative pixel was added to the collage.");
  } catch (error) {
    toast(error.message, true);
  }
}

function imageToolResultMarkup(asset) {
  const provenance = asset.source?.vram_relay || {};
  const kind = provenance.image_tool_kind || "edit";
  const instruction = provenance.edit_instruction || "No text instruction (upscale)";
  const geometry = assetGeometry(asset);
  const url = mediaUrl(asset.local_path);
  return `<article class="tool-result-card">
    <a href="${url}" target="_blank" rel="noreferrer" style="aspect-ratio:${geometry.ratio}"><img src="${url}" loading="lazy" width="${geometry.width}" height="${geometry.height}" alt="${escapeHtml(kind)} result ${escapeHtml(asset.filename)}">${civitaiTileBadge(asset)}</a>
    <div><span>${escapeHtml(imageToolLabel(kind))} result</span><strong>${escapeHtml(instruction)}</strong><small>${escapeHtml(provenance.workflow_name || provenance.workflow_file || "Unknown workflow")} · ${escapeHtml(asset.batch_title || "Saved batch")}</small></div>
  </article>`;
}

async function loadImageToolResults() {
  const view = $("#toolResultsView");
  if (!view) return;
  view.innerHTML = '<div class="gallery-empty"><strong>Loading edit and upscale results…</strong></div>';
  try {
    const payload = await api("/api/image-tool-results");
    state.imageToolResults = payload.images || [];
    view.innerHTML = state.imageToolResults.length
      ? `<div class="tool-results-heading"><strong>${state.imageToolResults.length} result${state.imageToolResults.length === 1 ? "" : "s"}</strong><span>Newest first</span></div><div class="tool-results-grid">${state.imageToolResults.map(imageToolResultMarkup).join("")}</div>`
      : '<div class="gallery-empty"><strong>No edit or upscale results yet.</strong><p>Open an image in Gallery view and send it to an image-tool API.</p></div>';
  } catch (error) {
    view.innerHTML = `<div class="gallery-empty"><strong>Could not load results</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function loadRecycleBin() {
  const view = $("#recycleBinView");
  if (!view) return;
  try {
    state.recycleBin = await api("/api/recycle-bin");
    const bin = state.recycleBin;
    view.innerHTML = bin.count
      ? `<div class="recycle-summary"><p class="step-label">RECYCLED PIXELS</p><strong>${bin.count} image${bin.count === 1 ? "" : "s"} reduced to ${bin.count} pixel${bin.count === 1 ? "" : "s"}</strong><p>Canvas ${bin.current_collage} is ${bin.current_pixels.toLocaleString()} / ${bin.capacity_per_collage.toLocaleString()} pixels full (${bin.current_percent}%). Each fixed ${bin.width} × ${bin.height} canvas fills left-to-right, one deleted image at a time, then WildCat starts the next canvas.</p></div><div class="recycle-collage-list">${[...(bin.collages || [])].reverse().map((collage) => `<figure class="recycle-collage-wrap"><img src="${collage.url}?v=${Date.now()}" width="${bin.width}" height="${bin.height}" alt="Recycle collage ${collage.number} made from ${collage.pixel_count} images"><figcaption><strong>Canvas ${collage.number}</strong><span>${collage.pixel_count.toLocaleString()} / ${collage.capacity.toLocaleString()} pixels${collage.complete ? " · complete" : ""}</span></figcaption></figure>`).join("")}</div>`
      : '<div class="gallery-empty"><strong>The recycle collage is empty.</strong><p>Delete an individual image from Gallery view to add its pixel.</p></div>';
  } catch (error) {
    view.innerHTML = `<div class="gallery-empty"><strong>Could not load recycle bin</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function findArchivedImage(file) {
  const dropZone = $("#finderDropZone");
  setMessage("#finderMessage", `Searching the archive for "${file.name}"…`);
  $("#finderResults").innerHTML = "";
  $("#finderDropZone")?.classList.add("is-processing");
  try {
    const formData = new FormData();
    formData.append("image", file, file.name);
    const response = await fetch("/api/image-finder", { method: "POST", body: formData });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Search failed.");
    if (!payload.match_count) {
      setMessage("#finderMessage", `No archive match for "${file.name}". It may have been recycled out of the archive.`, "error");
      return;
    }
    setMessage("#finderMessage", `${payload.match_count} match${payload.match_count === 1 ? "" : "es"} found for "${file.name}":`, "success");
    $("#finderResults").innerHTML = `
      <div class="finder-results-head"><strong>${payload.match_count} match${payload.match_count === 1 ? "" : "es"}</strong><button class="secondary-button" id="finderDismiss" type="button">Dismiss</button></div>
      <div class="finder-grid">${payload.matches.map((match) => finderMatchMarkup(match)).join("")}</div>`;
    $("#finderDismiss")?.addEventListener("click", () => { $("#finderResults").innerHTML = ""; setMessage("#finderMessage", ""); });
    $$("#finderResults [data-finder-recycle]").forEach((button) => button.addEventListener("click", () => recycleFinderMatch(button.dataset.finderRecycle)));
  } catch (error) {
    setMessage("#finderMessage", error.message, "error");
  } finally {
    $("#finderDropZone")?.classList.remove("is-processing");
  }
}

function finderMatchMarkup(match) {
  const similarity = Math.max(0, Math.round(((64 - Number(match.hamming_distance || 64)) / 64) * 100));
  return `<figure class="finder-match" data-finder-id="${escapeHtml(match.id)}">
    <a href="${mediaUrl(match.local_path)}" target="_blank" rel="noreferrer"><img src="${mediaUrl(match.local_path)}" loading="lazy" alt="${escapeHtml(match.filename)}"></a>
    <figcaption>
      <strong>${similarity}% match</strong>
      <span>${escapeHtml(match.filename)}</span>
      <small>${escapeHtml(match.batch_title || "Saved batch")}</small>
      <div class="finder-actions">
        <button class="secondary-button" type="button" data-finder-recycle="${escapeHtml(match.id)}">Recycle this match</button>
      </div>
    </figcaption>
  </figure>`;
}

async function recycleFinderMatch(assetId) {
  const fig = document.querySelector(`#finderResults .finder-match[data-finder-id="${assetId}"]`);
  if (!fig) return;
  if (!confirm("Recycle this archived image? Its pixel is saved to the collage and the file is deleted.")) return;
  let pixel = [128, 128, 128, 255];
  const imgSrc = fig.querySelector("img")?.src;
  if (imgSrc) {
    try {
      const image = new Image();
      image.crossOrigin = "anonymous";
      image.src = imgSrc;
      await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; });
      const canvas = document.createElement("canvas");
      canvas.width = 1;
      canvas.height = 1;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(image, 0, 0, 1, 1);
      pixel = [...ctx.getImageData(0, 0, 1, 1).data];
    } catch (_) {}
  }
  try {
    await api(`/api/assets/${assetId}`, { method: "DELETE", body: JSON.stringify({ pixel }) });
    toast("Archived match recycled.");
    fig.remove();
    await loadRecycleBin();
    if (!$("#finderResults").querySelector(".finder-match")) { $("#finderResults").innerHTML = ""; setMessage("#finderMessage", ""); }
  } catch (error) { toast(error.message, true); }
}

async function findAndRecycleExternalImage(file, fileHandle) {
  const dropMessage = $("#recycleDropMessage");
  const dropZone = $("#recycleDropZone");
  setMessage("#recycleDropMessage", `Reading ${file.name}…`);
  dropZone?.classList.add("is-processing");
  try {
    const formData = new FormData();
    formData.append("image", file, file.name);
    const response = await fetch("/api/recycle-bin/find-and-delete", { method: "POST", body: formData });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Upload failed.");
    let diskDeleted = false;
    if (payload.external) {
      try {
        const delResp = await fetch("/api/recycle-bin/delete-file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name }),
        });
        const delPayload = await delResp.json().catch(() => ({}));
        if (delPayload.count > 0) diskDeleted = true;
      } catch (_) {}
      if (diskDeleted) {
        setMessage("#recycleDropMessage", `Deleted and recycled "${file.name}".`, "success");
      } else {
        setMessage("#recycleDropMessage", `Recycled pixel from "${file.name}". External original kept; this edition never deletes files outside its library.`, "success");
      }
    } else {
      setMessage("#recycleDropMessage", `Recycled "${payload.filename}" from batch "${payload.batch_title}".`, "success");
    }
    toast(`Recycled: ${file.name}`);
    await loadRecycleBin();
    return true;
  } catch (error) {
    setMessage("#recycleDropMessage", `${file.name}: ${error.message}`, "error");
    return false;
  } finally { dropZone?.classList.remove("is-processing"); }
}

async function recycleExternalFiles(files) {
  const isImage = (file) => (file.type || "").startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp)$/i.test(file.name);
  const accepted = [...files].filter(isImage);
  if (!accepted.length) { setMessage("#recycleDropMessage", "Drop image files only.", "error"); return; }
  const list = `${accepted.slice(0, 10).map((f) => f.name).join("\n")}${accepted.length > 10 ? `\n…and ${accepted.length - 10} more` : ""}`;
  if (!confirm(`Recycle ${accepted.length} image${accepted.length === 1 ? "" : "s"}?\n\n${list}\n\nArchived copies are removed and a pixel is saved for each. Original files on your disk are kept.`)) return;
  let recycled = 0;
  for (const file of accepted) { const result = await findAndRecycleExternalImage(file); if (result) recycled++; }
  if (accepted.length > 1) setMessage("#recycleDropMessage", `${recycled} of ${accepted.length} images recycled.`, recycled === accepted.length ? "success" : "error");
}

async function browseAndRecycle() {
  if (!window.showOpenFilePicker) {
    const input = $("#recycleDropFiles");
    if (input) { input.click(); return; }
    setMessage("#recycleDropMessage", "Your browser does not support folder browsing. Use Chrome or Edge, or drag files instead.", "error");
    return;
  }
  try {
    const handles = await window.showOpenFilePicker({
      types: [{ description: "Images", accept: { "image/*": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"] } }],
      multiple: true,
    });
    if (!handles.length) return;
    const names = [];
    for (const h of handles) { const f = await h.getFile(); names.push(f.name); }
    if (!confirm(`Recycle ${names.length} file${names.length === 1 ? "" : "s"}?\n\n${names.slice(0, 10).join("\n")}${names.length > 10 ? `\n…and ${names.length - 10} more` : ""}\n\nWildCat will sample a pixel for the collage and delete the file from disk if found in known folders.`)) return;
    setMessage("#recycleDropMessage", `Processing ${handles.length} file${handles.length === 1 ? "" : "s"}…`);
    let recycled = 0;
    for (const handle of handles) {
      const file = await handle.getFile();
      const result = await findAndRecycleExternalImage(file);
      if (result) recycled++;
    }
    if (handles.length > 1) setMessage("#recycleDropMessage", `${recycled} of ${handles.length} images recycled.`, recycled === handles.length ? "success" : "error");
  } catch (error) {
    if (error.name !== "AbortError") setMessage("#recycleDropMessage", error.message, "error");
  }
}

async function renameBatch(batchId) {
  const batch = state.batches?.find((b) => b.id === batchId);
  if (!batch) return;
  const newTitle = prompt("New batch name:", batch.title || "");
  if (newTitle === null || !newTitle.trim()) return;
  try {
    await api(`/api/batches/${batchId}/rename`, { method: "POST", body: JSON.stringify({ title: newTitle.trim() }) });
    toast("Batch renamed.");
    await refreshBatches();
    if (state.selectedBatchId === batchId) await openBatch(batchId);
  } catch (error) { toast(error.message, true); }
}

function confirmJobAction(title, description, actionLabel) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "job-confirm-dialog";
    dialog.setAttribute("aria-labelledby", "job-confirm-heading");
    dialog.innerHTML = `<form method="dialog"><h2 id="job-confirm-heading">${escapeHtml(title)}</h2><p>${escapeHtml(description)}</p><div class="confirmation-actions"><button class="secondary-button" value="cancel" autofocus>Keep working</button><button class="danger-button" value="confirm">${escapeHtml(actionLabel)}</button></div></form>`;
    dialog.addEventListener("close", () => {
      const confirmed = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(confirmed);
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
}

async function stopEntireQueue() {
  if (!await confirmJobAction("Cancel all jobs?", "Stops the current job and cancels every waiting job. Images already saved stay in your library.", "Cancel all jobs")) return;
  try {
    const payload = await api("/api/actions/queue-stop", { method: "POST", body: "{}" });
    state.activeBatchId = payload.active?.batch_id || null;
    if (payload.errors?.length) throw new Error(payload.errors.join("; "));
    toast(`All jobs cancelled (${payload.cancelled_queued ?? 0} waiting jobs). Saved images kept.`);
    await refreshBatches();
    refreshActive();
    if (!state.activeBatchId) await showLatestBatch();
  } catch (error) { toast(error.message, true); }
}

function bindRatingButtons(root = document) {
  $$("[data-rating-index]", root).forEach((button) => button.addEventListener("click", () => {
    const index = Number(button.dataset.ratingIndex);
    const requested = Number(button.dataset.rating);
    const current = Number(state.galleryItems[index]?.asset.rating || 0);
    saveAssetRating(index, current === requested ? 0 : requested);
  }));
}

async function saveAssetRating(index, rating) {
  const item = state.galleryItems[index];
  if (!item) return;
  try {
    const payload = await api(`/api/assets/${item.asset.id}/rating`, {
      method: "POST",
      body: JSON.stringify({ rating }),
    });
    item.asset.rating = payload.asset.rating;
    $$(`[data-rating-index="${index}"]`).forEach((button) => {
      const active = Number(button.dataset.rating) <= Number(item.asset.rating);
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const rankings = $("#guideRankings");
    if (rankings) rankings.innerHTML = guideRankingsMarkup();
    toast(item.asset.rating ? `Image #${item.prompt.position} rated ${item.asset.rating} stars.` : `Rating cleared for image #${item.prompt.position}.`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function openGalleryBatch(batchId) {
  if (!batchId) return;
  state.selectedGalleryBatchId = batchId;
  renderGalleryBatchChoices();
  $("#galleryView").innerHTML = '<div class="gallery-empty"><strong>Loading gallery…</strong></div>';
  try {
    const payload = await api(`/api/batches/${batchId}`);
    renderGalleryBatch(payload.batch);
  } catch (error) {
    $("#galleryView").innerHTML = `<div class="gallery-empty"><strong>Could not open gallery</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function wallAssetMarkup(item, index) {
  const url = mediaUrl(item.asset.local_path);
  const geometry = assetGeometry(item.asset);
  return `<button class="image-wall-tile" type="button" data-wall-index="${index}" style="aspect-ratio:${geometry.ratio}" aria-label="Enlarge image ${item.prompt.position}">
    <img src="${url}" loading="lazy" width="${geometry.width}" height="${geometry.height}" alt="Generated output ${escapeHtml(item.asset.filename)}">
    ${civitaiTileBadge(item.asset)}
  </button>`;
}

function renderWallBatch(batch) {
  state.selectedWallBatchId = batch.id;
  state.wallItems = imageItems(batch);
  renderGalleryBatchChoices();
  const view = $("#imageWallView");
  if (!state.wallItems.length) {
    view.innerHTML = '<div class="gallery-empty"><strong>No saved images in this batch yet.</strong><p>Finished ComfyUI outputs will appear here automatically.</p></div>';
  } else {
    const cols = Math.min(5, Math.max(2, Math.floor((window.innerWidth || 1280) / 260)));
    const buckets = Array.from({ length: cols }, () => []);
    const heights = new Array(cols).fill(0);
    state.wallItems.forEach((item, index) => {
      const geometry = assetGeometry(item.asset);
      const weight = 1 / Math.max(0.2, Number(geometry.ratio) || 1);
      let target = 0;
      for (let c = 1; c < cols; c++) { if (heights[c] < heights[target]) target = c; }
      buckets[target].push({ item, index });
      heights[target] += weight;
    });
    view.innerHTML = `<div class="image-wall-grid" style="--wall-cols:${cols}">${buckets.map((bucket) => `<div class="wall-col">${bucket.map(({ item, index }) => wallAssetMarkup(item, index)).join("")}</div>`).join("")}</div>`;
  }
  $$("[data-wall-index]", $("#imageWallView")).forEach((button) => button.addEventListener("click", () => {
    showImageLightbox(Number(button.dataset.wallIndex));
  }));
}

async function openWallBatch(batchId) {
  if (!batchId) return;
  state.selectedWallBatchId = batchId;
  renderGalleryBatchChoices();
  $("#imageWallView").innerHTML = '<div class="gallery-empty"><strong>Loading image wall…</strong></div>';
  try {
    const payload = await api(`/api/batches/${batchId}`);
    renderWallBatch(payload.batch);
  } catch (error) {
    $("#imageWallView").innerHTML = `<div class="gallery-empty"><strong>Could not open image wall</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function metadataStatusLabel(status) {
  return {
    compliant: "Civitai ready",
    ready: "Ready to convert",
    unavailable: "No Comfy metadata",
    unsupported: "Unsupported format",
  }[status] || status;
}

function renderPostprocessSummary(payload) {
  const counts = payload.counts || {};
  $("#postprocessSummary").innerHTML = `
    <div class="metadata-summary-card">
      <div><p class="step-label">SELECTED BATCH</p><h3>${escapeHtml(payload.batch.title)}</h3></div>
      <div class="metadata-counts">
        <span class="metadata-count is-compliant"><b>${counts.compliant || 0}</b>Civitai ready</span>
        <span class="metadata-count is-ready"><b>${counts.ready || 0}</b>Ready to convert</span>
        <span class="metadata-count"><b>${counts.unavailable || 0}</b>No Comfy data</span>
        <span class="metadata-count"><b>${counts.unsupported || 0}</b>Unsupported</span>
      </div>
    </div>`;
}

function metadataImageRow(item) {
  const selected = item.id === state.selectedMetadataAssetId ? "is-active" : "";
  const extracted = item.extracted || {};
  const details = [
    extracted.sampler,
    extracted.steps != null ? `${extracted.steps} steps` : "",
    extracted.seed != null ? `seed ${extracted.seed}` : "",
  ].filter(Boolean).join(" · ");
  return `<button class="metadata-image-row ${selected}" type="button" data-metadata-asset="${item.id}">
    <img src="${mediaUrl(item.local_path)}" loading="lazy" alt="">
    <span class="metadata-image-copy">
      <strong>#${item.position} · ${escapeHtml(item.filename)}</strong>
      <small>${escapeHtml(details || item.message)}</small>
      <em class="metadata-status is-${escapeHtml(item.status)}">${escapeHtml(metadataStatusLabel(item.status))}</em>
    </span>
  </button>`;
}

function renderPostprocessBatch(payload) {
  state.selectedPostprocessBatchId = payload.batch.id;
  state.postprocessImages = payload.images || [];
  renderGalleryBatchChoices();
  renderPostprocessSummary(payload);
  const list = $("#metadataImageList");
  list.innerHTML = state.postprocessImages.length
    ? state.postprocessImages.map(metadataImageRow).join("")
    : '<p class="empty-note">This batch has no archived images.</p>';
  $$("[data-metadata-asset]", list).forEach((button) => button.addEventListener("click", () => {
    loadMetadataPreview(button.dataset.metadataAsset);
  }));
  $("#convertMetadataBatch").disabled = !(payload.counts?.ready > 0);
  const preferred = state.postprocessImages.some((item) => item.id === state.selectedMetadataAssetId)
    ? state.selectedMetadataAssetId
    : state.postprocessImages[0]?.id;
  if (preferred) loadMetadataPreview(preferred);
  else $("#metadataPreview").innerHTML = '<div class="library-empty"><strong>No images to inspect.</strong></div>';
}

async function openPostprocessBatch(batchId) {
  if (!batchId) return;
  state.selectedPostprocessBatchId = batchId;
  renderGalleryBatchChoices();
  $("#metadataImageList").innerHTML = '<p class="empty-note">Reading embedded PNG metadata…</p>';
  $("#metadataPreview").innerHTML = '<div class="library-empty"><strong>Inspecting batch…</strong></div>';
  setMessage("#postprocessMessage", "");
  try {
    const payload = await api(`/api/postprocess/batches/${batchId}`);
    renderPostprocessBatch(payload);
  } catch (error) {
    $("#metadataPreview").innerHTML = `<div class="library-empty"><strong>Could not inspect metadata</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function metadataValue(value) {
  return value === null || value === undefined || value === "" ? "Not present" : String(value);
}

function metadataStrength(value) {
  if (typeof value !== "number") return metadataValue(value);
  return String(Math.round(value * 100000000) / 100000000);
}

function resourceMetadataMarkup(data) {
  const model = data.model_resource;
  const loras = Array.isArray(data.loras) ? data.loras : [];
  const resources = [];
  if (model) {
    resources.push(`
      <div class="metadata-resource ${model.resolved ? "is-resolved" : "is-missing"}">
        <span><strong>Checkpoint</strong><b>${escapeHtml(model.name || data.model || "Unknown model")}</b></span>
        <code>${model.resolved ? escapeHtml(model.hash || "Hash unavailable") : "File not found"}</code>
      </div>`);
  }
  loras.forEach((lora) => {
    resources.push(`
      <div class="metadata-resource ${lora.resolved ? "is-resolved" : "is-missing"}">
        <span><strong>LoRA · ${escapeHtml(metadataStrength(lora.strength))}</strong><b>${escapeHtml(lora.name || "Unnamed LoRA")}</b></span>
        <code>${lora.resolved ? escapeHtml(lora.hash || "Hash unavailable") : "File not found"}</code>
      </div>`);
  });
  if (!resources.length) return "";
  const resolved = [model, ...loras].filter((item) => item?.resolved && item?.hash).length;
  return `<section class="metadata-resources">
    <div class="metadata-resources-head"><strong>Civitai resources</strong><span>${resolved}/${resources.length} identified from local files</span></div>
    <div class="metadata-resource-list">${resources.join("")}</div>
  </section>`;
}

async function loadMetadataPreview(assetId) {
  if (!assetId) return;
  state.selectedMetadataAssetId = assetId;
  $$("[data-metadata-asset]", $("#metadataImageList")).forEach((button) => {
    button.classList.toggle("is-active", button.dataset.metadataAsset === assetId);
  });
  $("#metadataPreview").innerHTML = '<div class="library-empty"><strong>Reading image metadata…</strong></div>';
  try {
    const payload = await api(`/api/postprocess/assets/${assetId}`);
    const image = payload.image;
    const data = image.extracted || {};
    const canConvert = image.status === "ready";
    $("#metadataPreview").innerHTML = `
      <div class="metadata-preview-head">
        <div><p class="step-label">EXACT PREVIEW</p><h3>#${image.position} · ${escapeHtml(image.filename)}</h3></div>
        <em class="metadata-status is-${escapeHtml(image.status)}">${escapeHtml(metadataStatusLabel(image.status))}</em>
      </div>
      <p class="metadata-explanation">${escapeHtml(image.message)}</p>
      <div class="metadata-facts">
        <span><strong>Sampler</strong>${escapeHtml(metadataValue(data.sampler))}</span>
        <span><strong>Scheduler</strong>${escapeHtml(metadataValue(data.scheduler))}</span>
        <span><strong>Steps</strong>${escapeHtml(metadataValue(data.steps))}</span>
        <span><strong>CFG</strong>${escapeHtml(metadataValue(data.cfg_scale))}</span>
        <span><strong>Seed</strong>${escapeHtml(metadataValue(data.seed))}</span>
        <span><strong>Size</strong>${data.width && data.height ? `${data.width} × ${data.height}` : "Not present"}</span>
        <span><strong>Model</strong>${escapeHtml(metadataValue(data.model))}</span>
        <span><strong>Model hash</strong>${escapeHtml(metadataValue(data.model_hash))}</span>
        <span><strong>VAE</strong>${escapeHtml(metadataValue(data.vae))}</span>
      </div>
      ${resourceMetadataMarkup(data)}
      <div class="metadata-preserved"><strong>Preserved PNG metadata</strong><span>${escapeHtml((image.preserved_keys || []).join(", ") || "None found")}</span></div>
      <label class="metadata-parameters-label"><span>Civitai parameters written to the PNG</span>
        <pre class="metadata-parameters">${escapeHtml(image.parameters || "No deterministic conversion is available for this image.")}</pre>
      </label>
      <div class="metadata-preview-actions">
        <a class="secondary-link" href="${mediaUrl(image.local_path)}" target="_blank" rel="noreferrer">Open image</a>
        <button class="primary-button" id="convertMetadataImage" type="button" ${canConvert ? "" : "disabled"}>${image.status === "compliant" ? "Already Civitai ready" : "Convert this image"}</button>
      </div>`;
    $("#convertMetadataImage")?.addEventListener("click", () => convertMetadataAsset(image.id));
  } catch (error) {
    $("#metadataPreview").innerHTML = `<div class="library-empty"><strong>Could not read this image</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function convertMetadataAsset(assetId) {
  const button = $("#convertMetadataImage");
  if (button) {
    button.disabled = true;
    button.textContent = "Converting metadata…";
  }
  try {
    await api(`/api/postprocess/assets/${assetId}`, { method: "POST", body: "{}" });
    toast("Civitai metadata written. Image pixels and ComfyUI data were preserved.");
    await openPostprocessBatch(state.selectedPostprocessBatchId);
    await loadMetadataPreview(assetId);
  } catch (error) {
    toast(error.message, true);
    if (button) {
      button.disabled = false;
      button.textContent = "Convert this image";
    }
  }
}

async function convertMetadataBatch() {
  const batchId = state.selectedPostprocessBatchId;
  if (!batchId || !confirm("Convert every ready PNG in this batch in place? Existing ComfyUI prompt/workflow metadata and image pixels will be preserved.")) return;
  const button = $("#convertMetadataBatch");
  button.disabled = true;
  button.textContent = "Converting batch…";
  setMessage("#postprocessMessage", "Writing deterministic Civitai metadata to ready PNGs…");
  try {
    const payload = await api(`/api/postprocess/batches/${batchId}`, { method: "POST", body: "{}" });
    const counts = payload.counts || {};
    const message = `${counts.converted || 0} converted · ${counts.already_compliant || 0} already ready · ${counts.errors || 0} errors`;
    setMessage("#postprocessMessage", message, counts.errors ? "error" : "success");
    toast(message, Boolean(counts.errors));
    await openPostprocessBatch(batchId);
  } catch (error) {
    setMessage("#postprocessMessage", error.message, "error");
    toast(error.message, true);
  } finally {
    button.textContent = "Convert ready images";
  }
}

function closeImageLightbox() {
  const dialog = $("#imageLightboxDialog");
  if (dialog?.open) dialog.close();
  document.body.classList.remove("has-modal");
  state.selectedWallIndex = null;
}

function showImageLightbox(index) {
  const item = state.wallItems[index];
  const dialog = $("#imageLightboxDialog");
  if (!item || !dialog) return;
  state.selectedWallIndex = index;
  const url = mediaUrl(item.asset.local_path);
  const geometry = assetGeometry(item.asset);
  dialog.innerHTML = `
    <div class="image-lightbox-shell">
      <button class="image-lightbox-close" type="button" aria-label="Close large image">Close</button>
      <img src="${url}" width="${geometry.width}" height="${geometry.height}" alt="Generated output ${escapeHtml(item.asset.filename)}">
      <div class="image-lightbox-controls">
        <button type="button" id="previousWallImage" ${index === 0 ? "disabled" : ""}>Previous</button>
        <span>${index + 1} / ${state.wallItems.length}</span>
        ${civitaiTileBadge(item.asset)}
        <button type="button" id="nextWallImage" ${index >= state.wallItems.length - 1 ? "disabled" : ""}>Next</button>
      </div>
    </div>`;
  dialog.querySelector(".image-lightbox-close").addEventListener("click", closeImageLightbox);
  dialog.querySelector("#previousWallImage")?.addEventListener("click", () => showImageLightbox(index - 1));
  dialog.querySelector("#nextWallImage")?.addEventListener("click", () => showImageLightbox(index + 1));
  if (!dialog.open) dialog.showModal();
  document.body.classList.add("has-modal");
}

function closeImageDetail() {
  const dialog = $("#imageDetailDialog");
  if (dialog?.open) dialog.close();
  document.body.classList.remove("has-modal");
  state.selectedGalleryIndex = null;
}

function galleryImageFamily(item) {
  if (!item) return [];
  const assets = (item.prompt.assets || []).filter((asset) => ["images", "gifs"].includes(asset.kind));
  const byId = new Map(assets.map((asset) => [asset.id, asset]));
  let root = byId.get(item.asset.id) || item.asset;
  const ancestors = new Set([root.id]);
  while (true) {
    const parentId = root.source?.vram_relay?.source_asset_id;
    const parent = parentId ? byId.get(parentId) : null;
    if (!parent || ancestors.has(parent.id)) break;
    ancestors.add(parent.id);
    root = parent;
  }
  const children = new Map();
  for (const asset of assets) {
    const parentId = asset.source?.vram_relay?.source_asset_id;
    if (!parentId || !byId.has(parentId)) continue;
    const group = children.get(parentId) || [];
    group.push(asset);
    children.set(parentId, group);
  }
  const orderedAssets = [];
  const visited = new Set();
  const appendBranch = (asset) => {
    if (!asset || visited.has(asset.id)) return;
    visited.add(asset.id);
    orderedAssets.push(asset);
    (children.get(asset.id) || []).forEach(appendBranch);
  };
  appendBranch(root);
  const galleryByAsset = new Map(state.galleryItems.map((galleryItem, index) => [galleryItem.asset.id, { ...galleryItem, galleryIndex: index }]));
  return orderedAssets.map((asset) => galleryByAsset.get(asset.id)).filter(Boolean);
}

function galleryVersionLabel(asset) {
  const kind = asset.source?.vram_relay?.image_tool_kind;
  if (kind === "edit") return "Edited image";
  if (kind === "upscale") return "Upscaled image";
  return "Original image";
}

async function runInpaint(index, overrides = {}) {
  const item = state.galleryItems[index];
  if (!item) return;
  const instruction = (overrides.instruction ?? "").trim() || "";
  const maskDataUrl = overrides.mask_data_url || "";
  const denoise = overrides.denoise ?? 0.75;
  const workflowId = overrides.workflow_id || "";
  if (!workflowId) { toast("Choose an edit workflow first.", true); return; }
  if (!instruction) { toast("Enter an edit instruction for the masked area.", true); return; }
  if (!maskDataUrl) { toast("Paint a mask on the image first.", true); return; }
  toast("Sending masked image to ComfyUI for inpainting…");
  try {
    const payload = await api(`/api/assets/${item.asset.id}/inpaint`, {
      method: "POST",
      body: JSON.stringify({ workflow_id: workflowId, instruction, mask_data_url: maskDataUrl, denoise: Number(denoise) }),
    });
    state.imageToolWorkflows = payload.image_tool_workflows || state.imageToolWorkflows;
    const refreshed = await api(`/api/batches/${item.asset.batch_id}`);
    renderGalleryBatch(refreshed.batch);
    const newest = payload.images?.at(-1);
    const resultIndex = newest ? state.galleryItems.findIndex((gi) => gi.asset.id === newest.id) : -1;
    const sourceIndex = state.galleryItems.findIndex((gi) => gi.asset.id === item.asset.id);
    showImageDetail(resultIndex >= 0 ? resultIndex : sourceIndex);
    toast("Inpainting complete. Use Previous version / Next version to compare.");
  } catch (error) { toast(error.message, true); }
}

function openMaskEditor(item) {
  const asset = item.asset;
  const url = mediaUrl(asset.local_path);
  const geometry = assetGeometry(asset);
  const overlay = document.createElement("div");
  overlay.className = "mask-editor-overlay";
  overlay.innerHTML = `
    <div class="mask-editor-toolbar">
      <div class="mask-editor-toolbar-left">
        <button class="secondary-button" id="maskUndo" type="button">Undo</button>
        <button class="secondary-button" id="maskClear" type="button">Clear</button>
        <button class="secondary-button" id="maskEraseMode" type="button">Erase: OFF</button>
        <label class="mask-brush-label">Brush <input id="maskBrushSize" type="range" min="2" max="150" value="30"><span id="maskBrushLabel">30 px</span></label>
      </div>
      <div class="mask-editor-toolbar-center">
        <span class="mask-editor-hint">Left-click = paint mask · Right-click = erase</span>
      </div>
      <div class="mask-editor-toolbar-right">
        <button class="secondary-button" id="maskClose" type="button">Close</button>
      </div>
    </div>
    <div class="mask-editor-canvas-area" id="maskCanvasArea">
      <div class="mask-editor-loading">Loading image…</div>
    </div>
    <div class="mask-editor-bottom">
      <div class="mask-editor-bottom-fields">
        <label><span>Edit instruction</span><textarea id="maskInstruction" rows="1" placeholder="remove the watermark, fix her hair"></textarea></label>
        <label><span>Denoise</span><input id="maskDenoise" type="range" min="0.1" max="1" step="0.05" value="0.75"><span id="maskDenoiseLabel">0.75</span></label>
        <div>${imageToolSelectMarkup("edit", "maskWorkflow")}</div>
        <button class="primary-button" id="maskRunInpaint" type="button" ${state.imageToolWorkflows.some((e) => e.kind === "edit") ? "" : "disabled"}>Inpaint masked area</button>
      </div>
      <p class="form-message" id="maskMessage" role="status"></p>
    </div>`;
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add("open"));
  const maskCanvas = document.createElement("canvas");
  maskCanvas.width = geometry.width;
  maskCanvas.height = geometry.height;
  const maskCtx = maskCanvas.getContext("2d");
  maskCtx.fillStyle = "black";
  maskCtx.fillRect(0, 0, maskCanvas.width, maskCanvas.height);
  const maskEditor = {
    maskCanvas, maskCtx, brushSize: 30, isDrawing: false, history: [],
    saveState() { this.history.push(this.maskCtx.getImageData(0, 0, this.maskCanvas.width, this.maskCanvas.height)); if (this.history.length > 30) this.history.shift(); },
    undo() { if (this.history.length) this.maskCtx.putImageData(this.history.pop(), 0, 0); },
    clear() { this.saveState(); this.maskCtx.fillStyle = "black"; this.maskCtx.fillRect(0, 0, this.maskCanvas.width, this.maskCanvas.height); },
    draw(x, y) { this.maskCtx.globalCompositeOperation = "source-over"; this.maskCtx.fillStyle = "white"; this.maskCtx.beginPath(); this.maskCtx.arc(x, y, this.brushSize / 2, 0, Math.PI * 2); this.maskCtx.fill(); },
    erase(x, y) { this.maskCtx.globalCompositeOperation = "source-over"; this.maskCtx.fillStyle = "black"; this.maskCtx.beginPath(); this.maskCtx.arc(x, y, this.brushSize / 2, 0, Math.PI * 2); this.maskCtx.fill(); },
    getDataURL() { return this.maskCanvas.toDataURL("image/png"); },
    hasMask() { const d = this.maskCtx.getImageData(0, 0, this.maskCanvas.width, this.maskCanvas.height).data; for (let i = 0; i < d.length; i += 4) { if (d[i] > 0) return true; } return false; },
  };
  let eraseMode = false;
  const area = overlay.querySelector("#maskCanvasArea");
  let overlayCanvas = null;
  const img = new Image();
  function syncOverlay() {
    if (!overlayCanvas) return;
    const rect = img.getBoundingClientRect();
    const areaRect = area.getBoundingClientRect();
    overlayCanvas.style.left = `${rect.left - areaRect.left}px`;
    overlayCanvas.style.top = `${rect.top - areaRect.top}px`;
    overlayCanvas.style.width = `${rect.width}px`;
    overlayCanvas.style.height = `${rect.height}px`;
  }
  function redrawOverlay() {
    if (!overlayCanvas) {
      overlayCanvas = document.createElement("canvas");
      overlayCanvas.className = "inpaint-overlay-canvas";
      area.appendChild(overlayCanvas);
    }
    overlayCanvas.width = maskCanvas.width;
    overlayCanvas.height = maskCanvas.height;
    const ctx = overlayCanvas.getContext("2d");
    ctx.drawImage(maskCanvas, 0, 0);
    const imgData = ctx.getImageData(0, 0, overlayCanvas.width, overlayCanvas.height);
    const d = imgData.data;
    for (let i = 0; i < d.length; i += 4) { if (d[i + 3] > 0) { d[i] = 255; d[i + 1] = 50; d[i + 2] = 80; d[i + 3] = 120; } }
    ctx.putImageData(imgData, 0, 0);
    syncOverlay();
  }
  img.onload = () => {
    area.innerHTML = "";
    img.style.maxWidth = "100%";
    img.style.maxHeight = "100%";
    img.style.display = "block";
    img.style.margin = "auto";
    img.style.cursor = "crosshair";
    img.className = "inpaint-source-image";
    area.appendChild(img);
    redrawOverlay();
  };
  img.crossOrigin = "anonymous";
  img.src = url;
  function getCoords(event) {
    const rect = area.querySelector(".inpaint-source-image")?.getBoundingClientRect();
    if (!rect) return null;
    return { x: (event.clientX - rect.left) / rect.width * maskCanvas.width, y: (event.clientY - rect.top) / rect.height * maskCanvas.height };
  }
  area.addEventListener("mousedown", (e) => { e.preventDefault(); maskEditor.saveState(); maskEditor.isDrawing = true; const c = getCoords(e); if (c) { const erasing = eraseMode || (e.buttons & 2) === 2; if (erasing) maskEditor.erase(c.x, c.y); else maskEditor.draw(c.x, c.y); redrawOverlay(); } });
  area.addEventListener("mousemove", (e) => { if (!maskEditor.isDrawing) return; const c = getCoords(e); if (c) { const erasing = eraseMode || (e.buttons & 2) === 2; if (erasing) maskEditor.erase(c.x, c.y); else maskEditor.draw(c.x, c.y); redrawOverlay(); } });
  area.addEventListener("mouseup", () => { maskEditor.isDrawing = false; });
  area.addEventListener("mouseleave", () => { maskEditor.isDrawing = false; });
  area.addEventListener("contextmenu", (e) => { e.preventDefault(); });
  overlay.querySelector("#maskBrushSize")?.addEventListener("input", (e) => { maskEditor.brushSize = Number(e.target.value); const l = overlay.querySelector("#maskBrushLabel"); if (l) l.textContent = `${e.target.value} px`; });
  overlay.querySelector("#maskDenoise")?.addEventListener("input", (e) => { const l = overlay.querySelector("#maskDenoiseLabel"); if (l) l.textContent = e.target.value; });
  overlay.querySelector("#maskUndo")?.addEventListener("click", () => { maskEditor.undo(); redrawOverlay(); });
  overlay.querySelector("#maskClear")?.addEventListener("click", () => { maskEditor.clear(); redrawOverlay(); });
  overlay.querySelector("#maskEraseMode")?.addEventListener("click", () => { eraseMode = !eraseMode; const b = overlay.querySelector("#maskEraseMode"); if (b) b.textContent = `Erase: ${eraseMode ? "ON" : "OFF"}`; });
  overlay.querySelector("#maskClose")?.addEventListener("click", () => { overlay.classList.remove("open"); setTimeout(() => overlay.remove(), 200); });
  overlay.querySelector("#maskRunInpaint")?.addEventListener("click", () => {
    if (!maskEditor.hasMask()) { toast("Paint a mask on the image first.", true); return; }
    const idx = state.galleryItems.findIndex((gi) => gi.asset.id === asset.id);
    if (idx >= 0) {
      overlay.classList.remove("open");
      setTimeout(() => overlay.remove(), 200);
      runInpaint(idx, {
        mask_data_url: maskEditor.getDataURL(),
        instruction: overlay.querySelector("#maskInstruction")?.value || "",
        denoise: parseFloat(overlay.querySelector("#maskDenoise")?.value || "0.75"),
        workflow_id: overlay.querySelector("#maskWorkflow")?.value || "",
      });
    }
  });
}

function showImageDetail(index) {
  const item = state.galleryItems[index];
  const dialog = $("#imageDetailDialog");
  if (!item || !dialog) return;
  const { asset, prompt } = item;
  const url = mediaUrl(asset.local_path);
  const geometry = assetGeometry(asset);
  const family = galleryImageFamily(item);
  const familyIndex = Math.max(0, family.findIndex((familyItem) => familyItem.asset.id === asset.id));
  const versionLabel = galleryVersionLabel(asset);
  const provenance = asset.source?.vram_relay || {};
  state.selectedGalleryIndex = index;
  dialog.innerHTML = `
    <div class="image-detail-shell">
      <header class="image-detail-head">
        <div><span>IMAGE ${index + 1} OF ${state.galleryItems.length}</span><h3>#${prompt.position} - ${escapeHtml(prompt.guide || "Imported prompt")}</h3></div>
        <button class="image-detail-close" type="button" aria-label="Close image details">Close</button>
      </header>
      <div class="image-detail-layout">
        <div class="image-detail-media">
          <img src="${url}" alt="Generated output ${escapeHtml(asset.filename)}">
          <div class="image-detail-version-navigation" aria-label="Original and edited image navigation">
            <button class="secondary-button" id="previousImageVersion" type="button" ${familyIndex === 0 ? "disabled" : ""}>‹ Previous version</button>
            <span><strong>Original + edits</strong><b>${escapeHtml(versionLabel)} · ${familyIndex + 1} / ${family.length}</b></span>
            <button class="secondary-button" id="nextImageVersion" type="button" ${familyIndex >= family.length - 1 ? "disabled" : ""}>Next version ›</button>
          </div>
          <div class="image-detail-navigation">
            <button class="secondary-button" id="previousGalleryImage" type="button" ${index === 0 ? "disabled" : ""}>Previous gallery image</button>
            <a class="secondary-link" href="${url}" target="_blank" rel="noreferrer">Open full image</a>
            <button class="secondary-button" id="nextGalleryImage" type="button" ${index >= state.galleryItems.length - 1 ? "disabled" : ""}>Next gallery image</button>
          </div>
        </div>
        <aside class="image-detail-copy">
          <div class="image-detail-facts">
            <span><strong>Workflow</strong>${escapeHtml(assetWorkflowLabel(asset))}</span>
            <span><strong>Run</strong>${prompt.completed_runs}/${prompt.target_runs}</span>
            <span><strong>Seed</strong>${escapeHtml(prompt.seed || "-")}</span>
            <span><strong>File</strong>${escapeHtml(asset.filename)}</span>
            <span><strong>Dimensions</strong><b id="detailDimensions">${asset.width && asset.height ? `${geometry.width} × ${geometry.height} px` : "Reading image…"}</b></span>
            <span><strong>Version</strong>${escapeHtml(versionLabel)} · ${familyIndex + 1}/${family.length}${provenance.edit_instruction ? `<br>${escapeHtml(provenance.edit_instruction)}` : ""}</span>
          </div>
          <div class="detail-rating"><span>Your rating</span>${starRatingMarkup(item, index)}</div>
          <div class="image-detail-prompt"><h4>Prompt</h4><p>${escapeHtml(prompt.prompt)}</p></div>
          ${prompt.negative_prompt ? `<details><summary>Negative prompt</summary><p>${escapeHtml(prompt.negative_prompt)}</p></details>` : ""}
          ${prompt.error ? `<details open><summary>Generation error</summary><p>${escapeHtml(prompt.error)}</p></details>` : ""}
          <section class="image-tool-actions">
            <div><h4>Edit or upscale this image</h4><p>WildCat Export Edition sends this saved image into the chosen ComfyUI API, archives the result here, then unloads ComfyUI.</p></div>
            <div class="image-tool-action-grid">
              <div>${imageToolSelectMarkup("edit", "detailEditWorkflow")}<label><span>Short edit instruction</span><textarea id="detailEditInstruction" rows="2" placeholder="remove water, or change her hair color"></textarea></label><button class="secondary-button" id="runImageEdit" type="button" ${state.imageToolWorkflows.some((entry) => entry.kind === "edit") ? "" : "disabled"}>Send for editing</button></div>
              <div>${imageToolSelectMarkup("upscale", "detailUpscaleWorkflow")}<button class="secondary-button" id="runImageUpscale" type="button" ${state.imageToolWorkflows.some((entry) => entry.kind === "upscale") ? "" : "disabled"}>Send for upscale</button></div>
            </div>
            <p class="form-message" id="imageToolRunMessage" role="status"></p>
          </section>
          <div class="inpaint-tool-actions">
            <button class="secondary-button" id="openMaskEditor" type="button">Open mask inpainting editor</button>
            <p class="inpaint-hint">Paint over the area you want to change. Opens a full-screen editor with brush controls.</p>
          </div>
          <div class="civitai-detail-row ${asset.civitai_posted ? "is-posted" : ""}">
            <span class="civitai-badge" title="${asset.civitai_posted ? "Posted to Civitai" : "Not posted to Civitai"}">${asset.civitai_posted ? "✓ CIVITAI" : "NOT POSTED"}</span>
            ${asset.civitai_posted && asset.civitai_url ? `<a class="civitai-link" href="${escapeHtml(asset.civitai_url)}" target="_blank" rel="noreferrer">Open post</a>` : ""}
            <button class="secondary-button" id="civitaiPost" type="button">${asset.civitai_posted ? "Add again to post queue" : "Add to post queue"}</button>
            <button class="secondary-button" id="civitaiMark" type="button">${asset.civitai_posted ? "Remove mark" : "Mark posted manually"}</button>
          </div>
          <div class="image-detail-actions">
            <button class="secondary-button" id="copyDetailPrompt" type="button">Copy prompt</button>
            <button class="primary-button" id="askDetailImage" type="button">Ask about this image</button>
            <button class="danger-button image-detail-delete" id="deleteDetailImage" type="button">Recycle this image</button>
          </div>
        </aside>
      </div>
    </div>`;
  dialog.querySelector(".image-detail-close").addEventListener("click", closeImageDetail);
  dialog.querySelector("#previousImageVersion")?.addEventListener("click", () => showImageDetail(family[familyIndex - 1].galleryIndex));
  dialog.querySelector("#nextImageVersion")?.addEventListener("click", () => showImageDetail(family[familyIndex + 1].galleryIndex));
  dialog.querySelector("#previousGalleryImage")?.addEventListener("click", () => showImageDetail(index - 1));
  dialog.querySelector("#nextGalleryImage")?.addEventListener("click", () => showImageDetail(index + 1));
  dialog.querySelector("#copyDetailPrompt").addEventListener("click", () => copyText(promptText(prompt), `Prompt #${prompt.position} copied.`));
  dialog.querySelector("#runImageEdit")?.addEventListener("click", () => runImageTool(index, "edit"));
  dialog.querySelector("#runImageUpscale")?.addEventListener("click", () => runImageTool(index, "upscale"));
  dialog.querySelector("#deleteDetailImage")?.addEventListener("click", () => deleteGalleryImage(index));
  dialog.querySelector("#openMaskEditor")?.addEventListener("click", () => openMaskEditor(item));
  dialog.querySelector("#civitaiPost")?.addEventListener("click", () => postAssetToCivitai(index));
  dialog.querySelector("#civitaiMark")?.addEventListener("click", () => markCivitaiPosted(index, !item.asset.civitai_posted));
  bindRatingButtons(dialog);
  const detailImage = dialog.querySelector(".image-detail-media > img");
  const updateDimensions = () => {
    const dimensions = dialog.querySelector("#detailDimensions");
    if (dimensions && detailImage.naturalWidth) dimensions.textContent = `${detailImage.naturalWidth} × ${detailImage.naturalHeight} px`;
  };
  detailImage.addEventListener("load", updateDimensions);
  if (detailImage.complete) updateDimensions();
  dialog.querySelector("#askDetailImage").addEventListener("click", () => {
    state.askPromptId = prompt.id;
    state.selectedBatchId = state.currentBatch.id;
    closeImageDetail();
    switchTab("library");
    renderBatchDetail(state.currentBatch);
    $("#qaQuestion")?.focus();
    $("#qaQuestion")?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  if (!dialog.open) dialog.showModal();
  document.body.classList.add("has-modal");
}

async function runImageTool(index, kind) {
  const item = state.galleryItems[index];
  const select = kind === "upscale" ? $("#detailUpscaleWorkflow") : $("#detailEditWorkflow");
  const button = kind === "upscale" ? $("#runImageUpscale") : $("#runImageEdit");
  const workflowId = select?.value || "";
  const instruction = kind === "edit" ? ($("#detailEditInstruction")?.value || "").trim() : "";
  if (!item || !workflowId || !button) return;
  if (kind === "edit" && !instruction) {
    setMessage("#imageToolRunMessage", "Enter a short edit instruction first.", "error");
    $("#detailEditInstruction")?.focus();
    return;
  }
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = kind === "upscale" ? "Upscaling in ComfyUI…" : "Editing in ComfyUI…";
  setMessage("#imageToolRunMessage", "Waiting for the single GPU lane, running ComfyUI, and archiving the result…");
  try {
    const payload = await api(`/api/assets/${item.asset.id}/image-tool`, {
      method: "POST",
      body: JSON.stringify({ workflow_id: workflowId, instruction }),
    });
    state.imageToolWorkflows = payload.image_tool_workflows || state.imageToolWorkflows;
    const refreshed = await api(`/api/batches/${item.asset.batch_id}`);
    renderGalleryBatch(refreshed.batch);
    const newest = payload.images?.at(-1);
    const resultIndex = newest ? state.galleryItems.findIndex((galleryItem) => galleryItem.asset.id === newest.id) : -1;
    const sourceIndex = state.galleryItems.findIndex((galleryItem) => galleryItem.asset.id === item.asset.id);
    showImageDetail(resultIndex >= 0 ? resultIndex : sourceIndex);
    toast(`${imageToolLabel(kind)} complete. Use Previous version and Next version below the image to compare.`);
  } catch (error) {
    setMessage("#imageToolRunMessage", error.message, "error");
    toast(error.message, true);
    button.disabled = false;
    button.textContent = originalText;
  }
}

function questionMarkup(item) {
  return `<div class="qa-item"><strong>${escapeHtml(item.question)}</strong><p>${escapeHtml(item.answer)}</p></div>`;
}

function comparisonMarkup(item) {
  const guideSummary = Object.entries(item.guide_samples || {}).map(([guide, files]) => `${guide}: ${files.length}`).join(" · ");
  return `<article class="comparison-result">
    <div><strong>${escapeHtml(item.model)}</strong><span>${escapeHtml(guideSummary)} · ${new Date(item.created_at).toLocaleString()}</span></div>
    ${item.focus ? `<p class="comparison-focus">Focus: ${escapeHtml(item.focus)}</p>` : ""}
    <pre>${escapeHtml(item.result)}</pre>
  </article>`;
}

function renderBatchDetail(batch) {
  state.currentBatch = batch;
  const canResume = ["queued", "interrupted", "failed", "complete_with_errors", "cancelled"].includes(batch.status);
  const canCancel = ["queued", "interrupted", "failed", "paused"].includes(batch.status);
  const canRerun = Boolean(batch.prompts?.length) && !["queued", "running", "pausing", "paused"].includes(batch.status);
  const canQueue = !["queued", "running", "pausing", "paused"].includes(batch.status) && Boolean(batch.prompts?.length);
  const assetCount = (batch.prompts || []).reduce((total, prompt) => total + (prompt.assets || []).length, 0);
  const unpostedCount = (batch.prompts || []).reduce((total, prompt) => total + (prompt.assets || []).filter((a) => !a.civitai_posted).length, 0);
  const referenceCount = Number(batch.request?.reference_count || 0);
  const plannedPromptCount = Number(batch.request?.effective_prompts_per_guide || 0) * (batch.request?.guides?.length || 0);
  const canRegeneratePrompts = !["queued", "running", "pausing", "paused", "regenerating_prompt"].includes(batch.status);
  const promptRecords = (batch.prompts || []).map((prompt) => `
    <article class="prompt-record ${state.askPromptId === prompt.id ? "is-selected" : ""}" data-prompt-record="${prompt.id}">
      <div class="prompt-record-head"><strong>#${prompt.position} · ${escapeHtml(prompt.guide || "Imported prompt")}</strong><span>${prompt.completed_runs}/${prompt.target_runs} runs · seed ${prompt.seed || "—"}</span></div>
      <div class="record-actions prompt-record-actions">
        ${canRegeneratePrompts ? `<button class="primary-button" type="button" data-regenerate-library-prompt="${prompt.id}">Regenerate prompt</button>` : ""}
        <button class="secondary-button" type="button" data-copy-prompt="${prompt.id}">Copy prompt</button>
        <button class="secondary-button" type="button" data-ask-prompt="${prompt.id}">Ask about this ${prompt.assets?.length ? "image" : "prompt"}</button>
      </div>
      ${prompt.reference_name ? `<div class="prompt-reference">Input reference: ${escapeHtml(prompt.reference_name)} · processed separately</div>` : ""}
      ${assetCarouselMarkup(prompt)}
      <div class="prompt-copy"><p>${escapeHtml(prompt.prompt)}</p>
        ${prompt.original_prompt ? `<details><summary>Original prompt before regeneration (${prompt.regeneration_count || 1} rewrite${Number(prompt.regeneration_count || 1) === 1 ? "" : "s"})</summary><p>${escapeHtml(prompt.original_prompt)}</p></details>` : ""}
        ${prompt.negative_prompt ? `<details><summary>Negative prompt</summary><p>${escapeHtml(prompt.negative_prompt)}</p></details>` : ""}
        ${prompt.error ? `<details open><summary>Generation error</summary><p>${escapeHtml(prompt.error)}</p></details>` : ""}
      </div>
    </article>`).join("");
  const selected = (batch.prompts || []).find((prompt) => prompt.id === state.askPromptId);
  const targetText = selected ? `Prompt #${selected.position} · ${selected.guide || "imported"}` : "Whole batch";
  const guidesWithImages = new Set((batch.prompts || []).filter((prompt) => prompt.guide && prompt.assets?.some((asset) => asset.kind === "images")).map((prompt) => prompt.guide));
  const canCompare = ["guides", "pbi"].includes(batch.source) && guidesWithImages.size >= 2;
  const resumeLabel = batch.status === "queued" ? "Start queued batch" : "Resume unfinished";
  $("#libraryDetail").innerHTML = `
    <div class="detail-head">
      <div><p class="step-label">${escapeHtml(batch.status.replaceAll("_", " "))}</p><h2>${escapeHtml(batch.title)}</h2><p>${escapeHtml(batch.brief)} · ${batch.total_prompts || plannedPromptCount} ${batch.total_prompts ? "prompts" : "prompts planned"}${referenceCount ? ` · ${referenceCount} temporary references` : ""} · ${batch.completed_runs}/${batch.total_runs} runs</p></div>
      <div class="detail-actions">
        ${canRerun ? '<button class="primary-button" id="rerunBatch" type="button">Run batch again</button>' : ""}
        ${canQueue ? '<button class="secondary-button" id="queueBatch" type="button">Queue without starting</button>' : ""}
        <button class="secondary-button" id="renameBatch" type="button">Rename batch</button>
        ${state.config?.civitai_has_api_token && unpostedCount ? `<button class="secondary-button" id="postBatchCivitai" type="button">Add ${unpostedCount} unposted to Civitai queue</button>` : ""}
        ${batch.prompts?.length ? '<button class="secondary-button" id="copyAllPrompts" type="button">Copy all prompts</button>' : ""}
        ${assetCount ? '<button class="secondary-button" id="openBatchFolder" type="button">Open image folder</button>' : ""}
        ${canResume ? `<button class="secondary-button" id="detailResume" type="button">${resumeLabel}</button>` : ""}
        ${canCancel ? '<button class="danger-button" id="detailCancel" type="button">Cancel job</button>' : ""}
        ${assetCount ? `<button class="danger-button" id="deleteBatchImages" type="button">Delete all ${assetCount} images</button>` : ""}
        <button class="danger-button" id="deleteBatch" type="button">Delete entire job</button>
      </div>
    </div>
    <div class="prompt-gallery">${promptRecords || '<p class="empty-note">Prompts have not been created yet.</p>'}</div>
    <section class="qa-panel">
      <h3>Ask prompt backend later</h3>
      <div class="qa-target">Context: <strong id="qaTarget">${escapeHtml(targetText)}</strong> · ComfyUI will be freed first; prompt backend unloads after the answer.</div>
      <div class="qa-controls">
        <label><span>Question</span><textarea id="qaQuestion" placeholder="What did I make here? How could I improve the composition? Which prompt details mattered most?"></textarea></label>
        <div>
          <label><span>Q&A model</span><select id="qaModel">${modelOptions(state.config.qa_model || state.config.prompt_model || "")}</select></label>
          <label class="check-row"><input id="qaIncludeImage" type="checkbox" ${selected?.assets?.length ? "checked" : ""} ${selected?.assets?.length ? "" : "disabled"}><span>Send selected image to a vision model</span></label>
        </div>
        <button class="primary-button" id="askButton" type="button">Ask</button>
      </div>
      <p class="form-message" id="qaMessage"></p>
      <div class="qa-history">${(batch.questions || []).map(questionMarkup).join("")}</div>
    </section>`;

  if (canCompare) {
    $("#libraryDetail").insertAdjacentHTML("beforeend", `
      <details class="compare-panel">
        <summary><span><strong>Compare markdown-guide outputs</strong><small>Optional — nothing is judged unless you run it.</small></span></summary>
        <div class="compare-body">
          <p>WildCat Export Edition samples outputs from each of the ${guidesWithImages.size} guides, asks a vision model to rank consistency and quality, saves the result, then unloads prompt backend.</p>
          <div class="compare-controls">
            <label><span>Vision model</span><select id="compareModel">${visionModelOptions(state.config.comparison_model || state.config.qa_model || "")}</select></label>
            <label><span>Samples per guide</span><select id="compareSamples"><option value="1">1</option><option value="2" selected>2</option><option value="3">3</option><option value="4">4</option></select></label>
          </div>
          <label><span>What matters most? <small>optional</small></span><textarea id="compareFocus" rows="3" placeholder="For example: realism, clothing detail, consistent faces, dynamic composition…"></textarea></label>
          <button class="primary-button" id="compareButton" type="button">Compare guide outputs</button>
          <p class="form-message" id="compareMessage"></p>
          <div class="comparison-history">${(batch.comparisons || []).map(comparisonMarkup).join("")}</div>
        </div>
      </details>`);
  }

  $$('[data-ask-prompt]').forEach((button) => button.addEventListener("click", () => {
    state.askPromptId = button.dataset.askPrompt;
    renderBatchDetail(batch);
    $("#qaQuestion").focus();
  }));
  bindLibraryCarousels($("#libraryDetail"));
  $$('[data-copy-prompt]').forEach((button) => button.addEventListener("click", () => {
    const prompt = batch.prompts.find((item) => item.id === button.dataset.copyPrompt);
    if (prompt) copyText(promptText(prompt), `Prompt #${prompt.position} copied.`);
  }));
  $$('[data-regenerate-library-prompt]').forEach((button) => button.addEventListener("click", () => {
    const prompt = batch.prompts.find((item) => item.id === button.dataset.regenerateLibraryPrompt);
    if (prompt) regenerateLibraryPrompt(batch, prompt, button);
  }));
  $("#copyAllPrompts")?.addEventListener("click", () => {
    const text = batch.prompts.map((prompt) => `${prompt.position}. ${promptText(prompt)}`).join("\n\n");
    copyText(text, `${batch.prompts.length} prompts copied.`);
  });
  $("#openBatchFolder")?.addEventListener("click", () => openBatchFolder(batch.id));
  $("#rerunBatch")?.addEventListener("click", () => rerunBatch(batch.id));
  $("#queueBatch")?.addEventListener("click", () => controlBatch(batch.id, "queue"));
  $("#renameBatch")?.addEventListener("click", () => renameBatch(batch.id));
  $("#postBatchCivitai")?.addEventListener("click", () => postBatchToCivitai(batch.id));
  $("#askButton").addEventListener("click", () => askQuestion(batch.id));
  $("#deleteBatch").addEventListener("click", () => deleteBatch(batch.id));
  $("#deleteBatchImages")?.addEventListener("click", () => deleteBatchImages(batch.id));
  $("#detailCancel")?.addEventListener("click", () => confirm("Cancel this job? Finished images already saved will be kept.") && controlBatch(batch.id, "cancel"));
  $("#detailResume")?.addEventListener("click", () => controlBatch(batch.id, "resume"));
  $("#compareButton")?.addEventListener("click", () => compareGuides(batch.id));
}

function promptText(prompt) {
  return prompt.negative_prompt
    ? `${prompt.prompt}\nNegative Prompt: ${prompt.negative_prompt}`
    : prompt.prompt;
}

async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }
  toast(successMessage);
}

async function rerunBatch(batchId) {
  const batch = state.currentBatch;
  if (!batch || batch.id !== batchId) return;
  const prompts = batch.prompts || [];
  const failedCount = prompts.filter((p) => String(p.error || "").trim()).length;
  const unratedCount = prompts.filter((p) => !(p.assets || []).some((a) => Number(a.rating || 0) > 0)).length;
  if (!state.workflows.length) { toast("Upload a ComfyUI API workflow first (Connections & workflow tab).", true); return; }
  const overlay = document.createElement("div");
  overlay.className = "mask-editor-overlay rerun-overlay";
  const workflowOptions = state.workflows.map((w) => {
    const current = state.workflows.find((item) => item.id === (batch.request?.workflow_id || state.config.active_workflow_id));
    const selected = current && w.id === current.id ? "selected" : "";
    return `<option value="${escapeHtml(w.id)}" ${selected}>${escapeHtml(w.name)}</option>`;
  }).join("");
  overlay.innerHTML = `
    <div class="rerun-dialog">
      <h3>Rerun "${escapeHtml(batch.title)}"</h3>
      <p class="rerun-sub">Copies the prompts into a new job and runs them through the workflow you pick.</p>
      <label><span>ComfyUI API workflow</span><select id="rerunWorkflow">${workflowOptions}</select></label>
      <fieldset class="rerun-scope">
        <legend>Prompts to rerun</legend>
        <label class="check-row"><input type="radio" name="rerunScope" value="all" checked><span>All ${prompts.length} prompt${prompts.length === 1 ? "" : "s"}</span></label>
        <label class="check-row ${failedCount ? "" : "is-disabled"}"><input type="radio" name="rerunScope" value="errors" ${failedCount ? "" : "disabled"}><span>Only failed prompt${failedCount === 1 ? "" : "s"} (${failedCount})</span></label>
        <label class="check-row ${unratedCount ? "" : "is-hidden"}"><input type="radio" name="rerunScope" value="unrated"><span>Only unrated prompt${unratedCount === 1 ? "" : "s"} (${unratedCount})</span></label>
      </label>
      <p class="rerun-hint">Runs per prompt carry over from the original job. New images get fresh seeds and are archived alongside the originals.</p>
      <div class="rerun-actions">
        <button class="secondary-button" id="rerunCancel" type="button">Cancel</button>
        <button class="primary-button" id="rerunStart" type="button">Start rerun</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add("open"));
  const close = () => { overlay.classList.remove("open"); setTimeout(() => overlay.remove(), 200); };
  overlay.querySelector("#rerunCancel")?.addEventListener("click", close);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  overlay.querySelector("#rerunStart")?.addEventListener("click", async () => {
    const workflowId = overlay.querySelector("#rerunWorkflow")?.value || "";
    const scope = overlay.querySelector('input[name="rerunScope"]:checked')?.value || "all";
    close();
    try {
      toast("Queuing rerun…");
      const payload = await api(`/api/batches/${batchId}/rerun`, {
        method: "POST",
        body: JSON.stringify({ workflow_id: workflowId, selection: scope }),
      });
      state.activeBatchId = payload.active?.batch_id || payload.active_batch_id || null;
      switchTab("run");
      if (state.activeBatchId) startActivePolling();
      toast(payload.queued ? `Rerun added as queue #${payload.queue_position || "?"}.` : "Rerun started.");
      await refreshBatches();
    } catch (error) {
      toast(error.message, true);
    }
  });
}

async function regenerateLibraryPrompt(batch, prompt, button) {
  if (!confirm(
    `Regenerate prompt #${prompt.position} with prompt backend and queue its replacement for ComfyUI? `
    + "The original prompt and all existing images will be kept.",
  )) return;
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Regenerating…";
  try {
    const model = $("#qaModel")?.value || batch.request?.model || $("#promptModel")?.value || "";
    const payload = await api(`/api/batches/${batch.id}/prompts/${prompt.id}/regenerate`, {
      method: "POST",
      body: JSON.stringify({ model }),
    });
    state.activeBatchId = payload.active?.batch_id || payload.active_batch_id || batch.id;
    state.liveBatchId = state.activeBatchId;
    switchTab("run");
    startActivePolling();
    toast(`Prompt #${prompt.position} was regenerated and its replacement run started.`);
  } catch (error) {
    button.disabled = false;
    button.textContent = originalLabel;
    toast(error.message, true);
    await openBatch(batch.id);
  }
}

async function compareGuides(batchId) {
  const button = $("#compareButton");
  const model = $("#compareModel").value;
  button.disabled = true;
  button.textContent = "Comparing images…";
  setMessage("#compareMessage", "Freeing ComfyUI, loading the vision model, and examining the guide samples…");
  try {
    await api("/api/compare", { method: "POST", body: JSON.stringify({
      batch_id: batchId,
      model,
      samples_per_guide: Number($("#compareSamples").value),
      focus: $("#compareFocus").value.trim(),
    }) });
    state.config.comparison_model = model;
    toast("Guide comparison saved. prompt backend is unloaded again.");
    await openBatch(batchId);
  } catch (error) {
    setMessage("#compareMessage", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Compare guide outputs";
    refreshStatus();
  }
}

async function openBatch(batchId) {
  state.selectedBatchId = batchId;
  state.askPromptId = null;
  refreshBatches();
  $("#libraryDetail").innerHTML = '<div class="library-empty"><strong>Loading batch…</strong></div>';
  try {
    const payload = await api(`/api/batches/${batchId}`);
    renderBatchDetail(payload.batch);
  } catch (error) {
    $("#libraryDetail").innerHTML = `<div class="library-empty"><strong>Could not open batch</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function openBatchFolder(batchId) {
  const button = $("#openBatchFolder");
  if (button) button.disabled = true;
  try {
    await api(`/api/batches/${batchId}/open-folder`, { method: "POST", body: "{}" });
    toast("Opened this job's archived image folder in File Explorer.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function askQuestion(batchId) {
  const question = $("#qaQuestion").value.trim();
  const model = $("#qaModel").value;
  const button = $("#askButton");
  button.disabled = true;
  button.textContent = "Switching models…";
  setMessage("#qaMessage", "Freeing ComfyUI, loading prompt backend, and preparing the answer…");
  try {
    const payload = await api("/api/ask", { method: "POST", body: JSON.stringify({
      batch_id: batchId,
      prompt_id: state.askPromptId,
      question,
      model,
      include_image: $("#qaIncludeImage").checked,
    }) });
    state.config.qa_model = model;
    await api("/api/settings", { method: "POST", body: JSON.stringify({ qa_model: model }) });
    setMessage("#qaMessage", "Answer complete. prompt backend has been unloaded again.", "success");
    toast("Answer saved to the batch. prompt backend is unloaded.");
    await openBatch(batchId);
    return payload.answer;
  } catch (error) {
    setMessage("#qaMessage", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Ask";
    refreshStatus();
  }
}

async function deleteBatch(batchId) {
  if (!confirm("Delete this batch from the WildCat Export Edition library? This removes WildCat Export Edition’s saved copies, but not ComfyUI’s original output files.")) return;
  try {
    await api(`/api/batches/${batchId}`, { method: "DELETE" });
    state.selectedBatchId = null;
    $("#libraryDetail").innerHTML = '<div class="library-empty"><strong>Batch deleted.</strong><p>ComfyUI originals were left untouched.</p></div>';
    refreshBatches();
  } catch (error) { toast(error.message, true); }
}

async function deleteBatchImages(batchId) {
  if (!confirm("Delete every archived image from this job? The prompts, progress, events, and ComfyUI originals will be kept.")) return;
  try {
    const payload = await api(`/api/batches/${batchId}/images`, { method: "DELETE" });
    toast(`Deleted ${payload.deleted_images} archived images. Job history was kept.`);
    await openBatch(batchId);
    await refreshBatches();
  } catch (error) { toast(error.message, true); }
}

async function controlBatch(batchId, action) {
  try {
    const payload = await api(`/api/batches/${batchId}/${action}`, { method: "POST", body: "{}" });
    if (["resume", "run", "queue"].includes(action)) {
      if (payload.queued) {
        toast(`Added to queue${payload.queue_position ? ` as #${payload.queue_position}` : ""}.`);
      } else if (payload.active_batch_id) {
        state.activeBatchId = payload.active_batch_id;
        switchTab("run");
        startActivePolling();
      }
      await refreshBatches();
      if (state.selectedBatchId === batchId) await openBatch(batchId);
    } else if (action === "cancel") {
      state.activeBatchId = payload.active?.batch_id || null;
      await refreshBatches();
      if (state.selectedBatchId === batchId) await openBatch(batchId);
      else await showLatestBatch();
    } else {
      if (action === "pause") toast("Pause requested. The current ComfyUI run will retry when you resume.");
      refreshActive();
    }
  } catch (error) { toast(error.message, true); }
}

async function controlCurrentPrompt(action) {
  const batchId = state.activeBatchId || state.liveBatchId;
  if (!batchId) return;
  const confirmations = {
    skip: "Skip this entire prompt and continue with the next one? Any completed runs for it will be kept.",
    regenerate: "Interrupt this run, unload ComfyUI, rewrite the current prompt with prompt backend, then resume ComfyUI?",
  };
  if (confirmations[action] && !confirm(confirmations[action])) return;
  const buttons = [$("#retryPrompt"), $("#regeneratePrompt"), $("#skipPrompt")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const payload = await api(`/api/batches/${batchId}/current-prompt/${action}`, {
      method: "POST",
      body: JSON.stringify({ model: $("#promptModel").value || "" }),
    });
    const position = payload.current_prompt?.position || "?";
    const messages = {
      retry: `Prompt #${position} is being interrupted and retried with a new seed.`,
      skip: `Prompt #${position} is being skipped. The batch will continue.`,
      regenerate: `Prompt #${position} is switching from ComfyUI to prompt backend for a rewrite.`,
    };
    toast(messages[action]);
    await refreshActive();
  } catch (error) {
    toast(error.message, true);
    await refreshActive();
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const token = $("#lmToken").value;
  const changes = {
    lm_url: $("#lmUrl").value.trim(),
    lm_studio_exe: $("#lmStudioExe").value.trim(),
    lm_api_token: token || "__KEEP__",
    comfy_url: $("#comfyUrl").value.trim(),
    comfy_path: $("#comfyPath").value.trim(),
    lm_context_length: Number($("#contextLength").value) || 0,
    comfy_job_timeout_minutes: Number($("#comfyTimeout").value),
    auto_start_apps: $("#autoStartApps").checked,
  };
  try {
    const payload = await api("/api/settings", { method: "POST", body: JSON.stringify(changes) });
    state.config = payload.config;
    setMessage("#settingsMessage", "Connections saved.", "success");
    await Promise.all([loadModels(), loadGuides(), refreshStatus()]);
  } catch (error) { setMessage("#settingsMessage", error.message, "error"); }
}

async function uploadWorkflow(event) {
  const files = [...(event.target.files || [])];
  if (!files.length) return;
  setMessage("#mappingMessage", "Reading workflow…");
  try {
    let payload;
    for (const file of files) {
      payload = await api("/api/workflow", { method: "POST", body: JSON.stringify(await exportWorkflowFile(file)) });
    }
    state.workflows = payload.workflows || [];
    state.config.active_workflow_id = payload.active_workflow_id;
    state.config.workflow_name = payload.workflow_entry.name;
    state.config.workflow_positive_fields = payload.workflow_entry.positive_fields;
    state.config.workflow_negative_fields = payload.workflow_entry.negative_fields;
    renderWorkflowChoices(payload.active_workflow_id);
    renderWorkflow(payload.workflow);
    setMessage("#mappingMessage", "Workflow loaded. Confirm the field mapping below.", "success");
  } catch (error) {
    setMessage("#mappingMessage", error.message, "error");
    toast(error.message, true);
  } finally { event.target.value = ""; }
}

async function uploadImageToolWorkflows(event) {
  const files = [...(event.target.files || [])];
  if (!files.length) return;
  const kind = $("#imageToolKind").value;
  setMessage("#imageToolWorkflowMessage", `Loading ${imageToolLabel(kind).toLowerCase()} API workflow${files.length === 1 ? "" : "s"}…`);
  try {
    let payload;
    for (const file of files) {
      payload = await api("/api/image-tool-workflows", {
        method: "POST",
        body: JSON.stringify({ ...await exportWorkflowFile(file), kind }),
      });
    }
    state.imageToolWorkflows = payload.image_tool_workflows || [];
    renderImageToolWorkflows();
    setMessage("#imageToolWorkflowMessage", `${files.length} ${imageToolLabel(kind).toLowerCase()} API workflow${files.length === 1 ? "" : "s"} loaded.`, "success");
  } catch (error) {
    setMessage("#imageToolWorkflowMessage", error.message, "error");
    toast(error.message, true);
  } finally {
    event.target.value = "";
  }
}

async function deleteImageToolWorkflow(workflowId) {
  const entry = state.imageToolWorkflows.find((item) => item.id === workflowId);
  if (!entry || !confirm(`Delete the saved ${imageToolLabel(entry.kind).toLowerCase()} API workflow "${entry.name}"?`)) return;
  try {
    const payload = await api(`/api/image-tool-workflows/${workflowId}`, { method: "DELETE" });
    state.imageToolWorkflows = payload.image_tool_workflows || [];
    renderImageToolWorkflows();
    setMessage("#imageToolWorkflowMessage", `"${entry.name}" was deleted.`, "success");
  } catch (error) {
    setMessage("#imageToolWorkflowMessage", error.message, "error");
    toast(error.message, true);
  }
}

async function saveMapping(event) {
  event.preventDefault();
  const positive = $$('input[name="positive"]:checked').map((input) => input.value);
  const negative = $$('input[name="negative"]:checked').map((input) => input.value);
  const workflowId = $("#workflowLibrary").value;
  try {
    await api("/api/workflow/mapping", { method: "POST", body: JSON.stringify({ workflow_id: workflowId, positive_fields: positive, negative_fields: negative }) });
    state.config.workflow_positive_fields = positive;
    state.config.workflow_negative_fields = negative;
    const entry = state.workflows.find((item) => item.id === workflowId);
    if (entry) {
      entry.positive_fields = positive;
      entry.negative_fields = negative;
    }
    setMessage("#mappingMessage", "Mapping saved. This workflow is ready for batches.", "success");
  } catch (error) { setMessage("#mappingMessage", error.message, "error"); }
}

async function selectWorkflow() {
  const workflowId = $("#workflowLibrary").value;
  if (!workflowId) return;
  try {
    const payload = await api("/api/workflow/select", {
      method: "POST",
      body: JSON.stringify({ workflow_id: workflowId }),
    });
    state.workflows = payload.workflows || state.workflows;
    state.config.active_workflow_id = workflowId;
    state.config.workflow_name = payload.workflow_entry.name;
    state.config.workflow_positive_fields = payload.workflow_entry.positive_fields;
    state.config.workflow_negative_fields = payload.workflow_entry.negative_fields;
    renderWorkflowChoices(workflowId);
    renderWorkflow(payload.workflow);
    setMessage("#mappingMessage", "Workflow selected. Its field mapping is shown below.", "success");
  } catch (error) {
    setMessage("#mappingMessage", error.message, "error");
  }
}

async function deleteWorkflow() {
  const workflowId = $("#workflowLibrary").value;
  const entry = state.workflows.find((item) => item.id === workflowId);
  if (!entry) return;
  if (!confirm(`Delete the saved ComfyUI API workflow "${entry.name}"? Completed image records will keep its name, but future jobs cannot use it.`)) return;
  const button = $("#deleteWorkflow");
  button.disabled = true;
  button.textContent = "Deleting…";
  try {
    const payload = await api(`/api/workflows/${workflowId}`, { method: "DELETE" });
    state.workflows = payload.workflows || [];
    state.workflow = payload.workflow || null;
    state.config.active_workflow_id = payload.active_workflow_id || "";
    state.config.workflow_name = payload.workflow_entry?.name || "";
    state.config.workflow_positive_fields = payload.workflow_entry?.positive_fields || [];
    state.config.workflow_negative_fields = payload.workflow_entry?.negative_fields || [];
    renderWorkflowChoices(state.config.active_workflow_id);
    renderWorkflow(state.workflow);
    setMessage("#mappingMessage", `"${entry.name}" was deleted.`, "success");
    toast(`Deleted ComfyUI API workflow "${entry.name}".`);
  } catch (error) {
    setMessage("#mappingMessage", error.message, "error");
    toast(error.message, true);
  } finally {
    button.textContent = "Delete API";
    button.disabled = !state.workflows.length;
  }
}

async function safetyAction(path, working, done) {
  setMessage("#safetyMessage", working);
  try {
    const payload = await api(path, { method: "POST", body: "{}" });
    if (payload.errors?.length) throw new Error(payload.errors.join("; "));
    setMessage("#safetyMessage", done, "success");
    toast(done);
    refreshStatus();
  } catch (error) { setMessage("#safetyMessage", error.message, "error"); }
}

async function stopComfyNow() {
  const button = $("#stopComfyNow");
  button.disabled = true;
  button.textContent = "Stopping ComfyUI…";
  setMessage("#batchMessage", "Interrupting ComfyUI, cancelling the active relay job, and unloading ComfyUI memory…");
  try {
    const payload = await api("/api/actions/stop-comfy", { method: "POST", body: "{}" });
    if (payload.errors?.length) throw new Error(payload.errors.join("; "));
    setMessage("#batchMessage", "ComfyUI stopped and unloaded. Any active relay job was cancelled.", "success");
    toast("ComfyUI stopped and its models were unloaded.");
    await refreshStatus();
    if (state.liveBatchId) await showLatestBatch();
  } catch (error) {
    setMessage("#batchMessage", error.message, "error");
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Release GPU memory";
  }
}

async function exitComfyApp() {
  const button = $("#exitComfyApp");
  button.disabled = true;
  button.textContent = "Exiting ComfyUI…";
  setMessage("#batchMessage", "Cancelling the relay job and closing the configured ComfyUI application…");
  try {
    const payload = await api("/api/actions/exit-comfy", { method: "POST", body: "{}" });
    const message = payload.already_stopped
      ? "ComfyUI was already closed."
      : `ComfyUI exited (process ${payload.exited_processes.join(", ")}).`;
    setMessage("#batchMessage", message, "success");
    toast(message);
    await refreshStatus();
    if (state.liveBatchId) await showLatestBatch();
  } catch (error) {
    setMessage("#batchMessage", error.message, "error");
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Close ComfyUI";
  }
}

function bindEvents() {
  $$(".nav-tab").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
  $$('input[name="source"]').forEach((radio) => radio.addEventListener("change", () => {
    const guides = radio.value === "guides" && radio.checked;
    $("#guideSource").classList.toggle("is-hidden", !guides && $('input[name="source"]:checked').value !== "guides");
    $("#pasteSource").classList.toggle("is-hidden", $('input[name="source"]:checked').value !== "paste");
  }));
  $("#batchForm").addEventListener("submit", startBatch);
  $("#queueOnly")?.addEventListener("change", () => {
    const btn = $("#startBatch");
    btn.textContent = $("#queueOnly").checked ? "Queue without starting" : "Start the full relay";
  });
  $("#promptsPerGuide").addEventListener("input", renderPromptEstimate);
  $("#referenceFiles").addEventListener("change", (event) => addReferenceFiles(event.target.files || []));
  $("#clearReferences").addEventListener("click", clearReferences);
  const referenceDropZone = $("#referenceDropZone");
  ["dragenter", "dragover"].forEach((name) => referenceDropZone.addEventListener(name, (event) => {
    event.preventDefault();
    referenceDropZone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => referenceDropZone.addEventListener(name, (event) => {
    event.preventDefault();
    referenceDropZone.classList.remove("is-dragging");
  }));
  referenceDropZone.addEventListener("drop", (event) => addReferenceFiles(event.dataTransfer?.files || []));
  $("#reloadGuides").addEventListener("click", loadGuides);
  $("#guideFiles").addEventListener("change", uploadGuides);
  $("#settingsForm").addEventListener("submit", saveSettings);
  $("#civitaiForm")?.addEventListener("submit", saveCivitaiSettings);
  $("#civitaiSync")?.addEventListener("click", syncCivitai);
  $("#civitaiQueuePublish")?.addEventListener("click", publishCivitaiQueue);
  $("#civitaiQueueClear")?.addEventListener("click", civitaiQueueClear);
  $("#civitaiSyncQueue")?.addEventListener("click", syncCivitai);
  $("#workflowFile").addEventListener("change", uploadWorkflow);
  $("#imageToolWorkflowFile").addEventListener("change", uploadImageToolWorkflows);
  $("#workflowLibrary").addEventListener("change", selectWorkflow);
  $("#deleteWorkflow").addEventListener("click", deleteWorkflow);
  $("#mappingForm").addEventListener("submit", saveMapping);
  $("#pauseBatch").addEventListener("click", () => state.activeBatchId && controlBatch(state.activeBatchId, "pause"));
  $("#retryPrompt").addEventListener("click", () => controlCurrentPrompt("retry"));
  $("#regeneratePrompt").addEventListener("click", () => controlCurrentPrompt("regenerate"));
  $("#skipPrompt").addEventListener("click", () => controlCurrentPrompt("skip"));
  $("#resumeBatch").addEventListener("click", () => state.liveBatchId && controlBatch(state.liveBatchId, "resume"));
  $("#cancelBatch").addEventListener("click", () => state.liveBatchId && confirm("Cancel this job? Finished images already saved will be kept.") && controlBatch(state.liveBatchId, "cancel"));
  $("#stopComfyNow").addEventListener("click", async () => {
    if (await confirmJobAction("Release ComfyUI's GPU memory?", "Cancels the current job and holds waiting jobs. ComfyUI stays open; its models are unloaded. Saved images are kept.", "Release memory")) await stopComfyNow();
  });
  $("#exitComfyApp").addEventListener("click", async () => {
    if (await confirmJobAction("Close ComfyUI?", "Cancels the current job, holds waiting jobs, and closes the configured ComfyUI process. Saved images are kept.", "Close ComfyUI")) await exitComfyApp();
  });
  $("#unloadLm").addEventListener("click", () => safetyAction("/api/actions/unload-lm", "Unloading prompt backend…", "prompt backend models unloaded."));
  $("#freeComfy").addEventListener("click", () => safetyAction("/api/actions/free-comfy", "Freeing ComfyUI…", "ComfyUI models and memory released."));
  ["#lmStatus", "#comfyStatus"].forEach((selector) => $(selector).addEventListener("click", () => switchTab("settings")));
  $("#guideStatus").addEventListener("click", () => switchTab("run"));
  let searchTimer;
  $("#librarySearch").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => refreshBatches(), 250);
  });
  $("#galleryBatchSelect").addEventListener("change", (event) => openGalleryBatch(event.target.value));
  $("#wallBatchSelect").addEventListener("change", (event) => openWallBatch(event.target.value));
  $("#postprocessBatchSelect").addEventListener("change", (event) => openPostprocessBatch(event.target.value));
  $("#leaderboardSearch").addEventListener("input", renderLeaderboard);
  $("#leaderboardSort").addEventListener("change", renderLeaderboard);
  $("#refreshToolResults").addEventListener("click", loadImageToolResults);
  $("#refreshRecycleBin").addEventListener("click", loadRecycleBin);
  const finderDropZone = $("#finderDropZone");
  if (finderDropZone) {
    finderDropZone.addEventListener("click", () => { $("#finderFiles")?.click(); });
    finderDropZone.addEventListener("dragover", (e) => { e.preventDefault(); finderDropZone.classList.add("is-dragging"); });
    finderDropZone.addEventListener("dragleave", () => { finderDropZone.classList.remove("is-dragging"); });
    finderDropZone.addEventListener("drop", (event) => { event.preventDefault(); finderDropZone.classList.remove("is-dragging"); const file = (event.dataTransfer?.files || [])[0]; if (file) findArchivedImage(file); });
  }
  $("#finderFiles")?.addEventListener("change", (event) => { const file = (event.target.files || [])[0]; if (file) findArchivedImage(file); event.target.value = ""; });
  const recycleDropZone = $("#recycleDropZone");
  if (recycleDropZone) {
    recycleDropZone.addEventListener("click", () => browseAndRecycle());
    recycleDropZone.addEventListener("dragover", (e) => { e.preventDefault(); recycleDropZone.classList.add("is-dragging"); });
    recycleDropZone.addEventListener("dragleave", () => { recycleDropZone.classList.remove("is-dragging"); });
    recycleDropZone.addEventListener("drop", (event) => { event.preventDefault(); recycleDropZone.classList.remove("is-dragging"); recycleExternalFiles(event.dataTransfer?.files || []); });
  }
  $("#recycleDropFiles")?.addEventListener("change", (event) => { recycleExternalFiles(event.target.files || []); event.target.value = ""; });
  $("#stopEntireQueue")?.addEventListener("click", stopEntireQueue);
  $("#convertMetadataBatch").addEventListener("click", convertMetadataBatch);
  $("#imageDetailDialog").addEventListener("close", () => {
    document.body.classList.remove("has-modal");
    state.selectedGalleryIndex = null;
  });
  $("#imageLightboxDialog").addEventListener("close", () => {
    document.body.classList.remove("has-modal");
    state.selectedWallIndex = null;
  });
  document.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target?.tagName);
    const detailDialog = $("#imageDetailDialog");
    if (detailDialog?.open && state.selectedGalleryIndex != null && !typing) {
      const family = galleryImageFamily(state.galleryItems[state.selectedGalleryIndex]);
      const familyIndex = family.findIndex((item) => item.galleryIndex === state.selectedGalleryIndex);
      if (event.key === "ArrowLeft" && familyIndex > 0) {
        event.preventDefault();
        showImageDetail(family[familyIndex - 1].galleryIndex);
      }
      if (event.key === "ArrowRight" && familyIndex >= 0 && familyIndex < family.length - 1) {
        event.preventDefault();
        showImageDetail(family[familyIndex + 1].galleryIndex);
      }
      return;
    }
    const dialog = $("#imageLightboxDialog");
    if (!dialog?.open || state.selectedWallIndex == null) return;
    if (event.key === "ArrowLeft" && state.selectedWallIndex > 0) {
      event.preventDefault();
      showImageLightbox(state.selectedWallIndex - 1);
    }
    if (event.key === "ArrowRight" && state.selectedWallIndex < state.wallItems.length - 1) {
      event.preventDefault();
      showImageLightbox(state.selectedWallIndex + 1);
    }
  });
}

window.openBatch = openBatch;

async function initialize() {
  bindEvents();
  try {
    const payload = await api("/api/bootstrap");
    state.config = payload.config || {};
    state.workflows = payload.workflows || [];
    state.imageToolWorkflows = payload.image_tool_workflows || [];
    state.references = payload.references || [];
    state.selectedReferenceIds = savedReferenceSelection();
    state.workflow = payload.workflow;
    state.batches = payload.batches || [];
    state.activeBatchId = payload.active?.batch_id || null;
    populateSettings();
    renderWorkflowChoices(state.config.active_workflow_id);
    renderWorkflow(state.workflow);
    renderImageToolWorkflows();
    renderReferenceTray();
    $("#batchList").innerHTML = state.batches.length ? state.batches.map(batchListMarkup).join("") : '<p class="empty-note">No batches yet.</p>';
    renderGalleryBatchChoices();
    await Promise.all([loadModels(), loadGuides(), refreshStatus()]);
    const requestedBatchId = new URLSearchParams(window.location.search).get("batch");
    if (requestedBatchId) {
      switchTab("library");
      await openBatch(requestedBatchId);
    } else if (state.activeBatchId) {
      startActivePolling();
    } else {
      await showLatestBatch();
    }
    state.statusTimer = setInterval(refreshStatus, 8000);
  } catch (error) {
    toast(error.message, true);
  }
}

initialize();
