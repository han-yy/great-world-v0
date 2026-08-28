const state = {
  token: localStorage.getItem("great_world_consent_token"),
  worldId: localStorage.getItem("great_world_id"),
  accessCode: null,
  view: null,
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers["X-Consent-Token"] = state.token;
  if (state.accessCode) headers["X-World-Access-Code"] = state.accessCode;
  const response = await fetch(path, { ...options, headers });
  let body = {};
  try { body = await response.json(); } catch (_) { /* no response body */ }
  if (!response.ok) {
    const problem = new Error(body.detail || body.message || `请求失败（${response.status}）`);
    problem.code = body.code;
    throw problem;
  }
  return body;
}

function text(element, value) {
  element.textContent = value ?? "";
}

function newRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function loadConsentNotice() {
  const notice = await api("/api/reality/consent-notice");
  const copy = $("#consent-copy");
  copy.replaceChildren();
  const intro = document.createElement("p");
  text(intro, notice.summary);
  copy.append(intro);
  const list = document.createElement("ul");
  for (const item of notice.points) {
    const li = document.createElement("li");
    text(li, item);
    list.append(li);
  }
  copy.append(list);
  $("#consent-form").dataset.noticeVersion = notice.version;
  $("#access-code-fields").hidden = !notice.access_code_required;
}

async function enterWorld() {
  const joined = await api("/api/worlds/default/join", { method: "POST", body: "{}" });
  state.worldId = joined.world_id;
  localStorage.setItem("great_world_id", state.worldId);
  $("#world-app").hidden = false;
  await refreshView();
}

async function refreshView() {
  if (!state.worldId) return;
  const view = await api(`/api/worlds/${encodeURIComponent(state.worldId)}/view`);
  if (view.world.is_archived && view.world.current_world_id !== state.worldId) {
    await enterWorld();
    return;
  }
  state.view = view;
  render(view);
}

function render(view) {
  text($("#world-name"), view.world.name);
  text($("#self-name"), view.self.name);
  text($("#self-location"), view.self.location_name || "你一时说不清这是哪里");
  text($("#child-description"), view.child.description);
  const abilityText = view.child.capabilities.length
    ? `ta 现在已经能${view.child.capabilities.join("、")}。`
    : "ta 还在学习怎样感知这里。";
  text($("#child-capabilities"), abilityText);
  text(
    $("#child-goal"),
    view.child.goal
      ? `ta 最近决定先做这件事：${view.child.goal}`
      : "ta 暂时没有决定接下来专心做什么。",
  );

  renderLocations(view.locations, view.self.location_id);
  renderTimeline(view.experiences);
  renderWishes(view.wishes);
}

function renderLocations(locations, currentId) {
  const grid = $("#location-grid");
  grid.replaceChildren();
  for (const location of locations) {
    const card = document.createElement("article");
    card.className = `location-card${location.id === currentId ? " current" : ""}`;
    const title = document.createElement("h3");
    text(title, location.name);
    const description = document.createElement("p");
    text(description, location.description || "这里很安静。");
    card.append(title, description);
    if (location.id === currentId) {
      const marker = document.createElement("span");
      marker.className = "marker";
      text(marker, "你在这里");
      card.append(marker);
    }
    const people = document.createElement("div");
    people.className = "resident-list";
    for (const resident of location.occupants || []) {
      const badge = document.createElement("span");
      badge.className = "resident";
      text(badge, resident.name);
      people.append(badge);
    }
    card.append(people);
    grid.append(card);
  }
}

function renderTimeline(experiences) {
  const list = $("#timeline");
  list.replaceChildren();
  if (!experiences.length) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    text(empty, "你刚来到这里，周围还很安静。");
    list.append(empty);
    return;
  }
  const template = $("#timeline-item-template");
  for (const experience of [...experiences].reverse()) {
    const item = template.content.cloneNode(true);
    text(item.querySelector(".timeline-title"), experience.summary);
    text(item.querySelector(".timeline-detail"), experience.detail || "");
    list.append(item);
  }
}

function renderWishes(wishes) {
  const pool = $("#wish-pool");
  pool.replaceChildren();
  if (!wishes.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    text(empty, "今天还没有人在这里留下愿望。");
    pool.append(empty);
    return;
  }
  for (const wish of wishes) {
    const card = document.createElement("div");
    card.className = "wish";
    text(card, `“${wish.text}”`);
    pool.append(card);
  }
}

function renderTurnFeedback(feedback) {
  const panel = $("#turn-feedback");
  const container = $("#turn-feedback-items");
  container.replaceChildren();
  for (const item of feedback || []) {
    const block = document.createElement("div");
    block.className = "feedback-item";
    const summary = document.createElement("p");
    summary.className = "feedback-summary";
    text(summary, item.summary);
    const detail = document.createElement("p");
    detail.className = "feedback-detail";
    text(detail, item.detail || "");
    block.append(summary, detail);
    container.append(block);
  }
  panel.hidden = !container.childElementCount;
}

async function submitMoment(event) {
  event.preventDefault();
  if (!state.view) return;
  const input = $("#moment-input");
  const submit = $("#moment-submit");
  const status = $("#moment-status");
  const intent = input.value.trim();
  if (!intent) return;

  status.classList.remove("error");
  submit.disabled = true;
  text(status, "请稍候，事情正在发生……");
  try {
    const result = await api(`/api/worlds/${encodeURIComponent(state.worldId)}/turns`, {
      method: "POST",
      body: JSON.stringify({
        text: intent,
        observed_seq: state.view.world.seq,
        request_id: newRequestId(),
      }),
    });
    input.value = "";
    state.view = result.view;
    render(result.view);
    renderTurnFeedback(result.feedback);
    text(status, result.message || "这里继续生活了下去。");
  } catch (problem) {
    status.classList.add("error");
    if (problem.code === "world_archived") {
      await enterWorld();
      status.classList.remove("error");
      text(status, "旧世界已经封存。你来到了一个刚刚开始的新世界，刚才的话还留在输入框里。");
    } else if (problem.code === "world_advanced") {
      await refreshView();
      text(status, "刚才这里又发生了些事。内容还留着，你可以再继续一次。");
    } else {
      text(status, problem.message);
    }
  } finally {
    submit.disabled = false;
    input.focus();
  }
}

async function submitConsent(event) {
  event.preventDefault();
  const error = $("#consent-error");
  text(error, "");
  try {
    state.accessCode = $("#access-code").value.trim() || null;
    const result = await api("/api/reality/consents", {
      method: "POST",
      body: JSON.stringify({
        display_name: $("#display-name").value.trim(),
        accepted: $("#consent-check").checked,
        notice_version: $("#consent-form").dataset.noticeVersion,
      }),
    });
    state.token = result.consent_token;
    state.accessCode = null;
    $("#access-code").value = "";
    localStorage.setItem("great_world_consent_token", state.token);
    $("#consent-dialog").close();
    await enterWorld();
  } catch (problem) {
    text(error, problem.message);
  }
}

async function init() {
  await loadConsentNotice();
  $("#consent-form").addEventListener("submit", submitConsent);
  $("#moment-form").addEventListener("submit", submitMoment);
  $("#moment-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      $("#moment-form").requestSubmit();
    }
  });
  $("#refresh-button").addEventListener("click", refreshView);

  if (!state.token) {
    $("#consent-dialog").showModal();
    return;
  }
  try {
    await enterWorld();
  } catch (_) {
    state.token = null;
    state.worldId = null;
    localStorage.removeItem("great_world_consent_token");
    localStorage.removeItem("great_world_id");
    $("#consent-dialog").showModal();
  }
}

init().catch((error) => {
  document.body.textContent = `入口暂时打不开：${error.message}`;
});
