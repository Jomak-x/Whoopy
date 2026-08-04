const state = {
  mode: "prompt",
  taskId: null,
  pollTimer: null,
  activeRunId: null,
};

const byId = (id) => document.getElementById(id);

const elements = {
  input: byId("meditation-input"),
  inputLabel: byId("input-label"),
  inputHelp: byId("input-help"),
  minutes: byId("minutes"),
  minutesValue: byId("minutes-value"),
  ttsModel: byId("tts-model"),
  ttsModelDetail: byId("tts-model-detail"),
  mossControls: byId("moss-controls"),
  mossLanguage: byId("moss-language"),
  mossVoiceSource: byId("moss-voice-source"),
  mossInstruction: byId("moss-instruction"),
  voice: byId("voice"),
  speed: byId("speed"),
  generate: byId("generate-button"),
  testVoice: byId("test-voice-button"),
  taskPanel: byId("task-panel"),
  taskTitle: byId("task-title"),
  taskMessage: byId("task-message"),
  cancel: byId("cancel-button"),
  errorPanel: byId("error-panel"),
  errorMessage: byId("error-message"),
  status: byId("status-content"),
  runs: byId("runs-list"),
  dialog: byId("run-dialog"),
  dialogTitle: byId("dialog-title"),
  audio: byId("audio-player"),
  artifact: byId("artifact-content"),
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed with status ${response.status}.`);
  }
  return payload;
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".mode-button").forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
  });
  const promptMode = mode === "prompt";
  elements.inputLabel.textContent = promptMode
    ? "What kind of moment do you need?"
    : "Paste your finished meditation script";
  elements.input.placeholder = promptMode
    ? "A gentle three-minute grounding meditation after a busy day…"
    : "Welcome to this moment.\\n\\n[pause: 3s]\\n\\nLet your shoulders soften.";
  elements.inputHelp.textContent = promptMode
    ? "Whoopy’s local Standard model will create and validate the plan and words."
    : "Use [pause: 3s] for exact silence. Basic mode needs speech models, but no local LLM.";
  document.querySelector(".duration-setting").classList.toggle("hidden", !promptMode);
  byId("model-note").textContent = promptMode
    ? "Standard local model · no upload"
    : "Basic local speech · no upload";
}

async function loadStatus() {
  elements.status.innerHTML =
    '<div class="status-skeleton"></div><div class="status-skeleton short"></div>';
  try {
    const result = await api("/api/status");
    if (!result.ok) throw new Error(result.error);
    const labels = { basic: "Script & speech", standard: "Prompt to meditation" };
    const profileRows = Object.entries(result.profiles)
      .map(([name, profile]) => {
        let badge = "Ready";
        let badgeClass = "";
        if (!profile.compatible) {
          badge = "Too demanding";
          badgeClass = "blocked";
        } else if (!profile.installed) {
          badge = "Download needed";
          badgeClass = "missing";
        }
        return `
          <div class="profile-status">
            <span>${escapeHtml(labels[name] || name)}</span>
            <span class="status-badge ${badgeClass}">${badge}</span>
          </div>`;
      })
      .join("");
    const speechLabels = {
      kokoro: "Kokoro",
      "fish-1.4": "Fish 1.4",
      "moss-local-v1.5": "MOSS Local 5B",
      "moss-v1.5": "MOSS flagship 8B",
    };
    const speechRows = Object.entries(result.speech_models || {})
      .filter(([name]) => name !== "fish-s2")
      .map(([name, model]) => {
        const option = elements.ttsModel.querySelector(`option[value="${name}"]`);
        if (option) option.disabled = !model.ready;
        return `
          <div class="profile-status">
            <span>${escapeHtml(speechLabels[name] || name)}</span>
            <span class="status-badge ${model.ready ? "" : "missing"}">
              ${model.ready ? "Ready" : "Not installed"}
            </span>
          </div>`;
      })
      .join("");
    elements.status.innerHTML = profileRows + speechRows;
    if (elements.ttsModel.selectedOptions[0]?.disabled) {
      elements.ttsModel.value = "kokoro";
      updateSpeechModel();
    }
  } catch (error) {
    elements.status.innerHTML = `<p class="input-help">${escapeHtml(error.message)}</p>`;
  }
}

async function startGeneration() {
  clearError();
  const text = elements.input.value.trim();
  if (!text) {
    showError("Tell Whoopy what to create first.");
    elements.input.focus();
    return;
  }
  elements.generate.disabled = true;
  elements.taskPanel.classList.remove("hidden");
  elements.taskTitle.textContent =
    state.mode === "prompt" ? "Creating your meditation" : "Giving your script a voice";
  elements.taskMessage.textContent =
    state.mode === "prompt"
      ? "Planning and writing with the local model…"
      : "Compiling pauses and preparing speech…";
  try {
    const task = await api("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: state.mode,
        text,
        minutes: Number(elements.minutes.value),
        tts_model: elements.ttsModel.value,
        voice: elements.voice.value,
        speed: Number(elements.speed.value),
        moss_language: elements.mossLanguage.value,
        moss_instruction: elements.mossInstruction.value,
        moss_use_reference: elements.mossVoiceSource.value === "reference",
      }),
    });
    state.taskId = task.task_id;
    schedulePoll();
  } catch (error) {
    finishTask();
    showError(error.message);
  }
}

async function testVoice() {
  clearError();
  elements.testVoice.disabled = true;
  elements.taskPanel.classList.remove("hidden");
  elements.taskTitle.textContent = "Creating a voice sample";
  elements.taskMessage.textContent = "Loading the selected speech model locally…";
  try {
    const task = await api("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: "script",
        text:
          "Notice the support beneath you.\\n\\n[pause: 2.4s]\\n\\nLet your breathing move at its own natural pace.",
        tts_model: elements.ttsModel.value,
        voice: elements.voice.value,
        speed: Number(elements.speed.value),
        moss_language: elements.mossLanguage.value,
        moss_instruction: elements.mossInstruction.value,
        moss_use_reference: elements.mossVoiceSource.value === "reference",
      }),
    });
    state.taskId = task.task_id;
    schedulePoll();
  } catch (error) {
    finishTask();
    showError(error.message);
  }
}

function updateSpeechModel() {
  const fish = elements.ttsModel.value === "fish-1.4";
  const moss = elements.ttsModel.value.startsWith("moss-");
  elements.voice.disabled = fish || moss;
  elements.speed.disabled = fish || moss;
  elements.mossControls.classList.toggle("hidden", !moss);
  if (fish) {
    elements.ttsModelDetail.textContent =
      "Fish 1.4 uses the slow local reference voice. License: CC BY-NC-SA 4.0 (non-commercial). It does not understand [emotion] tags; those are a Fish S2 feature, and S2 is too large for this laptop setup.";
  } else if (moss) {
    const size = elements.ttsModel.value === "moss-v1.5" ? "8B flagship" : "5B Local Transformer";
    elements.ttsModelDetail.textContent =
      `${size}, Apache-2.0. Supports a language tag, voice cloning, a free-form delivery instruction, and native [pause X.Ys] markup. Whoopy still uses its exact timeline pauses between blocks.`;
  } else {
    elements.ttsModelDetail.textContent =
      "Kokoro is the portable Apache-2.0 default. Voice and pace controls below are active.";
  }
}

function schedulePoll() {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = window.setTimeout(pollTask, 1200);
}

async function pollTask() {
  if (!state.taskId) return;
  try {
    const task = await api(`/api/tasks/${state.taskId}`);
    if (task.status === "completed") {
      elements.taskMessage.textContent = "Finished, checked, and saved.";
      finishTask();
      await loadRuns();
      const run = (await api("/api/runs")).runs.find((item) => item.run_id === task.run_id);
      if (run) openRun(run);
      return;
    }
    if (task.status === "failed") {
      finishTask();
      showError(task.error || "Generation failed.");
      return;
    }
    if (task.status === "cancelled") {
      finishTask();
      showError("Generation was cancelled. Existing checkpoints were kept when possible.");
      return;
    }
    elements.taskMessage.textContent =
      state.mode === "prompt"
        ? "The local models are drafting, speaking, and checking. Longer meditations take time."
        : "Whoopy is speaking each segment, joining exact pauses, and checking the WAV.";
    schedulePoll();
  } catch (error) {
    finishTask();
    showError(error.message);
  }
}

function finishTask() {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.taskId = null;
  elements.generate.disabled = false;
  elements.testVoice.disabled = false;
  window.setTimeout(() => elements.taskPanel.classList.add("hidden"), 650);
}

async function cancelTask() {
  if (!state.taskId) return;
  try {
    await api(`/api/tasks/${state.taskId}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    elements.taskMessage.textContent = "Stopping safely…";
  } catch (error) {
    showError(error.message);
  }
}

async function loadRuns() {
  try {
    const result = await api("/api/runs");
    if (!result.runs.length) {
      elements.runs.innerHTML =
        '<div class="empty-state">No completed meditation yet. Your first one will appear here.</div>';
      return;
    }
    elements.runs.innerHTML = result.runs
      .map((run) => {
        const source = run.source_kind === "generated_prompt" ? "Local prompt" : "Your script";
        const speechModels = {
          kokoro: "Kokoro",
          "fish-1.4": "Fish 1.4",
          "moss-local-v1.5": "MOSS Local 5B",
          "moss-v1.5": "MOSS flagship 8B",
        };
        const speechModel = speechModels[run.tts_model] || "";
        const duration = run.duration_seconds ? formatDuration(run.duration_seconds) : run.status;
        const quality =
          run.quality_passed === true
            ? '<span class="quality-mark">✓ checks passed</span>'
            : `<span>${escapeHtml(run.status)}</span>`;
        return `
          <article class="run-card">
            <div>
              <div class="run-card-top">
                <span class="run-type">${source}${speechModel ? ` · ${speechModel}` : ""}</span>
                ${quality}
              </div>
              <h3>${escapeHtml(cleanTitle(run.title))}</h3>
              <p>${formatDate(run.created_at)}</p>
            </div>
            <div class="run-card-bottom">
              <p>${escapeHtml(duration)}</p>
              ${
                run.has_audio
                  ? `<button class="run-open" type="button" data-run-id="${run.run_id}">Listen & inspect</button>`
                  : ""
              }
            </div>
          </article>`;
      })
      .join("");
    elements.runs.querySelectorAll(".run-open").forEach((button) => {
      button.addEventListener("click", () => {
        const run = result.runs.find((item) => item.run_id === button.dataset.runId);
        if (run) openRun(run);
      });
    });
  } catch (error) {
    elements.runs.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function openRun(run) {
  state.activeRunId = run.run_id;
  elements.dialogTitle.textContent = cleanTitle(run.title);
  elements.audio.src = `/api/runs/${run.run_id}/audio`;
  elements.dialog.showModal();
  selectArtifact("script");
}

async function selectArtifact(name) {
  if (!state.activeRunId) return;
  document.querySelectorAll(".artifact-tabs button").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.artifact === name));
  });
  elements.artifact.textContent = "Loading…";
  try {
    const response = await fetch(
      `/api/runs/${state.activeRunId}/artifact/${encodeURIComponent(name)}`,
    );
    if (response.status === 404) {
      elements.artifact.textContent = `This run does not have a ${name} artifact.`;
      return;
    }
    if (!response.ok) throw new Error(`Could not load ${name}.`);
    const text = await response.text();
    if (name !== "script") {
      try {
        elements.artifact.textContent = JSON.stringify(JSON.parse(text), null, 2);
        return;
      } catch {
        // Preserve original text when the artifact is not JSON.
      }
    }
    elements.artifact.textContent = text;
  } catch (error) {
    elements.artifact.textContent = error.message;
  }
}

function showError(message) {
  elements.errorMessage.textContent = message;
  elements.errorPanel.classList.remove("hidden");
}

function clearError() {
  elements.errorPanel.classList.add("hidden");
  elements.errorMessage.textContent = "";
}

function cleanTitle(title) {
  return title.startsWith("Render local script: ") ? title.replace("Render local script: ", "") : title;
}

function formatDuration(seconds) {
  const rounded = Math.round(seconds);
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

document.querySelectorAll(".mode-button").forEach((button) => {
  button.addEventListener("click", () => setMode(button.dataset.mode));
});
document.querySelectorAll(".artifact-tabs button").forEach((button) => {
  button.addEventListener("click", () => selectArtifact(button.dataset.artifact));
});
elements.minutes.addEventListener("input", () => {
  elements.minutesValue.textContent = `${elements.minutes.value} min`;
});
elements.generate.addEventListener("click", startGeneration);
elements.testVoice.addEventListener("click", testVoice);
elements.ttsModel.addEventListener("change", updateSpeechModel);
elements.cancel.addEventListener("click", cancelTask);
byId("refresh-status").addEventListener("click", loadStatus);
byId("refresh-runs").addEventListener("click", loadRuns);
elements.dialog.addEventListener("close", () => {
  elements.audio.pause();
  elements.audio.removeAttribute("src");
  elements.audio.load();
  state.activeRunId = null;
});

loadStatus();
loadRuns();
updateSpeechModel();
